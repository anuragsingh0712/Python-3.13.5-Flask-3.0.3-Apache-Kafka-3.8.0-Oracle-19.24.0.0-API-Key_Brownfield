"""Attendance resource: eligibility-checked check-ins and check-outs."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from flask import Blueprint, current_app, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core import (
    ConflictError,
    DomainError,
    cursor_params,
    encode_cursor,
    roles_required,
    success,
)
from app.repository import EventPublisher, NullEventPublisher, Repository

attendance_bp = Blueprint("attendance", __name__)


class CheckInDTO(BaseModel):
    """Input accepted for a check-in.  The timestamp is normally server generated."""

    model_config = ConfigDict(extra="forbid")
    member_id: int = Field(gt=0)
    check_in_at: datetime | None = None

    @field_validator("check_in_at")
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("check_in_at must include a timezone")
        return value.astimezone(timezone.utc) if value else value


class CheckOutDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_out_at: datetime | None = None

    @field_validator("check_out_at")
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("check_out_at must include a timezone")
        return value.astimezone(timezone.utc) if value else value


class AttendanceService:
    """Transactional domain operations for the members/memberships/attendance tables."""

    def __init__(
        self, repository: Repository, publisher: EventPublisher | None = None
    ) -> None:
        self.repository = repository
        self.publisher = publisher or NullEventPublisher()

    @staticmethod
    def _is_active(row: dict[str, Any]) -> bool:
        # A missing status is deliberately not treated as active: legacy rows must be migrated.
        return str(row.get("status", "")).upper() == "ACTIVE"

    @staticmethod
    def _as_date(value: Any) -> date | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None
        return None

    @classmethod
    def _membership_is_valid(cls, membership: dict[str, Any], today: date) -> bool:
        if not cls._is_active(membership):
            return False
        starts = cls._as_date(membership.get("start_date"))
        ends = cls._as_date(membership.get("end_date"))
        if starts is None or ends is None or not starts <= today <= ends:
            return False
        remaining = membership.get("remaining_visits")
        return remaining is None or int(remaining) > 0

    @staticmethod
    def _event_payload(attendance: dict[str, Any]) -> dict[str, Any]:
        # Events are intentionally a snapshot, so subsequent checkout does not mutate check-in data.
        return {
            "attendance_id": attendance["id"],
            "member_id": attendance["member_id"],
            "membership_id": attendance["membership_id"],
            "occurred_at": attendance.get("check_out_at") or attendance["check_in_at"],
        }

    def check_in(self, dto: CheckInDTO) -> dict[str, Any]:
        check_in_at = dto.check_in_at or datetime.now(timezone.utc)
        today = check_in_at.date()
        with self.repository.transaction():
            member = self.repository.get("members", dto.member_id, for_update=True)
            if not self._is_active(member):
                raise ConflictError("The member is not active.")

            # Lock the chosen membership before evaluating its balance/date range. The transaction
            # also serializes competing check-ins for this member in the supplied repositories.
            candidates = self.repository.list("memberships", member_id=dto.member_id)
            valid: list[dict[str, Any]] = []
            for candidate in candidates:
                locked = self.repository.get(
                    "memberships", candidate["id"], for_update=True
                )
                if self._membership_is_valid(locked, today):
                    valid.append(locked)
            if not valid:
                raise ConflictError("The member has no valid active membership.")
            membership = sorted(valid, key=lambda item: item["id"])[0]

            if any(
                row.get("check_out_at") is None
                for row in self.repository.list("attendance", member_id=dto.member_id)
            ):
                raise ConflictError("The member already has an open check-in.")

            # Null means an unlimited plan. A finite visit is consumed only after all eligibility
            # checks, and in the same transaction as creating the attendance record.
            if membership.get("remaining_visits") is not None:
                self.repository.update(
                    "memberships",
                    membership["id"],
                    {"remaining_visits": int(membership["remaining_visits"]) - 1},
                )
            attendance = self.repository.create(
                "attendance",
                {
                    "member_id": dto.member_id,
                    "membership_id": membership["id"],
                    "check_in_at": check_in_at,
                    "check_out_at": None,
                    "duration_minutes": None,
                },
            )
        self.publisher.publish_after_commit(
            "gym.attendance.check_in.v1", self._event_payload(attendance)
        )
        return attendance

    def check_out(
        self, attendance_id: int, dto: CheckOutDTO | None = None
    ) -> dict[str, Any]:
        if attendance_id <= 0:
            raise DomainError("validation_error", "attendance id is invalid.", 422)
        check_out_at = (dto.check_out_at if dto else None) or datetime.now(timezone.utc)
        with self.repository.transaction():
            attendance = self.repository.get(
                "attendance", attendance_id, for_update=True
            )
            if attendance.get("check_out_at") is not None:
                raise ConflictError("This attendance record is already checked out.")
            check_in_at = attendance.get("check_in_at")
            if not isinstance(check_in_at, datetime):
                raise DomainError(
                    "invalid_attendance",
                    "Attendance check-in timestamp is invalid.",
                    409,
                )
            if check_in_at.tzinfo is None:
                check_in_at = check_in_at.replace(tzinfo=timezone.utc)
            if check_out_at < check_in_at:
                raise DomainError(
                    "validation_error", "check_out_at cannot precede check_in_at.", 422
                )
            duration = int((check_out_at - check_in_at).total_seconds() // 60)
            attendance = self.repository.update(
                "attendance",
                attendance_id,
                {
                    "check_out_at": check_out_at,
                    "duration_minutes": duration,
                },
            )
        self.publisher.publish_after_commit(
            "gym.attendance.check_out.v1", self._event_payload(attendance)
        )
        return attendance

    def list_attendance(
        self,
        *,
        member_id: int | None = None,
        membership_id: int | None = None,
        cursor: int | None = None,
        limit: int = 20,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        rows = self.repository.list(
            "attendance", member_id=member_id, membership_id=membership_id
        )

        def in_range(row: dict[str, Any]) -> bool:
            checked_in = self._as_date(row.get("check_in_at"))
            return (
                checked_in is not None
                and (from_date is None or checked_in >= from_date)
                and (to_date is None or checked_in <= to_date)
            )

        rows = sorted(
            (
                row
                for row in rows
                if in_range(row) and (cursor is None or row["id"] > cursor)
            ),
            key=lambda row: row["id"],
        )
        page = rows[:limit]
        return page, encode_cursor(page[-1]["id"]) if len(
            rows
        ) > limit and page else None


def _service() -> AttendanceService:
    service = current_app.extensions.get(
        "attendance_service"
    ) or current_app.config.get("ATTENDANCE_SERVICE")
    if not isinstance(service, AttendanceService):
        raise RuntimeError("AttendanceService has not been configured.")
    return service


def _body(model: type[BaseModel], *, optional: bool = False) -> BaseModel:
    try:
        payload = request.get_json(silent=False)
    except Exception as error:
        raise DomainError(
            "invalid_json", "Request body must be valid JSON.", 400
        ) from error
    if payload is None and optional:
        payload = {}
    if payload is None:
        raise DomainError("invalid_json", "Request body must be valid JSON.", 400)
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise DomainError(
            "validation_error", "Request data is invalid.", 422
        ) from error


def _optional_positive_arg(name: str) -> int | None:
    value = request.args.get(name)
    if value is None:
        return None
    try:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError
        return parsed
    except ValueError:
        raise DomainError("validation_error", f"{name} is invalid.", 422) from None


def _date_arg(name: str) -> date | None:
    raw = request.args.get(name)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise DomainError("validation_error", f"{name} is invalid.", 422) from None


@attendance_bp.post("/attendance/check-in")
@roles_required("ADMIN", "STAFF")
def check_in() -> Any:
    return success(_service().check_in(_body(CheckInDTO)), status=201)


@attendance_bp.post("/attendance/<int:attendance_id>/check-out")
@roles_required("ADMIN", "STAFF")
def check_out(attendance_id: int) -> Any:
    return success(
        _service().check_out(attendance_id, _body(CheckOutDTO, optional=True))
    )


@attendance_bp.get("/attendance")
@roles_required("ADMIN", "STAFF", "TRAINER")
def list_attendance() -> Any:
    cursor, limit = cursor_params()
    from_date, to_date = _date_arg("from_date"), _date_arg("to_date")
    if from_date and to_date and from_date > to_date:
        raise DomainError("validation_error", "from_date cannot be after to_date.", 422)
    rows, next_cursor = _service().list_attendance(
        member_id=_optional_positive_arg("member_id"),
        membership_id=_optional_positive_arg("membership_id"),
        cursor=cursor,
        limit=limit,
        from_date=from_date,
        to_date=to_date,
    )
    return success(rows, meta={"next_cursor": next_cursor, "limit": limit})
