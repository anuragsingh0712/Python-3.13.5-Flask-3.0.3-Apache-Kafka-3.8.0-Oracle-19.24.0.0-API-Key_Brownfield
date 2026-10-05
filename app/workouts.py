"""Workout-plan and exercise resource service and Flask blueprint."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from flask import Blueprint, Response, current_app, g, request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core import DomainError, ForbiddenError, cursor_params, encode_cursor, parse_model, roles_required, success
from app.repository import EventPublisher, NullEventPublisher, Repository

workouts_bp = Blueprint("workouts", __name__, url_prefix="/api/v1")


class WorkoutPlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    member_id: int = Field(gt=0)
    trainer_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)


class WorkoutPlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    member_id: int | None = Field(default=None, gt=0)
    trainer_id: int | None = Field(default=None, gt=0)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def has_change(self) -> "WorkoutPlanUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class WorkoutExerciseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    sets: int | None = Field(default=None, gt=0)
    repetitions: int | None = Field(default=None, gt=0)
    weight: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    duration_seconds: int | None = Field(default=None, gt=0)
    rest_seconds: int | None = Field(default=None, ge=0)
    difficulty: str | None = Field(default=None, max_length=50)


class WorkoutService:
    """Transactional workout-plan operations over the shared repository contract."""

    def __init__(self, repository: Repository, publisher: EventPublisher | None = None) -> None:
        self.repository = repository
        self.publisher = publisher or NullEventPublisher()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _require_reference(self, table: str, identifier: int) -> dict[str, Any]:
        return self.repository.get(table, identifier)

    def get_plan(self, plan_id: int, *, for_update: bool = False) -> dict[str, Any]:
        return self.repository.get("workout_plans", plan_id, for_update=for_update)

    def list_plans(self, member_id: int | None, trainer_id: int | None, cursor: int | None, limit: int) -> tuple[list[dict[str, Any]], str | None]:
        rows = self.repository.list("workout_plans", member_id=member_id, trainer_id=trainer_id)
        rows.sort(key=lambda row: row["id"])
        if cursor is not None:
            rows = [row for row in rows if row["id"] > cursor]
        page = rows[:limit]
        next_cursor = encode_cursor(page[-1]["id"]) if len(rows) > limit and page else None
        return page, next_cursor

    def create_plan(self, values: dict[str, Any]) -> dict[str, Any]:
        with self.repository.transaction():
            self._require_reference("members", values["member_id"])
            self._require_reference("trainers", values["trainer_id"])
            now = self._now()
            plan = self.repository.create("workout_plans", {**values, "created_at": now, "updated_at": now})
        self.publisher.publish_after_commit("gym.workout_plan.created.v1", plan)
        return plan

    def update_plan(self, plan_id: int, values: dict[str, Any]) -> dict[str, Any]:
        with self.repository.transaction():
            self.get_plan(plan_id, for_update=True)
            if "member_id" in values:
                self._require_reference("members", values["member_id"])
            if "trainer_id" in values:
                self._require_reference("trainers", values["trainer_id"])
            return self.repository.update("workout_plans", plan_id, {**values, "updated_at": self._now()})

    def delete_plan(self, plan_id: int) -> None:
        with self.repository.transaction():
            self.get_plan(plan_id, for_update=True)
            # Delete dependants explicitly so the operation is valid with both the
            # in-memory repository and schemas which do not use ON DELETE CASCADE.
            for exercise in self.repository.list("workout_exercises", workout_plan_id=plan_id):
                self.repository.delete("workout_exercises", exercise["id"])
            self.repository.delete("workout_plans", plan_id)

    def create_exercise(self, plan_id: int, values: dict[str, Any]) -> dict[str, Any]:
        with self.repository.transaction():
            self.get_plan(plan_id, for_update=True)
            exercise = self.repository.create("workout_exercises", {"workout_plan_id": plan_id, **values})
        self.publisher.publish_after_commit("gym.workout_exercise.created.v1", exercise)
        return exercise

    def delete_exercise(self, plan_id: int, exercise_id: int) -> None:
        with self.repository.transaction():
            self.get_plan(plan_id, for_update=True)
            exercise = self.repository.get("workout_exercises", exercise_id, for_update=True)
            # An existing exercise under another plan must not be removable via a
            # guessed nested URL.
            if exercise.get("workout_plan_id") != plan_id:
                raise DomainError("not_found", "workout_exercise was not found.", 404)
            self.repository.delete("workout_exercises", exercise_id)


def init_workouts(app: Any, repository: Repository, publisher: EventPublisher | None = None) -> WorkoutService:
    """Install this independently testable resource's service on an application."""
    service = WorkoutService(repository, publisher)
    app.extensions["workout_service"] = service
    return service


def _service() -> WorkoutService:
    service = current_app.extensions.get("workout_service")
    if not isinstance(service, WorkoutService):
        raise RuntimeError("Workout service has not been configured.")
    return service


def _positive_query_id(name: str) -> int | None:
    value = request.args.get(name)
    if value is None:
        return None
    try:
        identifier = int(value)
    except ValueError as error:
        raise DomainError("validation_error", f"{name} is invalid.", 422) from error
    if identifier <= 0:
        raise DomainError("validation_error", f"{name} is invalid.", 422)
    return identifier


def _authorize_plan_mutation(plan: dict[str, Any] | None = None, requested_trainer_id: int | None = None) -> None:
    roles = getattr(g, "roles", frozenset())
    if roles.intersection({"ADMIN", "STAFF"}):
        return
    if "TRAINER" not in roles:
        raise ForbiddenError()
    raw_trainer_id = request.headers.get("X-Trainer-ID")
    try:
        trainer_id = int(raw_trainer_id) if raw_trainer_id is not None else 0
    except ValueError:
        trainer_id = 0
    if trainer_id <= 0:
        raise ForbiddenError()
    # Creation has no plan yet; a trainer may create only a plan assigned to self.
    assigned_id = plan["trainer_id"] if plan is not None else requested_trainer_id
    if assigned_id != trainer_id:
        raise ForbiddenError()


@workouts_bp.get("/workout-plans")
@roles_required("ADMIN", "STAFF", "TRAINER")
def list_workout_plans() -> Response:
    cursor, limit = cursor_params()
    plans, next_cursor = _service().list_plans(_positive_query_id("member_id"), _positive_query_id("trainer_id"), cursor, limit)
    meta: dict[str, Any] = {"limit": limit}
    if next_cursor is not None:
        meta["next_cursor"] = next_cursor
    return success(plans, meta=meta)


@workouts_bp.post("/workout-plans")
@roles_required("ADMIN", "STAFF", "TRAINER")
def create_workout_plan() -> Response:
    payload = parse_model(WorkoutPlanCreate).model_dump()
    _authorize_plan_mutation(requested_trainer_id=payload["trainer_id"])
    return success(_service().create_plan(payload), status=201)


@workouts_bp.patch("/workout-plans/<int:plan_id>")
@roles_required("ADMIN", "STAFF", "TRAINER")
def update_workout_plan(plan_id: int) -> Response:
    service = _service()
    plan = service.get_plan(plan_id)
    payload = parse_model(WorkoutPlanUpdate).model_dump(exclude_unset=True)
    _authorize_plan_mutation(plan)
    # A trainer cannot transfer the plan to a different trainer after passing the
    # ownership check on its original assignment.
    if "TRAINER" in getattr(g, "roles", frozenset()) and not getattr(g, "roles", frozenset()).intersection({"ADMIN", "STAFF"}):
        _authorize_plan_mutation(requested_trainer_id=payload.get("trainer_id", plan["trainer_id"]))
    return success(service.update_plan(plan_id, payload))


@workouts_bp.delete("/workout-plans/<int:plan_id>")
@roles_required("ADMIN", "STAFF", "TRAINER")
def delete_workout_plan(plan_id: int) -> Response:
    service = _service()
    _authorize_plan_mutation(service.get_plan(plan_id))
    service.delete_plan(plan_id)
    return Response(status=204)


@workouts_bp.post("/workout-plans/<int:plan_id>/exercises")
@roles_required("ADMIN", "STAFF", "TRAINER")
def create_workout_exercise(plan_id: int) -> Response:
    service = _service()
    _authorize_plan_mutation(service.get_plan(plan_id))
    payload = parse_model(WorkoutExerciseCreate).model_dump()
    return success(service.create_exercise(plan_id, payload), status=201)


@workouts_bp.delete("/workout-plans/<int:plan_id>/exercises/<int:exercise_id>")
@roles_required("ADMIN", "STAFF", "TRAINER")
def delete_workout_exercise(plan_id: int, exercise_id: int) -> Response:
    service = _service()
    _authorize_plan_mutation(service.get_plan(plan_id))
    service.delete_exercise(plan_id, exercise_id)
    return Response(status=204)
