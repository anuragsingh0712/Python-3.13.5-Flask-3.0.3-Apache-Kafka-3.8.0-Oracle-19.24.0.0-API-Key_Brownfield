"""Gym class scheduling and class-enrollment resource group."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from flask import Blueprint, current_app, request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core import (
    ConflictError,
    DomainError,
    NotFoundError,
    cursor_params,
    encode_cursor,
    parse_model,
    roles_required,
    success,
)
from app.repository import EventPublisher, NullEventPublisher, Repository

ClassStatus = Literal["SCHEDULED", "CANCELLED"]


class ClassCreateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=160)
    trainer_id: int = Field(gt=0)
    scheduled_at: datetime
    capacity: int = Field(ge=1, le=1000)

    @field_validator("scheduled_at")
    @classmethod
    def scheduled_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("scheduled_at must include a timezone")
        return value


class ClassPatchDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    trainer_id: int | None = Field(default=None, gt=0)
    scheduled_at: datetime | None = None
    capacity: int | None = Field(default=None, ge=1, le=1000)
    status: ClassStatus | None = None

    @field_validator("scheduled_at")
    @classmethod
    def patched_scheduled_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("scheduled_at must include a timezone")
        return value


class EnrollmentCreateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_id: int = Field(gt=0)


class ClassesService:
    def __init__(
        self, repository: Repository, publisher: EventPublisher | None = None
    ) -> None:
        self.repository = repository
        self.publisher = publisher or NullEventPublisher()

    @staticmethod
    def _class_payload(row: dict[str, Any]) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _enrollment_payload(row: dict[str, Any]) -> dict[str, Any]:
        return dict(row)

    def list_classes(
        self, cursor: int | None, limit: int, status: str | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if status is not None and status.upper() not in ClassStatus.__args__:
            raise DomainError("validation_error", "status is invalid.", 422)
        rows = self.repository.list(
            "gym_classes", status=status.upper() if status else None
        )
        rows.sort(key=lambda item: item["id"])
        rows = [row for row in rows if cursor is None or row["id"] > cursor]
        page = rows[:limit]
        meta: dict[str, Any] = {"limit": limit}
        if len(rows) > limit:
            meta["next_cursor"] = encode_cursor(page[-1]["id"])
        return [self._class_payload(row) for row in page], meta

    def get_class(self, class_id: int) -> dict[str, Any]:
        return self._class_payload(self.repository.get("gym_classes", class_id))

    def create_class(self, dto: ClassCreateDTO) -> dict[str, Any]:
        values = dto.model_dump()
        values.update({"status": "SCHEDULED"})
        with self.repository.transaction():
            created = self.repository.create("gym_classes", values)
        self.publisher.publish_after_commit(
            "gym.class.created.v1", {"class": self._class_payload(created)}
        )
        return self._class_payload(created)

    def update_class(self, class_id: int, dto: ClassPatchDTO) -> dict[str, Any]:
        changes = dto.model_dump(exclude_none=True)
        if not changes:
            raise DomainError(
                "validation_error", "At least one field must be supplied.", 422
            )
        with self.repository.transaction():
            existing = self.repository.get("gym_classes", class_id, for_update=True)
            if existing.get("status") == "CANCELLED" and changes.get("status") not in (
                None,
                "CANCELLED",
            ):
                raise ConflictError("A cancelled class cannot be reopened.")
            if "capacity" in changes:
                enrolled = len(
                    self.repository.list(
                        "class_enrollments", class_id=class_id, status="ENROLLED"
                    )
                )
                if changes["capacity"] < enrolled:
                    raise ConflictError(
                        "capacity cannot be lower than current enrollments."
                    )
            updated = self.repository.update("gym_classes", class_id, changes)
        action = "cancelled" if changes.get("status") == "CANCELLED" else "updated"
        self.publisher.publish_after_commit(
            f"gym.class.{action}.v1", {"class": self._class_payload(updated)}
        )
        return self._class_payload(updated)

    @staticmethod
    def _as_date(value: Any) -> date | None:
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

    def _locked_usable_membership(self, member_id: int) -> dict[str, Any] | None:
        """Lock and return an active, unexpired membership with available visits."""
        today = date.today()
        candidates: list[dict[str, Any]] = []
        for row in self.repository.list("memberships", member_id=member_id):
            membership = self.repository.get("memberships", row["id"], for_update=True)
            start, end = (
                self._as_date(membership.get("start_date")),
                self._as_date(membership.get("end_date")),
            )
            remaining = membership.get("remaining_visits")
            if (
                str(membership.get("status", "")).upper() == "ACTIVE"
                and start is not None
                and end is not None
                and start <= today <= end
                and (remaining is None or int(remaining) > 0)
            ):
                candidates.append(membership)
        return min(candidates, key=lambda item: item["id"]) if candidates else None

    def enroll(self, class_id: int, dto: EnrollmentCreateDTO) -> dict[str, Any]:
        """Serialize capacity and membership-visit consumption under row locks."""
        with self.repository.transaction():
            gym_class = self.repository.get("gym_classes", class_id, for_update=True)
            if gym_class.get("status") != "SCHEDULED":
                raise ConflictError("Only scheduled classes can be enrolled in.")
            member = self.repository.get("members", dto.member_id, for_update=True)
            if str(member.get("status", "")).upper() != "ACTIVE":
                raise ConflictError("The member is not active.")
            membership = self._locked_usable_membership(dto.member_id)
            if membership is None:
                raise ConflictError("The member has no usable membership.")
            if self.repository.list(
                "class_enrollments",
                class_id=class_id,
                member_id=dto.member_id,
                status="ENROLLED",
            ):
                raise ConflictError("The member is already enrolled in this class.")
            if len(
                self.repository.list(
                    "class_enrollments", class_id=class_id, status="ENROLLED"
                )
            ) >= int(gym_class["capacity"]):
                raise ConflictError("The class is at capacity.")
            if membership.get("remaining_visits") is not None:
                self.repository.update(
                    "memberships",
                    membership["id"],
                    {"remaining_visits": int(membership["remaining_visits"]) - 1},
                )
            now = datetime.now(timezone.utc)
            enrollment = self.repository.create(
                "class_enrollments",
                {
                    "class_id": class_id,
                    "member_id": dto.member_id,
                    "status": "ENROLLED",
                    "enrolled_at": now,
                },
            )
        self.publisher.publish_after_commit(
            "gym.enrollment.created.v1",
            {"enrollment": self._enrollment_payload(enrollment)},
        )
        return self._enrollment_payload(enrollment)

    def cancel_enrollment(self, class_id: int, enrollment_id: int) -> dict[str, Any]:
        with self.repository.transaction():
            self.repository.get("gym_classes", class_id, for_update=True)
            enrollment = self.repository.get(
                "class_enrollments", enrollment_id, for_update=True
            )
            if enrollment.get("class_id") != class_id:
                raise NotFoundError("enrollment")
            if enrollment.get("status") != "ENROLLED":
                raise ConflictError("The enrollment is already cancelled.")
            updated = self.repository.update(
                "class_enrollments", enrollment_id, {"status": "CANCELLED"}
            )
        self.publisher.publish_after_commit(
            "gym.enrollment.cancelled.v1",
            {"enrollment": self._enrollment_payload(updated)},
        )
        return self._enrollment_payload(updated)


def _service() -> ClassesService:
    service = current_app.extensions.get("classes_service")
    if not isinstance(service, ClassesService):
        raise RuntimeError("ClassesService is not configured.")
    return service


classes_bp = Blueprint("classes", __name__, url_prefix="/api/v1/classes")


@classes_bp.get("")
def list_classes():
    cursor, limit = cursor_params()
    items, meta = _service().list_classes(cursor, limit, request.args.get("status"))
    return success(items, meta=meta)


@classes_bp.get("/<int:class_id>")
def get_class(class_id: int):
    return success(_service().get_class(class_id))


@classes_bp.post("")
@roles_required("ADMIN", "STAFF")
def create_class():
    return success(_service().create_class(parse_model(ClassCreateDTO)), status=201)


@classes_bp.patch("/<int:class_id>")
@roles_required("ADMIN", "STAFF")
def patch_class(class_id: int):
    return success(_service().update_class(class_id, parse_model(ClassPatchDTO)))


@classes_bp.post("/<int:class_id>/enrollments")
@roles_required("ADMIN", "STAFF")
def create_enrollment(class_id: int):
    return success(
        _service().enroll(class_id, parse_model(EnrollmentCreateDTO)), status=201
    )


@classes_bp.delete("/<int:class_id>/enrollments/<int:enrollment_id>")
@roles_required("ADMIN", "STAFF")
def delete_enrollment(class_id: int, enrollment_id: int):
    return success(_service().cancel_enrollment(class_id, enrollment_id))


def register_classes(
    app: Any, repository: Repository, publisher: EventPublisher | None = None
) -> None:
    app.extensions["classes_service"] = ClassesService(repository, publisher)
    app.register_blueprint(classes_bp)
