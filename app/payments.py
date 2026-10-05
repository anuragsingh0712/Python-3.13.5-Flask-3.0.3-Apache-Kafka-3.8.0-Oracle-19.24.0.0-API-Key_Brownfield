"""Payments, refunds, and safe named operational reports."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping

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

payments_bp = Blueprint("payments", __name__, url_prefix="/api/v1")
reports_bp = Blueprint("reports", __name__, url_prefix="/api/v1")
PAYMENT_STATUSES = frozenset({"PENDING", "COMPLETED", "FAILED", "REFUNDED"})


class PaymentCreateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_id: int = Field(gt=0)
    membership_id: int | None = Field(default=None, gt=0)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    method: Literal["CASH", "CARD", "UPI", "BANK_TRANSFER", "OTHER"]
    reference_id: str = Field(min_length=1, max_length=200)
    created_at: datetime | None = None
    status: Literal["PENDING", "COMPLETED", "FAILED", "REFUNDED"] = "COMPLETED"

    @field_validator("method", mode="before")
    @classmethod
    def normalize_method(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("reference_id")
    @classmethod
    def normalize_reference(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reference_id must not be blank")
        return value


class RefundCreateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    reason: str | None = Field(default=None, max_length=500)


def _decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as error:
        raise DomainError(
            "invalid_payment", f"Stored {field} is invalid.", 409
        ) from error


class PaymentService:
    def __init__(
        self, repository: Repository, publisher: EventPublisher | None = None
    ) -> None:
        self.repository = repository
        self.publisher = publisher or NullEventPublisher()

    def create(self, payload: PaymentCreateDTO) -> dict[str, Any]:
        # The Oracle schema has a unique constraint, but enforcing the same invariant
        # inside this transaction keeps injected repositories faithful to production.
        # The database constraint remains the concurrent-writer backstop.
        with self.repository.transaction():
            if self.repository.list("payments", reference_id=payload.reference_id):
                raise ConflictError("A payment with this reference_id already exists.")
            self.repository.get("members", payload.member_id)
            if payload.membership_id is not None:
                membership = self.repository.get("memberships", payload.membership_id)
                if membership.get("member_id") != payload.member_id:
                    raise DomainError(
                        "validation_error",
                        "membership_id does not belong to member_id.",
                        422,
                    )
            payment = self.repository.create(
                "payments",
                {
                    "member_id": payload.member_id,
                    "membership_id": payload.membership_id,
                    "amount": payload.amount,
                    "refunded_amount": Decimal("0.00"),
                    "method": payload.method,
                    "reference_id": payload.reference_id,
                    "created_at": payload.created_at or datetime.now(timezone.utc),
                    "status": payload.status,
                },
            )
        self.publisher.publish_after_commit("gym.payment.created.v1", payment)
        return payment

    def list(
        self, cursor: int | None, limit: int, filters: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], str | None]:
        rows = sorted(
            self.repository.list("payments", **dict(filters)),
            key=lambda row: int(row["id"]),
        )
        rows = [row for row in rows if cursor is None or int(row["id"]) > cursor]
        page = rows[:limit]
        return page, encode_cursor(page[-1]["id"]) if len(
            rows
        ) > limit and page else None

    def refund(self, payment_id: int, payload: RefundCreateDTO) -> dict[str, Any]:
        with self.repository.transaction():
            payment = self.repository.get("payments", payment_id, for_update=True)
            if str(payment.get("status", "")).upper() != "COMPLETED":
                raise DomainError(
                    "invalid_state", "Only completed payments may be refunded.", 409
                )
            original = _decimal(payment.get("amount"), "amount")
            refunded = _decimal(payment.get("refunded_amount", 0), "refunded_amount")
            if refunded + payload.amount > original:
                raise DomainError(
                    "refund_exceeds_payment",
                    "Refund amount exceeds the remaining payment balance.",
                    422,
                )
            total = refunded + payload.amount
            payment = self.repository.update(
                "payments",
                payment_id,
                {
                    "refunded_amount": total,
                    "status": "REFUNDED" if total == original else "COMPLETED",
                },
            )
        self.publisher.publish_after_commit(
            "gym.payment.refunded.v1",
            {
                "payment_id": payment_id,
                "amount": payload.amount,
                "refunded_amount": payment["refunded_amount"],
                "status": payment["status"],
            },
        )
        return payment


def bind_payments(
    repository: Repository, publisher: EventPublisher | None = None
) -> None:
    payments_bp.repository = repository  # type: ignore[attr-defined]
    payments_bp.publisher = publisher or NullEventPublisher()  # type: ignore[attr-defined]
    reports_bp.repository = repository  # type: ignore[attr-defined]


def _dependencies() -> tuple[Repository, EventPublisher]:
    repository = (
        current_app.extensions.get("repository")
        or current_app.config.get("REPOSITORY")
        or getattr(payments_bp, "repository", None)
    )
    publisher = (
        current_app.extensions.get("event_publisher")
        or current_app.config.get("EVENT_PUBLISHER")
        or getattr(payments_bp, "publisher", None)
        or NullEventPublisher()
    )
    if repository is None:
        raise RuntimeError(
            "A Repository must be configured before registering payments routes."
        )
    return repository, publisher


def _payment_filters() -> dict[str, Any]:
    filters: dict[str, Any] = {}
    for name in ("member_id", "membership_id"):
        if name in request.args:
            try:
                value = int(request.args[name])
                if value <= 0:
                    raise ValueError
            except ValueError:
                raise DomainError(
                    "validation_error", f"{name} must be a positive integer.", 422
                ) from None
            filters[name] = value
    if "status" in request.args:
        status = request.args["status"].strip().upper()
        if status not in PAYMENT_STATUSES:
            raise DomainError("validation_error", "status is invalid.", 422)
        filters["status"] = status
    return filters


@payments_bp.get("/payments")
def list_payments():
    cursor, limit = cursor_params()
    repository, publisher = _dependencies()
    rows, next_cursor = PaymentService(repository, publisher).list(
        cursor, limit, _payment_filters()
    )
    return success(rows, meta={"next_cursor": next_cursor})


@payments_bp.post("/payments")
@roles_required("ADMIN", "STAFF")
def create_payment():
    repository, publisher = _dependencies()
    return success(
        PaymentService(repository, publisher).create(parse_model(PaymentCreateDTO)),
        status=201,
    )


@payments_bp.post("/payments/<int:payment_id>/refunds")
@roles_required("ADMIN", "STAFF")
def refund_payment(payment_id: int):
    repository, publisher = _dependencies()
    return success(
        PaymentService(repository, publisher).refund(
            payment_id, parse_model(RefundCreateDTO)
        )
    )


_REPORTS = {
    "daily-attendance": ("attendance", "check_in_at"),
    "monthly-attendance": ("attendance", "check_in_at"),
    "active-members": ("memberships", "end_date"),
    "expiring-memberships": ("memberships", "end_date"),
    "expired-memberships": ("memberships", "end_date"),
    "payment-statistics": ("payments", "created_at"),
    "trainer-statistics": ("trainer_assignments", "assigned_at"),
    "member-statistics": ("members", "joining_date"),
    "class-enrollment": ("class_enrollments", "enrolled_at"),
}


def _row_date(row: Mapping[str, Any], field: str) -> date | None:
    value = row.get(field)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None
    return None


def _report_dates() -> tuple[date | None, date | None]:
    values: list[date | None] = []
    for name in ("start_date", "end_date"):
        raw = request.args.get(name)
        try:
            values.append(date.fromisoformat(raw) if raw else None)
        except ValueError:
            raise DomainError(
                "validation_error", f"{name} must be an ISO-8601 date.", 422
            ) from None
    if values[0] and values[1] and values[0] > values[1]:
        raise DomainError(
            "validation_error", "start_date cannot be after end_date.", 422
        )
    return values[0], values[1]


class ReportService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def run(self, name: str, start: date | None, end: date | None) -> dict[str, Any]:
        if name not in _REPORTS:
            raise NotFoundError("report")
        table, field = _REPORTS[name]
        rows = [
            row
            for row in self.repository.list(table)
            if (d := _row_date(row, field)) is not None
            and (not start or d >= start)
            and (not end or d <= end)
        ]
        def counts(values: list[dict[str, Any]], key: str) -> dict[str, int]:
            return {
                status: sum(
                    1
                    for value in values
                    if str(value.get(key, "")).upper() == status
                )
                for status in sorted(
                    {str(value.get(key, "")).upper() for value in values}
                )
            }

        if name in {"daily-attendance", "monthly-attendance"}:
            monthly = name == "monthly-attendance"
            buckets: dict[str, int] = {}
            for row in rows:
                d = _row_date(row, "check_in_at")
                key = d.strftime("%Y-%m") if monthly else d.isoformat()
                buckets[key] = buckets.get(key, 0) + 1
            summary: dict[str, Any] = {
                "present_count": len(rows),
                "buckets": [
                    {"month" if monthly else "date": key, "present_count": count}
                    for key, count in sorted(buckets.items())
                ],
            }
        elif name == "active-members":
            active = [r for r in rows if str(r.get("status", "")).upper() == "ACTIVE"]
            summary = {
                "active_members": len({r.get("member_id") for r in active}),
                "active_memberships": len(active),
            }
        elif name in {"expiring-memberships", "expired-memberships"}:
            wanted = "ACTIVE" if name == "expiring-memberships" else "EXPIRED"
            selected = [r for r in rows if str(r.get("status", "")).upper() == wanted]
            summary = {
                "expiring_memberships"
                if wanted == "ACTIVE"
                else "expired_memberships": len(selected)
            }
        elif name == "payment-statistics":
            gross = sum(
                (_decimal(r.get("amount", 0), "amount") for r in rows), Decimal("0")
            )
            refunded = sum(
                (
                    _decimal(r.get("refunded_amount", 0), "refunded_amount")
                    for r in rows
                ),
                Decimal("0"),
            )
            summary = {
                "payment_count": len(rows),
                "gross_amount": gross,
                "refunded_amount": refunded,
                "net_amount": gross - refunded,
                "status_counts": counts(rows, "status"),
                "method_counts": counts(rows, "method"),
            }
        elif name == "trainer-statistics":
            by: dict[int, int] = {}
            [
                by.__setitem__(r["trainer_id"], by.get(r["trainer_id"], 0) + 1)
                for r in rows
                if r.get("trainer_id") is not None
            ]
            summary = {
                "assignment_count": len(rows),
                "trainer_count": len(by),
                "assignments_by_trainer": [
                    {"trainer_id": k, "assigned_members": v}
                    for k, v in sorted(by.items())
                ],
            }
        elif name == "member-statistics":
            summary = {
                "member_count": len(rows),
                "status_counts": counts(rows, "status"),
            }
        else:
            # Report both the lifecycle totals and the useful capacity-facing
            # aggregate: active enrolled members per class.  Cancelled rows are
            # retained in status_counts/history but do not consume a class seat.
            enrolled = [
                row for row in rows if str(row.get("status", "")).upper() == "ENROLLED"
            ]
            by_class: dict[int, int] = {}
            for row in enrolled:
                class_id = row.get("class_id")
                if class_id is not None:
                    by_class[int(class_id)] = by_class.get(int(class_id), 0) + 1
            summary = {
                "enrollment_count": len(rows),
                "enrolled_count": len(enrolled),
                "enrollments_by_class": [
                    {"class_id": class_id, "enrolled_members": enrolled_count}
                    for class_id, enrolled_count in sorted(by_class.items())
                ],
                "status_counts": counts(rows, "status"),
            }
        summary["count"] = len(rows)
        return {
            "report": name,
            "start_date": start,
            "end_date": end,
            "summary": summary,
        }


@reports_bp.get("/reports/<report_name>")
@roles_required("ADMIN", "STAFF", "TRAINER")
def get_report(report_name: str):
    repository, _ = _dependencies()
    start, end = _report_dates()
    return success(ReportService(repository).run(report_name, start, end))
