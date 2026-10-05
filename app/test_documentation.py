"""Contract tests for the hand-written documentation and injected resource seams."""
from flask import Flask

from app import create_app
from app.core import Settings, configure_app
from app.repository import CapturingPublisher, InMemoryRepository
from app.trainers import configure_trainers, trainers_bp


SETTINGS = Settings(
    {"admin-key": frozenset({"ADMIN"}), "trainer-key": frozenset({"TRAINER"}), "staff-key": frozenset({"STAFF"})},
    "", "", "", "", "",
)
ADMIN = {"X-API-Key": "admin-key"}


def test_public_documentation_is_open_and_business_operations_are_secured():
    app = create_app(settings=SETTINGS)
    client = app.test_client()
    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 200
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.get_json()
    assert spec["paths"]["/api/v1/members/{member_id}"]["patch"]["parameters"][0]["in"] == "path"
    assert spec["paths"]["/api/v1/workout-plans"]["post"]["requestBody"]["required"] is True
    assert spec["paths"]["/api/v1/payments/{payment_id}/refunds"]["post"]["security"] == [{"ApiKeyAuth": []}]
    assert "MemberCreate" in spec["components"]["schemas"]
    assert client.get("/api/v1/members").status_code == 401


def test_members_auth_roles_and_created_event_through_factory_seam():
    repository, publisher = InMemoryRepository(), CapturingPublisher()
    client = create_app(repository=repository, publisher=publisher, settings=SETTINGS).test_client()
    payload = {"first_name": "Ada", "last_name": "Lovelace", "email": "ADA@example.test", "phone": "+15551234"}
    assert client.post("/api/v1/members", json=payload).status_code == 401
    assert client.post("/api/v1/members", json=payload, headers={"X-API-Key": "trainer-key"}).status_code == 403
    created = client.post("/api/v1/members", json=payload, headers=ADMIN)
    assert created.status_code == 201
    assert created.get_json()["data"]["email"] == "ada@example.test"
    assert publisher.events[0][0] == "gym.member.created.v1"
    assert client.get("/api/v1/members?status=active", headers=ADMIN).get_json()["data"][0]["id"] == 1


def test_trainer_resource_rejects_wrong_role_and_publishes_assignment_event():
    app, repository, publisher = Flask(__name__), InMemoryRepository(), CapturingPublisher()
    configure_app(app, SETTINGS)
    configure_trainers(repository, publisher)
    app.register_blueprint(trainers_bp)
    client = app.test_client()
    trainer = {"name": "Tess", "email": "tess@example.test", "phone": "5551234", "max_members": 2}
    assert client.post("/api/v1/trainers", json=trainer).status_code == 401
    assert client.post("/api/v1/trainers", json=trainer, headers={"X-API-Key": "trainer-key"}).status_code == 403
    assert client.post("/api/v1/trainers", json=trainer, headers=ADMIN).status_code == 201
    repository.create("members", {"status": "ACTIVE", "name": "Ada"})
    assigned = client.post("/api/v1/trainer-assignments", json={"trainer_id": 1, "member_id": 1}, headers=ADMIN)
    assert assigned.status_code == 201
    assert publisher.events == [("gym.trainer_assignment.created.v1", assigned.get_json()["data"])]


def test_workout_owner_authorization_and_post_commit_events():
    repository, publisher = InMemoryRepository(), CapturingPublisher()
    repository.create("members", {"status": "ACTIVE"})
    repository.create("trainers", {"status": "ACTIVE"})
    client = create_app(repository=repository, publisher=publisher, settings=SETTINGS).test_client()
    payload = {"member_id": 1, "trainer_id": 1, "name": "Strength"}
    assert client.post("/api/v1/workout-plans", json=payload).status_code == 401
    assert client.post("/api/v1/workout-plans", json=payload, headers={"X-API-Key": "trainer-key", "X-Trainer-ID": "2"}).status_code == 403
    created = client.post("/api/v1/workout-plans", json=payload, headers={"X-API-Key": "trainer-key", "X-Trainer-ID": "1"})
    assert created.status_code == 201
    exercise = client.post("/api/v1/workout-plans/1/exercises", json={"name": "Squat", "sets": 3}, headers=ADMIN)
    assert exercise.status_code == 201
    assert [topic for topic, _ in publisher.events] == ["gym.workout_plan.created.v1", "gym.workout_exercise.created.v1"]
