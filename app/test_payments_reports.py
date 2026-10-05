"""Focused coverage for canonical payment and named report behavior."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.core import ConflictError, NotFoundError
from app.payments import (
    PaymentCreateDTO,
    PaymentService,
    ReportService,
    RefundCreateDTO,
)
from app.repository import InMemoryRepository


def _payment_service() -> tuple[PaymentService, InMemoryRepository]:
    repository = InMemoryRepository()
    repository.create("members", {"name": "Ada"})
    return PaymentService(repository), repository


def _report_repository() -> InMemoryRepository:
    repository = InMemoryRepository()
    repository.create(
        "attendance", {"member_id": 1, "check_in_at": datetime(2025, 2, 3, 9, tzinfo=timezone.utc)}
    )
    repository.create(
        "attendance", {"member_id": 2, "check_in_at": datetime(2025, 2, 3, 10, tzinfo=timezone.utc)}
    )
    # Null check-in is not presence and must not appear in attendance buckets.
    repository.create("attendance", {"member_id": 3, "check_in_at": None})
    repository.create(
        "memberships", {"member_id": 1, "status": "ACTIVE", "end_date": date(2025, 2, 10)}
    )
    repository.create(
        "memberships", {"member_id": 2, "status": "EXPIRED", "end_date": date(2025, 2, 11)}
    )
    repository.create(
        "payments",
        {
            "amount": Decimal("12.00"),
            "refunded_amount": Decimal("2.00"),
            "method": "CARD",
            "status": "COMPLETED",
            "created_at": datetime(2025, 2, 3, tzinfo=timezone.utc),
        },
    )
    repository.create(
        "trainer_assignments", {"trainer_id": 7, "member_id": 1, "assigned_at": date(2025, 2, 3)}
    )
    repository.create(
        "members", {"name": "Ada", "status": "ACTIVE", "joining_date": date(2025, 2, 3)}
    )
    repository.create(
        "class_enrollments",
        {"class_id": 9, "member_id": 1, "status": "ENROLLED", "enrolled_at": date(2025, 2, 3)},
    )
    return repository


def test_payment_reference_is_unique_for_in_memory_transactions():
    service, _ = _payment_service()
    payment = PaymentCreateDTO(
        member_id=1, amount=Decimal("10.00"), method="CARD", reference_id="charge-unique"
    )
    service.create(payment)

    with pytest.raises(ConflictError):
        service.create(payment)


def test_partial_refund_remains_completed_and_full_refund_is_refunded():
    service, _ = _payment_service()
    payment = service.create(
        PaymentCreateDTO(member_id=1, amount=Decimal("10.00"), method="CARD", reference_id="charge-1")
    )

    partial = service.refund(payment["id"], RefundCreateDTO(amount=Decimal("4.00")))
    assert partial["status"] == "COMPLETED"
    assert partial["refunded_amount"] == Decimal("4.00")

    complete = service.refund(payment["id"], RefundCreateDTO(amount=Decimal("6.00")))
    assert complete["status"] == "REFUNDED"
    assert complete["refunded_amount"] == Decimal("10.00")


@pytest.mark.parametrize(
    ("report_name", "assertion"),
    [
        ("daily-attendance", lambda summary: summary["buckets"] == [{"date": "2025-02-03", "present_count": 2}]),
        ("monthly-attendance", lambda summary: summary["buckets"] == [{"month": "2025-02", "present_count": 2}]),
        ("active-members", lambda summary: summary["active_members"] == 1),
        ("expiring-memberships", lambda summary: summary["expiring_memberships"] == 1),
        ("expired-memberships", lambda summary: summary["expired_memberships"] == 1),
        ("payment-statistics", lambda summary: summary["net_amount"] == Decimal("10.00")),
        ("trainer-statistics", lambda summary: summary["assignments_by_trainer"] == [{"trainer_id": 7, "assigned_members": 1}]),
        ("member-statistics", lambda summary: summary["member_count"] == 1),
        ("class-enrollment", lambda summary: summary["enrollments_by_class"] == [{"class_id": 9, "enrolled_members": 1}]),
    ],
)
def test_every_named_report_has_a_schema_backed_aggregate(report_name, assertion):
    result = ReportService(_report_repository()).run(
        report_name, date(2025, 2, 1), date(2025, 2, 28)
    )
    assert result["report"] == report_name
    assert assertion(result["summary"])


def test_membership_reports_use_status_and_end_date_range():
    reports = ReportService(_report_repository())
    assert reports.run("active-members", date(2025, 2, 11), date(2025, 2, 11))["summary"]["active_members"] == 0
    assert reports.run("expired-memberships", date(2025, 2, 11), date(2025, 2, 11))["summary"]["expired_memberships"] == 1


def test_unknown_report_is_not_an_alias():
    with pytest.raises(NotFoundError):
        ReportService(_report_repository()).run("revenue", None, None)
