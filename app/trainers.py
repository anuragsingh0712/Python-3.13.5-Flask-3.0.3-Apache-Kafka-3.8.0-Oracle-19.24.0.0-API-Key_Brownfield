"""Trainer management and trainer/member assignment resource.

The module deliberately has no application-factory dependency.  The repository and
publisher can be injected with :func:`configure_trainers`, or supplied through Flask
extensions/configuration when the blueprint is registered.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, current_app, request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core import (
    ConflictError,
    DomainError,
    cursor_params,
    encode_cursor,
    parse_model,
    roles_required,
    success,
)
from app.repository import EventPublisher, NullEventPublisher, Repository

TRAINER_STATUSES = frozenset({"ACTIVE", "INACTIVE", "ON_LEAVE"})

# A null capacity deliberately represents an unlimited trainer roster.
ASSIGNMENT_CREATED_TOPIC = "gym.trainer_assignment.created.v1"


class _DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TrainerCreateDTO(_DTO):
    """Fields intentionally allow either a display name or split names.

    Deployments have used both forms of the trainer record; all supplied fields are
    preserved in the table row while email, phone, status and capacity are canonical.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    phone: str = Field(min_length=3, max_length=40)
    specialization: str | None = Field(default=None, max_length=200)
    status: str = "ACTIVE"
    max_members: int | None = Field(default=None, gt=0, le=100000)

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        value = value.lower()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("email must be valid")
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        value = value.upper()
        if value not in TRAINER_STATUSES:
            raise ValueError("status is not allowed")
        return value


class TrainerPatchDTO(_DTO):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, min_length=3, max_length=254)
    phone: str | None = Field(default=None, min_length=3, max_length=40)
    specialization: str | None = Field(default=None, max_length=200)
    status: str | None = None
    max_members: int | None = Field(default=None, gt=0, le=100000)

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.lower()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("email must be valid")
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.upper()
        if value not in TRAINER_STATUSES:
            raise ValueError("status is not allowed")
        return value

    @model_validator(mode="after")
    def not_empty(self) -> "TrainerPatchDTO":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class TrainerAssignmentCreateDTO(_DTO):
    trainer_id: int = Field(gt=0)
    member_id: int = Field(gt=0)


class TrainerService:
    """Transactional trainer use-cases, independent of Flask and Oracle details."""

    def __init__(
        self, repo: Repository, publisher: EventPublisher | None = None
    ) -> None:
        self.repo = repo
        self.publisher = publisher or NullEventPublisher()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _assert_unique(
        self, values: dict[str, Any], ignore_id: int | None = None
    ) -> None:
        for row in self.repo.list("trainers"):
            if row["id"] != ignore_id and any(
                row.get(key) == value for key, value in values.items()
            ):
                field = "email" if row.get("email") == values.get("email") else "phone"
                raise ConflictError(f"A trainer with this {field} already exists.")

    def list_trainers(
        self, cursor: int | None, limit: int, status: str | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if status is not None:
            status = status.upper()
            if status not in TRAINER_STATUSES:
                raise DomainError("validation_error", "status is invalid.", 422)
        rows = sorted(
            self.repo.list("trainers", status=status), key=lambda item: item["id"]
        )
        if cursor is not None:
            rows = [row for row in rows if row["id"] > cursor]
        page = rows[:limit]
        meta: dict[str, Any] = (
            {"next_cursor": encode_cursor(page[-1]["id"])}
            if len(rows) > limit and page
            else {}
        )
        return page, meta

    def create_trainer(self, dto: TrainerCreateDTO) -> dict[str, Any]:
        values = dto.model_dump()
        # Oracle owns one canonical trainer name column; split names are API input
        # conveniences and are never sent as nonexistent database columns.
        name = values.pop("name") or " ".join(
            part for part in (values.pop("first_name"), values.pop("last_name")) if part
        )
        if not name:
            raise DomainError(
                "validation_error", "name or first_name/last_name is required.", 422
            )
        values["name"] = name
        with self.repo.transaction():
            self._assert_unique({"email": values["email"], "phone": values["phone"]})
            return self.repo.create("trainers", values)

    def update_trainer(self, trainer_id: int, dto: TrainerPatchDTO) -> dict[str, Any]:
        values = dto.model_dump(exclude_unset=True)
        split_names = [
            values.pop(key) for key in ("first_name", "last_name") if key in values
        ]
        if split_names and "name" not in values:
            current = self.repo.get("trainers", trainer_id)
            values["name"] = (
                " ".join(str(part) for part in split_names if part) or current["name"]
            )
        with self.repo.transaction():
            trainer = self.repo.get("trainers", trainer_id, for_update=True)
            unique = {key: values[key] for key in ("email", "phone") if key in values}
            if unique:
                self._assert_unique(unique, ignore_id=trainer_id)
            # A capacity reduction cannot strand existing assignments.
            # ``None`` means unlimited.  Only a finite capacity can be reduced
            # below (and therefore validated against) the existing roster.
            if "max_members" in values and values["max_members"] is not None:
                assigned = len(
                    self.repo.list("trainer_assignments", trainer_id=trainer_id)
                )
                if assigned > values["max_members"]:
                    raise ConflictError(
                        "max_members cannot be below the current assignment count."
                    )
            return self.repo.update("trainers", trainer["id"], values)

    def create_assignment(self, dto: TrainerAssignmentCreateDTO) -> dict[str, Any]:
        with self.repo.transaction():
            trainer = self.repo.get("trainers", dto.trainer_id, for_update=True)
            member = self.repo.get("members", dto.member_id, for_update=True)
            if trainer.get("status") != "ACTIVE":
                raise ConflictError("Only an ACTIVE trainer can be assigned.")
            if member.get("status") != "ACTIVE":
                raise ConflictError("Only an ACTIVE member can be assigned.")
            if self.repo.list(
                "trainer_assignments", trainer_id=trainer["id"], member_id=member["id"]
            ):
                raise ConflictError("This trainer/member assignment already exists.")
            # A missing legacy field and an explicit null both mean unlimited;
            # never index capacity unconditionally.
            capacity = trainer.get("max_members")
            if capacity is not None and len(
                self.repo.list("trainer_assignments", trainer_id=trainer["id"])
            ) >= int(capacity):
                raise ConflictError("Trainer has reached member capacity.")
            assignment = self.repo.create(
                "trainer_assignments",
                {
                    "trainer_id": trainer["id"],
                    "member_id": member["id"],
                    "assigned_at": self._now(),
                },
            )
        # Leaving the transaction before publishing makes the event strictly post-commit.
        self.publisher.publish_after_commit(ASSIGNMENT_CREATED_TOPIC, assignment)
        return assignment

    def delete_assignment(self, assignment_id: int) -> None:
        with self.repo.transaction():
            assignment = self.repo.get(
                "trainer_assignments", assignment_id, for_update=True
            )
            self.repo.delete("trainer_assignments", assignment["id"])


trainers_bp = Blueprint("trainers", __name__, url_prefix="/api/v1")
trainers_bp.repo = None  # type: ignore[attr-defined]
trainers_bp.publisher = None  # type: ignore[attr-defined]


def configure_trainers(
    repo: Repository, publisher: EventPublisher | None = None
) -> None:
    """Inject dependencies for applications using the module-level blueprint."""
    trainers_bp.repo = repo  # type: ignore[attr-defined]
    trainers_bp.publisher = publisher or NullEventPublisher()  # type: ignore[attr-defined]


def _service() -> TrainerService:
    repo = getattr(trainers_bp, "repo", None)
    publisher = getattr(trainers_bp, "publisher", None)
    if repo is None:
        repo = current_app.extensions.get("repository") or current_app.config.get(
            "REPOSITORY"
        )
    if publisher is None:
        publisher = current_app.extensions.get("publisher") or current_app.config.get(
            "PUBLISHER"
        )
    if repo is None:
        raise RuntimeError("Trainer repository has not been configured.")
    return TrainerService(repo, publisher)


@trainers_bp.get("/trainers")
def get_trainers():
    cursor, limit = cursor_params()
    rows, meta = _service().list_trainers(cursor, limit, request.args.get("status"))
    return success(rows, meta=meta)


@trainers_bp.post("/trainers")
@roles_required("ADMIN", "STAFF")
def post_trainer():
    return success(_service().create_trainer(parse_model(TrainerCreateDTO)), status=201)


@trainers_bp.patch("/trainers/<int:trainer_id>")
@roles_required("ADMIN", "STAFF")
def patch_trainer(trainer_id: int):
    return success(_service().update_trainer(trainer_id, parse_model(TrainerPatchDTO)))


@trainers_bp.post("/trainer-assignments")
@roles_required("ADMIN", "STAFF")
def post_trainer_assignment():
    return success(
        _service().create_assignment(parse_model(TrainerAssignmentCreateDTO)),
        status=201,
    )


@trainers_bp.delete("/trainer-assignments/<int:assignment_id>")
@roles_required("ADMIN", "STAFF")
def delete_trainer_assignment(assignment_id: int):
    _service().delete_assignment(assignment_id)
    return success(None)
