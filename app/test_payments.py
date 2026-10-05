"""Focused module tests for payments and reports."""

from app import create_app
from app.core import Settings
from app.repository import CapturingPublisher, InMemoryRepository


def client():
    repo, publisher = InMemoryRepository(), CapturingPublisher()
    repo.create("members", {"name": "Ada"})
    repo.create("memberships", {"member_id": 1})
    app = create_app(
        repository=repo,
        publisher=publisher,
        settings=Settings({"key": frozenset({"ADMIN"})}, "", "", "", "", ""),
    )
    return app.test_client(), repo, publisher


def test_create_and_bounded_refunds():
    http, _, publisher = client()
    headers = {"X-API-Key": "key"}
    created = http.post(
        "/api/v1/payments",
        json={
            "member_id": 1,
            "membership_id": 1,
            "amount": "10.00",
            "method": "card",
            "reference_id": "payment-1",
        },
        headers=headers,
    )
    assert created.status_code == 201
    assert (
        http.post(
            "/api/v1/payments/1/refunds", json={"amount": "4.00"}, headers=headers
        ).get_json()["data"]["status"]
        == "COMPLETED"
    )
    assert (
        http.post(
            "/api/v1/payments/1/refunds", json={"amount": "7.00"}, headers=headers
        ).status_code
        == 422
    )
    assert [event[0] for event in publisher.events] == [
        "gym.payment.created.v1",
        "gym.payment.refunded.v1",
    ]


def test_payment_statistics_report_is_authenticated_and_aggregated():
    http, _, _ = client()
    assert http.get("/api/v1/reports/payment-statistics").status_code == 401
    response = http.get(
        "/api/v1/reports/payment-statistics", headers={"X-API-Key": "key"}
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["summary"]["net_amount"] == "0"
