"""Members resource: DTOs, service logic, and the members Flask blueprint."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from flask import Blueprint, current_app, request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core import DomainError, cursor_params, encode_cursor, parse_model, roles_required, success
from app.repository import EventPublisher, Repository

members_bp = Blueprint("members", __name__)

_MEMBER_STATUSES = frozenset({"active", "inactive", "suspended"})
# Cursor semantics are deliberately tied to the monotonically increasing id.
_MEMBER_SORT_FIELDS = frozenset({"id"})
_MEMBER_FILTER_FIELDS = frozenset({"status"})


class _MemberInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=320)
    phone: str = Field(min_length=3, max_length=40)
    status: Literal["active", "inactive", "suspended"] = "active"
    date_of_birth: date | None = None

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.lower()
        if value.count("@") != 1 or value.startswith("@") or value.endswith("@"):
            raise ValueError("email must be valid")
        return value

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        # Preserve a leading + while making equivalent formatted phone numbers unique.
        normalized = "".join(character for character in value if character.isdigit() or character == "+")
        if normalized.count("+") > 1 or "+" in normalized[1:] or len(normalized.lstrip("+")) < 3:
            raise ValueError("phone must be valid")
        return normalized


class MemberCreateDTO(_MemberInput):
    """Payload accepted when a member is created."""


class MemberPatchDTO(BaseModel):
    """Payload accepted when a member is updated; at least one property is required."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, min_length=3, max_length=320)
    phone: str | None = Field(default=None, min_length=3, max_length=40)
    status: Literal["active", "inactive", "suspended"] | None = None
    date_of_birth: date | None = None

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return _MemberInput.normalize_email(value) if value is not None else value

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        return _MemberInput.normalize_phone(value) if value is not None else value


class MemberDTO(BaseModel):
    """The explicit, safe representation returned by the member API."""

    model_config = ConfigDict(extra="ignore")

    id: int
    first_name: str
    last_name: str
    email: str
    phone: str
    status: Literal["active", "inactive", "suspended"]
    date_of_birth: date | None = None
    created_at: datetime
    updated_at: datetime


class MemberService:
    TABLE = "members"

    def __init__(self, repository: Repository, publisher: EventPublisher) -> None:
        self.repository = repository
        self.publisher = publisher

    @staticmethod
    def _dto(row: dict[str, Any]) -> dict[str, Any]:
        return MemberDTO.model_validate(row).model_dump(mode="json")

    def _assert_unique(self, values: dict[str, Any], ignore_id: int | None = None) -> None:
        """Delegate uniqueness checks to the repository's locking-aware implementation."""
        assert_unique = getattr(self.repository, "assert_unique", None)
        if not callable(assert_unique):
            raise RuntimeError("The member repository must support assert_unique.")
        # Check each independently: a duplicate email OR duplicate phone is a conflict.
        for key in ("email", "phone"):
            if key in values:
                assert_unique(self.TABLE, {key: values[key]}, ignore_id=ignore_id)

    def create(self, payload: MemberCreateDTO) -> dict[str, Any]:
        values = payload.model_dump(mode="python")
        now = datetime.now(timezone.utc)
        values.update(created_at=now, updated_at=now)
        with self.repository.transaction():
            self._assert_unique(values)
            row = self.repository.create(self.TABLE, values)
        result = self._dto(row)
        self.publisher.publish_after_commit("gym.member.created.v1", result)
        return result

    def get(self, member_id: int) -> dict[str, Any]:
        return self._dto(self.repository.get(self.TABLE, member_id))

    def update(self, member_id: int, payload: MemberPatchDTO) -> dict[str, Any]:
        values = payload.model_dump(exclude_unset=True, mode="python")
        if not values:
            raise DomainError("validation_error", "At least one member field must be supplied.", 422)
        # Explicit null for date_of_birth is a supported way to clear it. Other nullable
        # fields are blocked by Pydantic's type definitions.
        values["updated_at"] = datetime.now(timezone.utc)
        with self.repository.transaction():
            self.repository.get(self.TABLE, member_id, for_update=True)
            self._assert_unique(values, ignore_id=member_id)
            row = self.repository.update(self.TABLE, member_id, values)
        result = self._dto(row)
        self.publisher.publish_after_commit("gym.member.updated.v1", result)
        return result

    def list(self, *, cursor: int | None, limit: int, status: str | None, search: str | None,
             sort: str, direction: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows = self.repository.list(self.TABLE, status=status)
        if search:
            needle = search.casefold()
            rows = [row for row in rows if needle in " ".join(
                str(row.get(field, "")) for field in ("first_name", "last_name", "email", "phone")
            ).casefold()]
        descending = direction == "desc"
        # The id-only allow-list keeps opaque ID cursors stable under concurrent writes.
        rows.sort(key=lambda row: row[sort], reverse=descending)
        if cursor is not None:
            rows = [row for row in rows if row["id"] < cursor] if descending else [
                row for row in rows if row["id"] > cursor
            ]
        page = rows[:limit]
        meta: dict[str, Any] = {"limit": limit}
        if len(rows) > limit and page:
            meta["next_cursor"] = encode_cursor(page[-1]["id"])
        return [self._dto(row) for row in page], meta


def configure_members(repository: Repository, publisher: EventPublisher) -> MemberService:
    """Attach this module's service to the blueprint before the app registers it."""
    service = MemberService(repository, publisher)
    members_bp.members_service = service  # type: ignore[attr-defined]
    return service


def _service() -> MemberService:
    service = current_app.extensions.get("members_service")
    if service is None:
        service = getattr(members_bp, "members_service", None)
    if not isinstance(service, MemberService):
        raise RuntimeError("Members service has not been configured.")
    return service


def _list_arguments() -> tuple[int | None, int, str | None, str | None, str, str]:
    allowed = {"cursor", "limit", "status", "search", "sort", "order"}
    unknown = set(request.args) - allowed
    if unknown:
        raise DomainError("validation_error", "Unsupported member query parameter.", 422)
    cursor, limit = cursor_params()
    status = request.args.get("status")
    if status is not None and status not in _MEMBER_STATUSES:
        raise DomainError("validation_error", "status is invalid.", 422)
    search = request.args.get("search")
    if search is not None and not search.strip():
        raise DomainError("validation_error", "search is invalid.", 422)
    sort = request.args.get("sort", "id")
    direction = request.args.get("order", "asc").lower()
    if sort not in _MEMBER_SORT_FIELDS or direction not in {"asc", "desc"}:
        raise DomainError("validation_error", "sort or order is invalid.", 422)
    return cursor, limit, status, search.strip() if search else None, sort, direction


@members_bp.get("/members")
def list_members():
    cursor, limit, status, search, sort, direction = _list_arguments()
    data, meta = _service().list(cursor=cursor, limit=limit, status=status, search=search, sort=sort, direction=direction)
    return success(data, meta=meta)


@members_bp.post("/members")
@roles_required("ADMIN", "STAFF")
def create_member():
    return success(_service().create(parse_model(MemberCreateDTO)), status=201)


@members_bp.get("/members/<int:member_id>")
def get_member(member_id: int):
    return success(_service().get(member_id))


@members_bp.patch("/members/<int:member_id>")
@roles_required("ADMIN", "STAFF")
def update_member(member_id: int):
    return success(_service().update(member_id, parse_model(MemberPatchDTO)))
