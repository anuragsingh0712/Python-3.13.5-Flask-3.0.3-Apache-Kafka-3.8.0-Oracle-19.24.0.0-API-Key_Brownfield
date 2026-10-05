"""Focused verifier coverage using the public Flask and in-memory seams.

The Oracle test is deliberately opt-in: it exercises the actual migration and a
real row lock only when a disposable Oracle instance is explicitly configured.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app import create_app
from app.attendance import AttendanceService, CheckInDTO
from app.core import ConflictError, Settings
from app.repository import CapturingPublisher, InMemoryRepository


SETTINGS = Settings(
    {
        "admin-key": frozenset({"ADMIN"}),
        "staff-key": frozenset({"STAFF"}),
        "trainer-key": frozenset({"TRAINER"}),
    },
    "",
    "",
    "",
    "",
    "",
)
ADMIN = {"X-API-Key": "admin-key"}
TRAINER = {"X-API-Key": "trainer-key", "X-Trainer-ID": "1"}
NOW = datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def api():
    repo = InMemoryRepository()
    return create_app(
        repository=repo, publisher=CapturingPublisher(), settings=SETTINGS
    ).test_client(), repo


def member_payload(number: int = 1) -> dict[str, str]:
    return {
        "name": f"Member {number}",
        "email": f"member{number}@example.test",
        "phone": f"+1555000{number:04d}",
        "joining_date": "2025-01-01",
    }


def seed_member(repo: InMemoryRepository, identifier: int = 1) -> None:
    # Resource services only require ACTIVE status for the cross-resource tests.
    while len(repo.list("members")) < identifier:
        repo.create("members", {"status": "ACTIVE", "name": "Seed"})


def seed_membership(
    repo: InMemoryRepository, member_id: int, visits: int | None = 3
) -> int:
    return repo.create(
        "memberships",
        {
            "member_id": member_id,
            "status": "ACTIVE",
            # This fixture serves both check-ins at NOW and class enrollment,
            # whose eligibility check uses the current date.  Cover both dates
            # rather than relying on a historical fixed membership period.
            "start_date": min(NOW.date(), date.today()) - timedelta(days=1),
            "end_date": max(NOW.date(), date.today()) + timedelta(days=5),
            "remaining_visits": visits,
        },
    )["id"]


def test_member_membership_and_plan_endpoints_include_renewal_history(api):
    client, _ = api
    member = client.post("/api/v1/members", json=member_payload(), headers=ADMIN)
    assert member.status_code == 201
    assert client.get("/api/v1/members", headers=ADMIN).status_code == 200
    assert client.get("/api/v1/members/1", headers=ADMIN).status_code == 200
    assert (
        client.patch(
            "/api/v1/members/1", json={"address": "1 Main"}, headers=ADMIN
        ).status_code
        == 200
    )

    free_plan = client.post(
        "/api/v1/membership-plans",
        json={
            "name": "Free unlimited",
            "price": "0.00",
            "visit_limit": None,
            "duration_days": 30,
        },
        headers=ADMIN,
    )
    assert free_plan.status_code == 201
    assert (
        client.get("/api/v1/membership-plans?is_active=true", headers=ADMIN).status_code
        == 200
    )
    assert (
        client.patch(
            "/api/v1/membership-plans/1", json={"price": "0.00"}, headers=ADMIN
        ).status_code
        == 200
    )
    membership = client.post(
        "/api/v1/memberships",
        json={"member_id": 1, "plan_id": 1, "start_date": "2025-01-01"},
        headers=ADMIN,
    )
    assert membership.status_code == 201
    original = membership.get_json()["data"]
    renewed = client.post(
        "/api/v1/memberships/1/renew", json={"start_date": "2025-02-01"}, headers=ADMIN
    )
    assert renewed.status_code == 200
    assert renewed.get_json()["data"]["id"] != original["id"]
    history = client.get("/api/v1/memberships?member_id=1", headers=ADMIN).get_json()[
        "data"
    ]
    assert [row["status"] for row in history] == ["EXPIRED", "ACTIVE"]
    assert history[0]["price_paid"] == "0.00" and history[0]["remaining_visits"] is None


def test_unlimited_membership_never_consumes_visits_and_attendance_filters(api):
    client, repo = api
    seed_member(repo)
    membership_id = seed_membership(repo, 1, None)
    checked_in = client.post(
        "/api/v1/attendance/check-in",
        json={"member_id": 1, "check_in_at": NOW.isoformat()},
        headers=ADMIN,
    )
    assert checked_in.status_code == 201
    assert repo.get("memberships", membership_id)["remaining_visits"] is None
    attendance_id = checked_in.get_json()["data"]["id"]
    assert (
        client.post(
            f"/api/v1/attendance/{attendance_id}/check-out",
            json={"check_out_at": (NOW + timedelta(minutes=45)).isoformat()},
            headers=ADMIN,
        ).status_code
        == 200
    )
    listed = client.get(
        "/api/v1/attendance?from_date=2025-01-15&to_date=2025-01-15", headers=ADMIN
    )
    assert listed.status_code == 200 and [
        x["id"] for x in listed.get_json()["data"]
    ] == [attendance_id]
    assert (
        client.get(
            "/api/v1/attendance?from_date=2025-01-16&to_date=2025-01-15", headers=ADMIN
        ).status_code
        == 422
    )


def test_duplicate_and_racing_checkins_have_one_winner():
    repo, publisher = InMemoryRepository(), CapturingPublisher()
    seed_member(repo)
    membership_id = seed_membership(repo, 1, 1)
    service = AttendanceService(repo, publisher)

    def check_in():
        try:
            return service.check_in(CheckInDTO(member_id=1, check_in_at=NOW))["id"]
        except ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: check_in(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert repo.get("memberships", membership_id)["remaining_visits"] == 0
    assert len(repo.list("attendance")) == 1


def test_trainer_on_leave_rejected_and_null_capacity_is_unlimited(api):
    client, repo = api
    seed_member(repo, 2)
    leave = client.post(
        "/api/v1/trainers",
        json={
            "name": "Leave",
            "email": "leave@test",
            "phone": "111",
            "status": "ON_LEAVE",
        },
        headers=ADMIN,
    )
    unlimited = client.post(
        "/api/v1/trainers",
        json={
            "name": "Unlimited",
            "email": "unlimited@test",
            "phone": "222",
            "max_members": None,
        },
        headers=ADMIN,
    )
    assert leave.status_code == unlimited.status_code == 201
    assert (
        client.get("/api/v1/trainers?status=ON_LEAVE", headers=ADMIN).status_code == 200
    )
    assert (
        client.patch(
            "/api/v1/trainers/1", json={"specialization": "Recovery"}, headers=ADMIN
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/trainer-assignments",
            json={"trainer_id": 1, "member_id": 1},
            headers=ADMIN,
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/trainer-assignments",
            json={"trainer_id": 2, "member_id": 1},
            headers=ADMIN,
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/trainer-assignments",
            json={"trainer_id": 2, "member_id": 2},
            headers=ADMIN,
        ).status_code
        == 201
    )
    assert (
        client.delete("/api/v1/trainer-assignments/1", headers=ADMIN).status_code == 200
    )


def test_workout_owner_and_all_nested_endpoints(api):
    client, repo = api
    seed_member(repo)
    repo.create("trainers", {"status": "ACTIVE"})
    repo.create("trainers", {"status": "ACTIVE"})
    created = client.post(
        "/api/v1/workout-plans",
        json={"member_id": 1, "trainer_id": 1, "name": "Plan"},
        headers=TRAINER,
    )
    assert created.status_code == 201
    assert (
        client.get("/api/v1/workout-plans?member_id=1", headers=ADMIN).status_code
        == 200
    )
    assert (
        client.patch(
            "/api/v1/workout-plans/1",
            json={"name": "Nope"},
            headers={"X-API-Key": "trainer-key", "X-Trainer-ID": "2"},
        ).status_code
        == 403
    )
    assert (
        client.patch(
            "/api/v1/workout-plans/1", json={"name": "Updated"}, headers=TRAINER
        ).status_code
        == 200
    )
    exercise = client.post(
        "/api/v1/workout-plans/1/exercises",
        json={"name": "Squat", "sets": 3},
        headers=TRAINER,
    )
    assert exercise.status_code == 201
    assert (
        client.delete(
            "/api/v1/workout-plans/1/exercises/1", headers=TRAINER
        ).status_code
        == 204
    )
    assert client.delete("/api/v1/workout-plans/1", headers=TRAINER).status_code == 204


def test_class_capacity_duplicate_and_cancellation_endpoints(api):
    client, repo = api
    seed_member(repo, 2)
    seed_membership(repo, 1, 2)
    seed_membership(repo, 2, 2)
    gym_class = client.post(
        "/api/v1/classes",
        json={
            "name": "Yoga",
            "trainer_id": 1,
            "scheduled_at": NOW.isoformat(),
            "capacity": 1,
        },
        headers=ADMIN,
    )
    assert gym_class.status_code == 201
    assert client.get("/api/v1/classes", headers=ADMIN).status_code == 200
    assert client.get("/api/v1/classes/1", headers=ADMIN).status_code == 200
    assert (
        client.post(
            "/api/v1/classes/1/enrollments", json={"member_id": 1}, headers=ADMIN
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/classes/1/enrollments", json={"member_id": 1}, headers=ADMIN
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/classes/1/enrollments", json={"member_id": 2}, headers=ADMIN
        ).status_code
        == 409
    )
    assert (
        client.delete("/api/v1/classes/1/enrollments/1", headers=ADMIN).status_code
        == 200
    )
    assert (
        client.patch(
            "/api/v1/classes/1", json={"status": "CANCELLED"}, headers=ADMIN
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/classes/1/enrollments", json={"member_id": 2}, headers=ADMIN
        ).status_code
        == 409
    )


def test_payment_reference_duplicate_and_refund_state_transitions(api):
    client, repo = api
    seed_member(repo)
    body = {
        "member_id": 1,
        "amount": "10.00",
        "method": "CARD",
        "reference_id": "ref-1",
    }
    payment = client.post("/api/v1/payments", json=body, headers=ADMIN)
    assert (
        payment.status_code == 201
        and client.get("/api/v1/payments?member_id=1", headers=ADMIN).status_code == 200
    )
    # A duplicate external payment reference must be rejected by the resource,
    # including when tests use the in-memory repository rather than Oracle DDL.
    assert client.post("/api/v1/payments", json=body, headers=ADMIN).status_code == 409
    assert (
        client.post(
            "/api/v1/payments/1/refunds", json={"amount": "4.00"}, headers=ADMIN
        ).get_json()["data"]["status"]
        == "COMPLETED"
    )
    assert (
        client.post(
            "/api/v1/payments/1/refunds", json={"amount": "7.00"}, headers=ADMIN
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/payments/1/refunds", json={"amount": "6.00"}, headers=ADMIN
        ).get_json()["data"]["status"]
        == "REFUNDED"
    )
    assert (
        client.post(
            "/api/v1/payments/1/refunds", json={"amount": "0.01"}, headers=ADMIN
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "report_name",
    [
        "daily-attendance",
        "monthly-attendance",
        "active-members",
        "expiring-memberships",
        "expired-memberships",
        "payment-statistics",
        "trainer-statistics",
        "member-statistics",
        "class-enrollment",
    ],
)
def test_every_named_report_endpoint_is_available(api, report_name):
    client, repo = api
    # Seed the canonical date column for every report table, avoiding fake status fields.
    repo.create("attendance", {"check_in_at": NOW})
    repo.create(
        "memberships",
        {"member_id": 1, "status": "ACTIVE", "end_date": date(2025, 1, 15)},
    )
    repo.create(
        "payments",
        {
            "created_at": NOW,
            "amount": Decimal("2"),
            "refunded_amount": Decimal("0"),
            "status": "COMPLETED",
            "method": "CARD",
        },
    )
    repo.create("trainer_assignments", {"trainer_id": 1, "assigned_at": NOW})
    repo.create("members", {"joining_date": date(2025, 1, 15), "status": "ACTIVE"})
    repo.create(
        "class_enrollments", {"class_id": 1, "enrolled_at": NOW, "status": "ENROLLED"}
    )
    response = client.get(
        f"/api/v1/reports/{report_name}?start_date=2025-01-01&end_date=2025-01-31",
        headers=ADMIN,
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["report"] == report_name


@pytest.mark.integration
def test_oracle_migration_constraints_and_real_row_locking_are_opt_in():
    """Run only against an explicitly supplied disposable Oracle database, never a fake."""
    if os.getenv("RUN_ORACLE_INTEGRATION") != "1":
        pytest.skip("set RUN_ORACLE_INTEGRATION=1 with ORACLE_DSN and ORACLE_PASSWORD")
    if not os.getenv("ORACLE_DSN") or not os.getenv("ORACLE_PASSWORD"):
        pytest.skip(
            "ORACLE_DSN and ORACLE_PASSWORD are required for real Oracle integration"
        )
    oracledb = pytest.importorskip("oracledb")
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, str(root / "scripts" / "init_schema.py")],
        cwd=root,
        env=os.environ.copy(),
        check=True,
    )
    connect = dict(
        user=os.getenv("ORACLE_USERNAME", "gym"),
        password=os.environ["ORACLE_PASSWORD"],
        dsn=os.environ["ORACLE_DSN"],
    )
    first, second = oracledb.connect(**connect), oracledb.connect(**connect)
    try:
        cursor = first.cursor()
        cursor.execute(
            "SELECT constraint_name FROM user_constraints WHERE table_name = 'PAYMENTS' AND constraint_type IN ('P', 'U')"
        )
        assert cursor.fetchall(), (
            "migration must create payment primary/unique constraints"
        )
        # Use a test-owned plan row so this is an actual competing Oracle row lock,
        # rather than a driver-shaped fake or an empty SELECT FOR UPDATE.
        marker = f"pytest-lock-{os.getpid()}"
        returned_id = cursor.var(int)
        cursor.execute(
            "INSERT INTO membership_plans (name, duration_days, price, visit_limit, is_active) "
            "VALUES (:name, 1, 0, NULL, 1) RETURNING id INTO :id",
            {"name": marker, "id": returned_id},
        )
        plan_id = int(returned_id.getvalue()[0])
        first.commit()
        cursor.execute(
            "SELECT id FROM membership_plans WHERE id = :id FOR UPDATE", {"id": plan_id}
        )
        contender = second.cursor()
        try:
            with pytest.raises(oracledb.DatabaseError, match="ORA-00054"):
                contender.execute(
                    "SELECT id FROM membership_plans WHERE id = :id FOR UPDATE NOWAIT",
                    {"id": plan_id},
                )
        finally:
            contender.close()
            first.rollback()
        cursor.execute("DELETE FROM membership_plans WHERE id = :id", {"id": plan_id})
        first.commit()
        cursor.close()
    finally:
        first.close()
        second.close()
