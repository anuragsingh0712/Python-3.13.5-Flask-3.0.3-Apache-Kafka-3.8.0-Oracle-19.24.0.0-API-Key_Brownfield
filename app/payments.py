"""Payments, refunds, and read-only operational reports.

The module deliberately depends only on the repository and publisher interfaces.  An
application can inject those through ``bind_payments`` (or its Flask extensions),
which keeps the payment write transaction usable with both Oracle and the in-memory
repository used by tests.  Reports are intentionally calculated from canonical
schema fields, rather than accepting a client-selected table or column.
"""} }]}Oops used multi syntax to functions maybe response? It accepted? tool result blank likely call malformed? It shows tool? Actually nested. Need check. Then big edit. Need old text exact entirety report section too huge ~118 lines. Can do successive delete old map then class. Replace map only then class run. First map. Also obsolete comment. Use exact. +#+#+#+#+#+ിയassistant to=functions.edit_file კომენტary 񎔊ppնչ্ছেনಲ್ಲಿ  天天中彩票是不是彩票招商്യം d天天 全民彩票天天  北京赛车怎么json{Jsii 

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


# These are the complete public report names.  Values identify only fixed,
# canonical schema fields; neither a table nor a column is accepted from a request.
_REPORTS: dict[str, tuple[str, str]] = {
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

    @staticmethod
    def _dated_rows(
        rows: list[dict[str, Any]], field: str, start: date | None, end: date | None
    ) -> list[dict[str, Any]]:
        """Return rows with a real canonical date in the inclusive requested range."""
        selected: list[dict[str, Any]] = []
        for row in rows:
            row_date = _row_date(row, field)
            if row_date is None:
                continue
            if (start and row_date < start) or (end and row_date > end):
                continue
            selected.append(row)
        return selected

    @staticmethod
    def _counts(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            key = str(row.get(field, "UNKNOWN")).upper()
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    @staticmethod
    def _attendance_buckets(
        rows: list[dict[str, Any]], monthly: bool
    ) -> list[dict[str, Any]]:
        buckets: dict[str, int] = {}
        for row in rows:
            checked_in = _row_date(row, "check_in_at")
            # _dated_rows already rejects null check_in_at.  Keeping this guard makes
            # presence semantics explicit if this helper is reused.
            if checked_in is None:
                continue
            key = checked_in.strftime("%Y-%m") if monthly else checked_in.isoformat()
            buckets[key] = buckets.get(key, 0) + 1
        label = "month" if monthly else "date"
        return [
            {label: key, "present_count": value}
            for key, value in sorted(buckets.items())
        ]

    def run(
        self, name: str, start: date | None, end: date | None
    ) -> dict[str, Any]:
        try:
            table, date_field = _REPORTS[name]
        except KeyError:
            raise NotFoundError("report") from None

        rows = self._dated_rows(self.repository.list(table), date_field, start, end)
        summary: dict[str, Any]
        if name in {"daily-attendance", "monthly-attendance"}:
            # A check-in is the canonical proof of attendance; ATTENDANCE has no
            # status column and no inferred status is consulted here.
            monthly = name == "monthly-attendance"
            summary = {
                "present_count": len(rows),
                "buckets": self._attendance_buckets(rows, monthly),
            }
        elif name == "active-members":
            active = [
                row for row in rows if str(row.get("status", "")).upper() == "ACTIVE"
            ]
            member_ids = {
                row.get("member_id")
                for row in active
                if row.get("member_id") is not None
            }
            summary = {
                "active_members": len(member_ids),
                "active_memberships": len(active),
            }
        elif name == "expiring-memberships":
            expiring = [
                row for row in rows if str(row.get("status", "")).upper() == "ACTIVE"
            ]
            summary = {
                "expiring_memberships": len(expiring),
                "by_end_date": self._date_buckets(expiring, "end_date"),
            }
        elif name == "expired-memberships":
            expired = [
                row for row in rows if str(row.get("status", "")).upper() == "EXPIRED"
            ]
            summary = {
                "expired_memberships": len(expired),
                "by_end_date": self._date_buckets(expired, "end_date"),
            }
        elif name == "payment-statistics":
            gross = sum(
                (_decimal(row.get("amount", 0), "amount") for row in rows),
                Decimal("0"),
            )
            refunded = sum(
                (
                    _decimal(row.get("refunded_amount", 0), "refunded_amount")
                    for row in rows
                ),
                Decimal("0"),
            )
            summary = {
                "payment_count": len(rows),
                "gross_amount": gross,
                "refunded_amount": refunded,
                "net_amount": gross - refunded,
                "status_counts": self._counts(rows, "status"),
                "method_counts": self._counts(rows, "method"),
            }
        elif name == "trainer-statistics":
            by_trainer: dict[str, int] = {}
            for row in rows:
                trainer_id = row.get("trainer_id")
                if trainer_id is not None:
                    key = str(trainer_id)
                    by_trainer[key] = by_trainer.get(key, 0) + 1
            summary = {
                "assignment_count": len(rows),
                "trainer_count": len(by_trainer),
                "assignments_by_trainer": [
                    {"trainer_id": int(key), "assigned_members": value}
                    for key, value in sorted(by_trainer.items(), key=lambda item: int(item[0]))
                ],
            }
        elif name == "member-statistics":
            summary = {
                "member_count": len(rows),
                "status_counts": self._counts(rows, "status"),
                "joined_by_date": self._date_buckets(rows, "joining_date"),
            }
        else:  # class-enrollment
            by_class: dict[str, int] = {}
            for row in rows:
                if str(row.get("status", "")).upper() != "ENROLLED":
                    continue
                class_id = row.get("class_id")
                if class_id is not None:
                    key = str(class_id)
                    by_class[key] = by_class.get(key, 0) + 1
            summary = {
                "enrollment_count": len(rows),
                "enrolled_count": sum(by_class.values()),
                "status_counts": self._counts(rows, "status"),
                "enrollments_by_class": [
                    {"class_id": int(key), "enrolled_members": value}
                    for key, value in sorted(by_class.items(), key=lambda item: int(item[0]))
                ],
            }
        # Keep a uniform selected-row count while named fields retain their
        # domain-specific meaning (for example, active_members is de-duplicated).
        summary["count"] = len(rows)
        return {
            "report": name,
            "start_date": start,
            "end_date": end,
            "summary": summary,
        }

    @staticmethod
    def _date_buckets(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in rows:
            value = _row_date(row, field)
            if value is not None:
                key = value.isoformat()
                counts[key] = counts.get(key, 0) + 1
        return [{"date": key, "count": value} for key, value in sorted(counts.items())]


@reports_bp.get("/reports/<report_name>")
@roles_required("ADMIN", "STAFF", "TRAINER")
def get_report(report_name: str):
    repository, _ = _dependencies()
    start, end = _report_dates()
    return success(ReportService(repository).run(report_name, start, end))
