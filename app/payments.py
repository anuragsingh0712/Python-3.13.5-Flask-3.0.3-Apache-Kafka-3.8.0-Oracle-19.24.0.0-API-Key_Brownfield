"""Payments, refunds, and read-only operational reports.

The module deliberately depends only on the repository and publisher interfaces.  An
application can inject those through ``bind_payments`` (or its Flask extensions),
which keeps the payment write transaction usable with both Oracle and the in-memory
repository used by tests.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping

from flask import Blueprint, current_app, request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core import (
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
    """The amount is the original captured amount, never a negative adjustment."""

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
    def clean_method(cls, value: Any) -> str:
        if not isinstance(value, str):
            return value
        return value.strip().upper()

    @field_validator("reference_id")
    @classmethod
    def clean_reference_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reference_id must not be blank")
        return value


class RefundCreateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    reason: str | None = Field(default=None, max_length=500)


class PaymentService:
    def __init__(
        self, repository: Repository, publisher: EventPublisher | None = None
    ) -> None:
        self.repository = repository
        self.publisher = publisher or NullEventPublisher()

    def create(self, payload: PaymentCreateDTO) -> dict[str, Any]:
        # The foreign key checks make the same rule apply to the in-memory adapter
        # and to an Oracle implementation which may additionally enforce FK DDL.
        self.repository.get("members", payload.member_id)
        if payload.membership_id is not None:
            membership = self.repository.get("memberships", payload.membership_id)
            if membership.get("member_id") != payload.member_id:
                raise DomainError(
                    "validation_error",
                    "membership_id does not belong to member_id.",
                    422,
                )

        values = {
            "member_id": payload.member_id,
            "membership_id": payload.membership_id,
            "amount": payload.amount,
            "refunded_amount": Decimal("0.00"),
            "method": payload.method,
            "reference_id": payload.reference_id,
            "created_at": payload.created_at or datetime.now(timezone.utc),
            "status": payload.status,
        }
        with self.repository.transaction():
            payment = self.repository.create("payments", values)
        self.publisher.publish_after_commit("gym.payment.created.v1", payment)
        return payment

    def list(
        self, cursor: int | None, limit: int, filters: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], str | None]:
        rows = self.repository.list("payments", **dict(filters))
        rows.sort(key=lambda row: int(row["id"]))
        rows = [row for row in rows if cursor is None or int(row["id"]) > cursor]
        page = rows[:limit]
        next_cursor = (
            encode_cursor(page[-1]["id"]) if len(rows) > limit and page else None
        )
        return page, next_cursor

    def refund(self, payment_id: int, payload: RefundCreateDTO) -> dict[str, Any]:
        # Keeping the read, bound check, and write inside the transaction is required:
        # concurrent refunds must not be able to exceed the original captured amount.
        with self.repository.transaction():
            payment = self.repository.get("payments", payment_id, for_update=True)
            status = str(payment.get("status", "")).upper()
            if status != "COMPLETED":
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
            # The persisted payment status has no partial-refund state.  A partial
            # refund is represented solely by refunded_amount; only a full refund
            # transitions the payment to REFUNDED.
            state = "REFUNDED" if total == original else "COMPLETED"
            updated = self.repository.update(
                "payments",
                payment_id,
                {
                    "refunded_amount": total,
                    "status": state,
                },
            )
        self.publisher.publish_after_commit(
            "gym.payment.refunded.v1",
            {
                "payment_id": payment_id,
                "amount": payload.amount,
                "refunded_amount": updated["refunded_amount"],
                "status": updated["status"],
            },
        )
        return updated


def _decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as error:
        raise DomainError(
            "invalid_payment", f"Stored {field} is invalid.", 409
        ) from error


def bind_payments(
    repository: Repository, publisher: EventPublisher | None = None
) -> None:
    """Optional lightweight injection point for apps that do not use extensions."""
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
        raw = request.args.get(name)
        if raw is not None:
            try:
                value = int(raw)
                if value <= 0:
                    raise ValueError
            except ValueError:
                raise DomainError(
                    "validation_error", f"{name} must be a positive integer.", 422
                ) from None
            filters[name] = value
    if request.args.get("status") is not None:
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
    payment = PaymentService(repository, publisher).create(
        parse_model(PaymentCreateDTO)
    )
    return success(payment, status=201)


@payments_bp.post("/payments/<int:payment_id>/refunds")
@roles_required("ADMIN", "STAFF")
def refund_payment(payment_id: int):
    repository, publisher = _dependencies()
    payment = PaymentService(repository, publisher).refund(
        payment_id, parse_model(RefundCreateDTO)
    )
    return success(payment)


# Reports are named, allow-listed queries.  Each one has exactly one schema-backed
# table and date column; request input is never used as an identifier.
_REPORTS: dict[str, tuple[str, str]] = {
    "attendance": ("attendance", "check_in_at"),
    "member": ("members", "joining_date"),
    "membership": ("memberships", "start_date"),
    "revenue": ("payments", "created_at"),
    "payment": ("payments", "created_at"),
    "trainer-member": ("trainer_assignments", "assigned_at"),
    "trainer-assignment": ("trainer_assignments", "assigned_at"),
    "class-enrollment": ("class_enrollments", "enrolled_at"),
    "classes": ("gym_classes", "scheduled_at"),
}


def _report_dates() -> tuple[date | None, date | None]:
    def parse(name: str) -> date | None:
        raw = request.args.get(name)
        if raw is None:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise DomainError(
                "validation_error", f"{name} must be an ISO-8601 date.", 422
            ) from None

    start, end = parse("start_date"), parse("end_date")
    if start and end and start > end:
        raise DomainError(
            "validation_error", "start_date cannot be after end_date.", 422
        )
    return start, end


def _row_date(row: Mapping[str, Any], field: str) -> date | None:
    """Read only the allow-listed date column for the selected report."""
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


class ReportService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def run(self, name: str, start: date | None, end: date | None) -> dict[str, Any]:
        try:
            table, date_field = _REPORTS[name]
        except KeyError:
            raise NotFoundError("report") from None
        # An SQL-backed adapter may provide a parameterized, pre-defined aggregate;
        # the name is allow-listed above and never originates as a table identifier.
        delegate = getattr(self.repository, "aggregate_report", None)
        if callable(delegate):
            result = delegate(name, start_date=start, end_date=end)
            return {
                "report": name,
                "start_date": start,
                "end_date": end,
                "summary": result,
            }
        rows = self.repository.list(table)
        selected = []
        for row in rows:
            row_date = _row_date(row, date_field)
            if (start and (row_date is None or row_date < start)) or (
                end and (row_date is None or row_date > end)
            ):
                continue
            selected.append(row)
        summary: dict[str, Any] = {"count": len(selected)}
        if name in {"revenue", "payment"}:
            gross = sum(
                (_decimal(row.get("amount", 0), "amount") for row in selected),
                Decimal("0"),
            )
            refunded = sum(
                (
                    _decimal(row.get("refunded_amount", 0), "refunded_amount")
                    for row in selected
                ),
                Decimal("0"),
            )
            summary.update(
                {
                    "gross_amount": gross,
                    "refunded_amount": refunded,
                    "net_amount": gross - refunded,
                }
            )
        elif name == "attendance":
            summary["present_count"] = sum(
                1
                for row in selected
                if str(row.get("status", "")).upper() in {"PRESENT", "CHECKED_IN"}
            )
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
