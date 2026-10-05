"""Regression coverage for canonical payment and report schema names."""
from datetime import datetime, timezone
from decimal import Decimal

from app.payments import PaymentCreateDTO, PaymentService, ReportService, RefundCreateDTO
from app.repository import InMemoryRepository


def _payment_service() -> tuple[PaymentService, InMemoryRepository]:
    repository = InMemoryRepository()
    repository.create("members", {"name": "Ada"})
    return PaymentService(repository), repository


def test_partial_refund_remains_completed_and_full_refund_is_refunded():
    service, _ = _payment_service()
    payment = service.create(PaymentCreateDTO(
        member_id=1, amount=Decimal("10.00"), method="CARD", reference_id="charge-1"
    ))

    partial = service.refund(payment["id"], RefundCreateDTO(amount=Decimal("4.00")))
    assert partial["status"] == "COMPLETED"
    assert partial["refunded_amount"] == Decimal("4.00")
    assert set(partial).isdisjoint({"last_refund_amount", "last_refund_reason", "refunded_at"})

    complete = service.refund(payment["id"], RefundCreateDTO(amount=Decimal("6.00")))
    assert complete["status"] == "REFUNDED"
    assert complete["refunded_amount"] == Decimal("10.00")


def test_reports_filter_using_each_canonical_date_column():
    repository = InMemoryRepository()
    repository.create("payments", {
        "amount": Decimal("12.00"), "refunded_amount": Decimal("0"),
        "created_at": datetime(2025, 2, 3, tzinfo=timezone.utc),
        # A legacy date must not influence the revenue report filter.
        "paid_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
    })
    repository.create("attendance", {"check_in_at": datetime(2025, 2, 4, tzinfo=timezone.utc)})
    repository.create("gym_classes", {"scheduled_at": datetime(2025, 2, 5, tzinfo=timezone.utc)})
    repository.create("trainer_assignments", {"assigned_at": datetime(2025, 2, 6, tzinfo=timezone.utc)})
    reports = ReportService(repository)

    assert reports.run("revenue", datetime(2025, 2, 3).date(), datetime(2025, 2, 3).date())["summary"]["count"] == 1
    assert reports.run("attendance", datetime(2025, 2, 4).date(), datetime(2025, 2, 4).date())["summary"]["count"] == 1
    assert reports.run("classes", datetime(2025, 2, 5).date(), datetime(2025, 2, 5).date())["summary"]["count"] == 1
    assert reports.run("trainer-member", datetime(2025, 2, 6).date(), datetime(2025, 2, 6).date())["summary"]["count"] == 1
