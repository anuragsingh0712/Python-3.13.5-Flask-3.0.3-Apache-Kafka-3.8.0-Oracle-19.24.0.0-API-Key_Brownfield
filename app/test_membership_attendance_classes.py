"""Focused canonical-schema and enrollment eligibility tests."""
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.attendance import AttendanceService, CheckInDTO
from app.classes import ClassCreateDTO, ClassesService, EnrollmentCreateDTO
from app.core import ConflictError
from app.memberships import MembershipCreateDTO, MembershipService, PlanCreateDTO
from app.repository import CapturingPublisher, InMemoryRepository


def test_membership_and_attendance_use_canonical_fields_only():
    repo = InMemoryRepository()
    publisher = CapturingPublisher()
    repo.create("members", {"status": "ACTIVE"})
    memberships = MembershipService(repo, publisher)

    plan = memberships.create_plan(PlanCreateDTO(name="Monthly", price="30.00", visit_limit=2, duration_days=30, is_active=True))
    membership = memberships.create_membership(MembershipCreateDTO(member_id=1, plan_id=plan["id"], start_date=date.today()))
    assert {"start_date", "end_date", "price_paid", "remaining_visits"} <= membership.keys()
    assert not {"starts_on", "ends_on", "price_snapshot", "visit_limit_snapshot"} & membership.keys()
    assert publisher.events[-1][1]["start_date"] == date.today()

    attendance = AttendanceService(repo, publisher).check_in(CheckInDTO(member_id=1, check_in_at=datetime.now(timezone.utc)))
    assert {"check_in_at", "check_out_at"} <= attendance.keys()
    assert "checked_in_at" not in attendance and "checked_out_at" not in attendance
    assert repo.get("memberships", membership["id"])["remaining_visits"] == 1
    with pytest.raises(ValidationError):
        CheckInDTO.model_validate({"member_id": 1, "checked_in_at": datetime.now(timezone.utc).isoformat()})


def test_enrollment_rejects_exhausted_membership_and_uses_class_id():
    repo = InMemoryRepository()
    publisher = CapturingPublisher()
    repo.create("members", {"status": "ACTIVE"})
    membership = repo.create("memberships", {
        "member_id": 1, "status": "ACTIVE", "start_date": date.today() - timedelta(days=1),
        "end_date": date.today() + timedelta(days=1), "remaining_visits": 0,
    })
    service = ClassesService(repo, publisher)
    gym_class = service.create_class(ClassCreateDTO(
        name="Yoga", trainer_id=1, scheduled_at=datetime.now(timezone.utc), capacity=2,
    ))

    with pytest.raises(ConflictError, match="usable membership"):
        service.enroll(gym_class["id"], EnrollmentCreateDTO(member_id=1))
    assert repo.list("class_enrollments") == []

    repo.update("memberships", membership["id"], {"remaining_visits": 1})
    enrollment = service.enroll(gym_class["id"], EnrollmentCreateDTO(member_id=1))
    assert enrollment["class_id"] == gym_class["id"]
    assert "gym_class_id" not in enrollment
    assert repo.get("memberships", membership["id"])["remaining_visits"] == 0
    assert publisher.events[-1][1]["enrollment"]["class_id"] == gym_class["id"]
