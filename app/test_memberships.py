"""Focused membership plan and lifecycle snapshot regression tests."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core import ConflictError
from app.memberships import (
    MembershipCreateDTO,
    MembershipService,
    PlanCreateDTO,
    PlanPatchDTO,
)
from app.repository import CapturingPublisher, InMemoryRepository


def _service() -> tuple[InMemoryRepository, CapturingPublisher, MembershipService]:
    repo = InMemoryRepository()
    publisher = CapturingPublisher()
    repo.create("members", {"status": "ACTIVE"})
    return repo, publisher, MembershipService(repo, publisher)


def test_free_unlimited_plan_snapshots_null_visits_and_patch_preserves_null():
    repo, _, service = _service()
    plan = service.create_plan(
        PlanCreateDTO(name="Free access", price="0.00", duration_days=31)
    )
    assert plan["price"] == Decimal("0.00")
    assert plan["visit_limit"] is None

    membership = service.create_membership(
        MembershipCreateDTO(member_id=1, plan_id=plan["id"], start_date=date.today())
    )
    assert membership["price_paid"] == Decimal("0.00")
    assert membership["remaining_visits"] is None
    assert service.is_usable(membership)

    # Omission makes no change; explicit null is retained as unlimited.
    unchanged = service.patch_plan(plan["id"], PlanPatchDTO(name="Free access"))
    assert unchanged["visit_limit"] is None
    finite = service.patch_plan(plan["id"], PlanPatchDTO(visit_limit=4))
    assert finite["visit_limit"] == 4
    unlimited = service.patch_plan(plan["id"], PlanPatchDTO(visit_limit=None))
    assert unlimited["visit_limit"] is None
    assert repo.get("membership_plans", plan["id"])["visit_limit"] is None

    with pytest.raises(ValidationError):
        PlanCreateDTO(name="Invalid", price="-0.01", duration_days=30)


def test_renew_creates_new_snapshot_and_keeps_historical_terms_immutable():
    repo, publisher, service = _service()
    plan = service.create_plan(
        PlanCreateDTO(
            name="Monthly", price="30.00", visit_limit=10, duration_days=30
        )
    )
    original_start = date.today() - timedelta(days=10)
    original = service.create_membership(
        MembershipCreateDTO(member_id=1, plan_id=plan["id"], start_date=original_start)
    )
    repo.update("memberships", original["id"], {"remaining_visits": 6})
    service.patch_plan(
        plan["id"], PlanPatchDTO(price="45.00", visit_limit=20, duration_days=60)
    )

    renewed = service.action(original["id"], "renew", date.today())
    historical = repo.get("memberships", original["id"])

    assert renewed["id"] != original["id"]
    assert renewed["status"] == "ACTIVE"
    assert renewed["price_paid"] == Decimal("45.00")
    assert renewed["remaining_visits"] == 20
    assert renewed["end_date"] == date.today() + timedelta(days=59)
    assert historical == {
        **original,
        "status": "EXPIRED",
        "remaining_visits": 6,
    }
    assert publisher.events[-1] == ("gym.membership.renewed.v1", renewed)


def test_renew_rejects_a_second_active_lifecycle_without_changing_history():
    repo, _, service = _service()
    plan = service.create_plan(
        PlanCreateDTO(name="Monthly", price="30.00", visit_limit=10, duration_days=30)
    )
    original = service.create_membership(MembershipCreateDTO(member_id=1, plan_id=plan["id"]))
    other = repo.create(
        "memberships",
        {
            "member_id": 1,
            "plan_id": plan["id"],
            "status": "ACTIVE",
            "price_paid": Decimal("30.00"),
            "remaining_visits": 10,
            "start_date": date.today(),
            "end_date": date.today() + timedelta(days=29),
        },
    )

    with pytest.raises(ConflictError, match="already has an active"):
        service.action(original["id"], "renew")
    assert repo.get("memberships", original["id"])["status"] == "ACTIVE"
    assert repo.get("memberships", other["id"])["status"] == "ACTIVE"
