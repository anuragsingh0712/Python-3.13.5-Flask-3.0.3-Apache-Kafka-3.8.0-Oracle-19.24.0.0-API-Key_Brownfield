"""Focused contract coverage for trainer status and nullable capacity."""

from app import create_app
from app.core import Settings
from app.repository import InMemoryRepository


SETTINGS = Settings(
    {"admin-key": frozenset({"ADMIN"})}, "", "", "", "", ""
)
ADMIN_HEADERS = {"X-API-Key": "admin-key"}


def _client_and_repository():
    repository = InMemoryRepository()
    return create_app(repository=repository, settings=SETTINGS).test_client(), repository


def _trainer_payload(**overrides):
    payload = {
        "name": "Taylor Trainer",
        "email": "taylor@example.test",
        "phone": "555-0100",
    }
    payload.update(overrides)
    return payload


def _active_member(repository, number):
    return repository.create(
        "members", {"name": f"Member {number}", "status": "ACTIVE"}
    )


def test_trainer_create_and_patch_accept_null_or_omitted_capacity():
    client, _ = _client_and_repository()

    omitted = client.post(
        "/api/v1/trainers", json=_trainer_payload(), headers=ADMIN_HEADERS
    )
    assert omitted.status_code == 201
    assert omitted.get_json()["data"]["max_members"] is None

    explicit_null = client.post(
        "/api/v1/trainers",
        json=_trainer_payload(email="null@example.test", phone="555-0101", max_members=None),
        headers=ADMIN_HEADERS,
    )
    assert explicit_null.status_code == 201
    assert explicit_null.get_json()["data"]["max_members"] is None

    patched = client.patch(
        "/api/v1/trainers/1", json={"max_members": None}, headers=ADMIN_HEADERS
    )
    assert patched.status_code == 200
    assert patched.get_json()["data"]["max_members"] is None


def test_trainer_validation_allows_on_leave_and_rejects_invalid_status_or_capacity():
    client, _ = _client_and_repository()

    on_leave = client.post(
        "/api/v1/trainers",
        json=_trainer_payload(status="on_leave"),
        headers=ADMIN_HEADERS,
    )
    assert on_leave.status_code == 201
    assert on_leave.get_json()["data"]["status"] == "ON_LEAVE"

    invalid_status = client.post(
        "/api/v1/trainers",
        json=_trainer_payload(email="bad-status@example.test", phone="555-0101", status="BUSY"),
        headers=ADMIN_HEADERS,
    )
    assert invalid_status.status_code == 422
    assert invalid_status.get_json()["error"]["code"] == "validation_error"

    invalid_capacity = client.post(
        "/api/v1/trainers",
        json=_trainer_payload(email="bad-capacity@example.test", phone="555-0102", max_members=0),
        headers=ADMIN_HEADERS,
    )
    assert invalid_capacity.status_code == 422
    assert invalid_capacity.get_json()["error"]["code"] == "validation_error"


def test_on_leave_trainer_cannot_receive_assignment():
    client, repository = _client_and_repository()
    trainer = client.post(
        "/api/v1/trainers",
        json=_trainer_payload(status="ON_LEAVE"),
        headers=ADMIN_HEADERS,
    )
    member = _active_member(repository, 1)

    response = client.post(
        "/api/v1/trainer-assignments",
        json={"trainer_id": trainer.get_json()["data"]["id"], "member_id": member["id"]},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "conflict"


def test_unlimited_trainer_accepts_multiple_assignments_and_cannot_be_reduced_too_far():
    client, repository = _client_and_repository()
    trainer = client.post(
        "/api/v1/trainers", json=_trainer_payload(), headers=ADMIN_HEADERS
    ).get_json()["data"]
    members = [_active_member(repository, number) for number in (1, 2)]

    for member in members:
        response = client.post(
            "/api/v1/trainer-assignments",
            json={"trainer_id": trainer["id"], "member_id": member["id"]},
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 201

    reduction = client.patch(
        f"/api/v1/trainers/{trainer['id']}",
        json={"max_members": 1},
        headers=ADMIN_HEADERS,
    )
    assert reduction.status_code == 409
    assert reduction.get_json()["error"]["code"] == "conflict"


def test_finite_trainer_capacity_rejects_assignment_at_limit():
    client, repository = _client_and_repository()
    trainer = client.post(
        "/api/v1/trainers",
        json=_trainer_payload(max_members=1),
        headers=ADMIN_HEADERS,
    ).get_json()["data"]
    first, second = _active_member(repository, 1), _active_member(repository, 2)

    accepted = client.post(
        "/api/v1/trainer-assignments",
        json={"trainer_id": trainer["id"], "member_id": first["id"]},
        headers=ADMIN_HEADERS,
    )
    rejected = client.post(
        "/api/v1/trainer-assignments",
        json={"trainer_id": trainer["id"], "member_id": second["id"]},
        headers=ADMIN_HEADERS,
    )
    assert accepted.status_code == 201
    assert rejected.status_code == 409
    assert rejected.get_json()["error"]["code"] == "conflict"
