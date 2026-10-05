"""Membership plans and memberships resource group."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from flask import Blueprint, current_app, request
from pydantic import BaseModel, ConfigDict, Field

from app.core import ConflictError, DomainError, NotFoundError, cursor_params, encode_cursor, parse_model, roles_required, success
from app.repository import EventPublisher, NullEventPublisher, Repository

PLAN_TABLE = "membership_plans"
MEMBERSHIP_TABLE = "memberships"
MEMBER_TABLE = "members"
ACTIVE = "ACTIVE"
MembershipStatus = Literal["ACTIVE", "CANCELLED", "SUSPENDED", "EXPIRED"]


class PlanCreateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    visit_limit: int = Field(gt=0, le=100000)
    duration_days: int = Field(gt=0, le=3650)
    is_active: bool = True


class PlanPatchDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    visit_limit: int | None = Field(default=None, gt=0, le=100000)
    duration_days: int | None = Field(default=None, gt=0, le=3650)
    is_active: bool | None = None


class MembershipCreateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_id: int = Field(gt=0)
    plan_id: int = Field(gt=0)
    start_date: date | None = None


class MembershipActionDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date | None = None


MembershipPlanCreateDTO = PlanCreateDTO
MembershipPlanPatchDTO = PlanPatchDTO


def _page(rows: list[dict[str, Any]], cursor: int | None, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = sorted(rows, key=lambda row: int(row["id"]))
    if cursor is not None:
        rows = [row for row in rows if int(row["id"]) > cursor]
    selected, more = rows[:limit], len(rows) > limit
    meta: dict[str, Any] = {"limit": limit}
    if more and selected:
        meta["next_cursor"] = encode_cursor(int(selected[-1]["id"]))
    return selected, meta


def _active_member(member: dict[str, Any]) -> bool:
    if "status" in member and str(member["status"]).upper() != ACTIVE:
        return False
    return member.get("is_active", member.get("active", True)) is not False


def _plan_terms(plan: dict[str, Any]) -> dict[str, Any]:
    if not plan.get("is_active", True):
        raise DomainError("invalid_plan", "Membership plan is not active.", 422)
    return plan


class MembershipService:
    def __init__(self, repo: Repository, publisher: EventPublisher | None = None) -> None:
        self.repo = repo
        self.publisher = publisher or NullEventPublisher()

    def _publish(self, action: str, membership: dict[str, Any]) -> None:
        self.publisher.publish_after_commit(f"gym.membership.{action}.v1", membership)

    def list_plans(self, cursor: int | None, limit: int, is_active: bool | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return _page(self.repo.list(PLAN_TABLE, is_active=is_active), cursor, limit)

    def create_plan(self, dto: PlanCreateDTO) -> dict[str, Any]:
        with self.repo.transaction():
            if any(p.get("name", "").casefold() == dto.name.casefold() for p in self.repo.list(PLAN_TABLE)):
                raise ConflictError("A membership plan with this name already exists.")
            event = self.repo.create(PLAN_TABLE, dto.model_dump())
        self.publisher.publish_after_commit("gym.membership.plan.created.v1", event)
        return event

    def patch_plan(self, plan_id: int, dto: PlanPatchDTO) -> dict[str, Any]:
        values = dto.model_dump(exclude_none=True)
        if not values:
            raise DomainError("validation_error", "At least one field must be supplied.", 422)
        with self.repo.transaction():
            self.repo.get(PLAN_TABLE, plan_id, for_update=True)
            if "name" in values and any(p["id"] != plan_id and p.get("name", "").casefold() == values["name"].casefold() for p in self.repo.list(PLAN_TABLE)):
                raise ConflictError("A membership plan with this name already exists.")
            event = self.repo.update(PLAN_TABLE, plan_id, values)
        self.publisher.publish_after_commit("gym.membership.plan.updated.v1", event)
        return event

    def list_memberships(self, cursor: int | None, limit: int, member_id: int | None = None, status: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if status is not None and status.upper() not in MembershipStatus.__args__:
            raise DomainError("validation_error", "status is invalid.", 422)
        rows = self.repo.list(MEMBERSHIP_TABLE, member_id=member_id, status=status.upper() if status else None)
        for row in rows:
            row["usable"] = self.is_usable(row)
        return _page(rows, cursor, limit)

    @staticmethod
    def is_usable(membership: dict[str, Any], on_date: date | None = None) -> bool:
        today = on_date or date.today()
        start, end = membership.get("start_date"), membership.get("end_date")
        if isinstance(start, str):
            start = date.fromisoformat(start)
        if isinstance(end, str):
            end = date.fromisoformat(end)
        remaining = membership.get("remaining_visits")
        return membership.get("status") == ACTIVE and start is not None and end is not None and start <= today <= end and (remaining is None or int(remaining) > 0)

    def create_membership(self, dto: MembershipCreateDTO) -> dict[str, Any]:
        start = dto.start_date or date.today()
        with self.repo.transaction():
            member = self.repo.get(MEMBER_TABLE, dto.member_id, for_update=True)
            if not _active_member(member):
                raise DomainError("inactive_member", "Member must be active.", 422)
            plan = _plan_terms(self.repo.get(PLAN_TABLE, dto.plan_id, for_update=True))
            if self.repo.list(MEMBERSHIP_TABLE, member_id=dto.member_id, status=ACTIVE):
                raise ConflictError("Member already has an active membership.")
            event = self.repo.create(MEMBERSHIP_TABLE, self._membership_values(dto.member_id, plan, start))
        self._publish("created", event)
        return event

    @staticmethod
    def _membership_values(member_id: int, plan: dict[str, Any], start_date: date) -> dict[str, Any]:
        visits = int(plan["visit_limit"])
        return {"member_id": member_id, "plan_id": plan["id"], "status": ACTIVE,
                "price_paid": plan["price"], "remaining_visits": visits,
                "start_date": start_date, "end_date": start_date + timedelta(days=int(plan["duration_days"]) - 1)}

    def action(self, membership_id: int, action: str, start_date: date | None = None) -> dict[str, Any]:
        action = action.lower()
        if action not in {"renew", "cancel", "suspend", "expire"}:
            raise NotFoundError("membership action")
        with self.repo.transaction():
            membership = self.repo.get(MEMBERSHIP_TABLE, membership_id, for_update=True)
            if action == "renew":
                member = self.repo.get(MEMBER_TABLE, membership["member_id"], for_update=True)
                if not _active_member(member):
                    raise DomainError("inactive_member", "Member must be active.", 422)
                conflicts = [m for m in self.repo.list(MEMBERSHIP_TABLE, member_id=membership["member_id"], status=ACTIVE) if m["id"] != membership_id]
                if conflicts:
                    raise ConflictError("Member already has an active membership.")
                plan = _plan_terms(self.repo.get(PLAN_TABLE, membership["plan_id"], for_update=True))
                event = self.repo.update(MEMBERSHIP_TABLE, membership_id, self._membership_values(membership["member_id"], plan, start_date or date.today()))
            else:
                if membership["status"] != ACTIVE:
                    raise ConflictError("Only an active membership can be changed this way.")
                event = self.repo.update(MEMBERSHIP_TABLE, membership_id, {"status": {"cancel": "CANCELLED", "suspend": "SUSPENDED", "expire": "EXPIRED"}[action]})
        self._publish(action + "ed" if action == "expire" else action + "led" if action == "cancel" else action + "ed", event)
        return event


memberships_bp = Blueprint("memberships", __name__)


def configure_memberships(repository: Repository, publisher: EventPublisher) -> MembershipService:
    service = MembershipService(repository, publisher)
    memberships_bp.memberships_service = service  # type: ignore[attr-defined]
    return service


def _service() -> MembershipService:
    service = current_app.extensions.get("memberships_service") or current_app.extensions.get("membership_service")
    if service is None:
        service = getattr(memberships_bp, "memberships_service", None)
    if not isinstance(service, MembershipService):
        raise RuntimeError("Membership service has not been configured.")
    return service


@memberships_bp.get("/membership-plans")
@roles_required("ADMIN", "STAFF", "TRAINER")
def get_plans():
    cursor, limit = cursor_params()
    active_raw = request.args.get("is_active")
    if active_raw is not None and active_raw.lower() not in {"true", "false"}:
        raise DomainError("validation_error", "is_active is invalid.", 422)
    rows, meta = _service().list_plans(cursor, limit, None if active_raw is None else active_raw.lower() == "true")
    return success(rows, meta=meta)


@memberships_bp.post("/membership-plans")
@roles_required("ADMIN", "STAFF")
def post_plan():
    return success(_service().create_plan(parse_model(PlanCreateDTO)), status=201)


@memberships_bp.patch("/membership-plans/<int:plan_id>")
@roles_required("ADMIN", "STAFF")
def patch_plan(plan_id: int):
    return success(_service().patch_plan(plan_id, parse_model(PlanPatchDTO)))


@memberships_bp.get("/memberships")
@roles_required("ADMIN", "STAFF", "TRAINER")
def get_memberships():
    cursor, limit = cursor_params()
    try:
        member_id = int(request.args["member_id"]) if "member_id" in request.args else None
        if member_id is not None and member_id < 1:
            raise ValueError
    except ValueError:
        raise DomainError("validation_error", "member_id is invalid.", 422) from None
    rows, meta = _service().list_memberships(cursor, limit, member_id, request.args.get("status"))
    return success(rows, meta=meta)


@memberships_bp.post("/memberships")
@roles_required("ADMIN", "STAFF")
def post_membership():
    return success(_service().create_membership(parse_model(MembershipCreateDTO)), status=201)


@memberships_bp.post("/memberships/<int:membership_id>/<action>")
@roles_required("ADMIN", "STAFF")
def membership_action(membership_id: int, action: str):
    body = request.get_json(silent=True) or {}
    try:
        dto = MembershipActionDTO.model_validate(body)
    except Exception as error:
        raise DomainError("validation_error", "Request data is invalid.", 422) from error
    return success(_service().action(membership_id, action, dto.start_date))
