"""Contract tests for the hand-written documentation and injected resource seams."""

from pathlib import Path

from app import create_app
from app.core import Settings
from app.repository import CapturingPublisher, InMemoryRepository


SETTINGS = Settings(
    {
        "admin-key": frozenset({"ADMIN"}),
        "trainer-key": frozenset({"TRAINER"}),
        "staff-key": frozenset({"STAFF"}),
    },
    "",
    "",
    "",
    "",
    "",
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
    assert (
        spec["paths"]["/api/v1/members/{member_id}"]["patch"]["parameters"][0]["in"]
        == "path"
    )
    assert (
        spec["paths"]["/api/v1/workout-plans"]["post"]["requestBody"]["required"]
        is True
    )
    assert spec["paths"]["/api/v1/payments/{payment_id}/refunds"]["post"][
        "security"
    ] == [{"ApiKeyAuth": []}]
    assert "MemberCreate" in spec["components"]["schemas"]
    assert client.get("/api/v1/members").status_code == 401


def test_openapi_tracks_the_repaired_public_contract():
    spec = create_app(settings=SETTINGS).test_client().get("/openapi.json").get_json()
    schemas = spec["components"]["schemas"]

    member = schemas["MemberCreate"]
    assert member["required"] == ["name", "email", "phone", "joining_date"]
    assert "first_name" not in member["properties"]
    assert "last_name" not in member["properties"]
    assert {"address", "emergency_contact", "date_of_birth"} <= set(
        member["properties"]
    )

    plan = schemas["MembershipPlanCreate"]
    assert plan["properties"]["price"]["minimum"] == 0
    assert "visit_limit" not in plan["required"]
    assert plan["properties"]["visit_limit"]["nullable"] is True

    trainer = schemas["TrainerCreate"]
    assert "ON_LEAVE" in trainer["properties"]["status"]["enum"]
    assert "max_members" not in trainer["required"]
    assert trainer["properties"]["max_members"]["nullable"] is True

    attendance_parameters = spec["paths"]["/api/v1/attendance"]["get"]["parameters"]
    assert {parameter["name"] for parameter in attendance_parameters} >= {
        "from_date",
        "to_date",
        "membership_id",
    }
    report_parameter = spec["paths"]["/api/v1/reports/{report_name}"]["get"][
        "parameters"
    ][0]
    assert report_parameter["schema"]["enum"] == [
        "daily-attendance",
        "monthly-attendance",
        "active-members",
        "expiring-memberships",
        "expired-memberships",
        "payment-statistics",
        "trainer-statistics",
        "member-statistics",
        "class-enrollment",
    ]


def test_compose_has_fresh_checkout_defaults_and_pinned_infrastructure_images():
    compose_path = Path(__file__).resolve().parent.parent / "docker-compose.yml"
    compose = compose_path.read_text()
    assert "env_file:" not in compose
    assert "gvenzl/oracle-free:23-slim" in compose
    assert "apache/kafka:3.8.0" in compose
    assert 'API_KEY: \'{"replace-admin-key"' in compose


def test_members_auth_roles_and_created_event_through_factory_seam():
    repository, publisher = InMemoryRepository(), CapturingPublisher()
    client = create_app(
        repository=repository, publisher=publisher, settings=SETTINGS
    ).test_client()
    payload = {
        "name": "Ada Lovelace",
        "email": "ADA@example.test",
        "phone": "+15551234",
        "joining_date": "2025-01-01",
    }
    assert client.post("/api/v1/members", json=payload).status_code == 401
    assert (
        client.post(
            "/api/v1/members", json=payload, headers={"X-API-Key": "trainer-key"}
        ).status_code
        == 403
    )
    created = client.post("/api/v1/members", json=payload, headers=ADMIN)
    assert created.status_code == 201
    assert created.get_json()["data"]["email"] == "ada@example.test"
    assert publisher.events[0][0] == "gym.member.created.v1"
    assert (
        client.get("/api/v1/members?status=active", headers=ADMIN).get_json()["data"][
            0
        ]["id"]
        == 1
    )


def test_trainer_resource_rejects_wrong_role_and_publishes_assignment_event():
    repository, publisher = InMemoryRepository(), CapturingPublisher()
    client = create_app(
        repository=repository, publisher=publisher, settings=SETTINGS
    ).test_client()
    trainer = {
        "name": "Tess",
        "email": "tess@example.test",
        "phone": "5551234",
        "max_members": 2,
    }
    assert client.post("/api/v1/trainers", json=trainer).status_code == 401
    assert (
        client.post(
            "/api/v1/trainers", json=trainer, headers={"X-API-Key": "trainer-key"}
        ).status_code
        == 403
    )
    assert (
        client.post("/api/v1/trainers", json=trainer, headers=ADMIN).status_code == 201
    )
    repository.create("members", {"status": "ACTIVE", "name": "Ada"})
    assigned = client.post(
        "/api/v1/trainer-assignments",
        json={"trainer_id": 1, "member_id": 1},
        headers=ADMIN,
    )
    assert assigned.status_code == 201
    assert publisher.events == [
        ("gym.trainer_assignment.created.v1", assigned.get_json()["data"])
    ]


def test_workout_owner_authorization_and_post_commit_events():
    repository, publisher = InMemoryRepository(), CapturingPublisher()
    repository.create("members", {"status": "ACTIVE"})
    repository.create("trainers", {"status": "ACTIVE"})
    client = create_app(
        repository=repository, publisher=publisher, settings=SETTINGS
    ).test_client()
    payload = {"member_id": 1, "trainer_id": 1, "name": "Strength"}
    assert client.post("/api/v1/workout-plans", json=payload).status_code == 401
    assert (
        client.post(
            "/api/v1/workout-plans",
            json=payload,
            headers={"X-API-Key": "trainer-key", "X-Trainer-ID": "2"},
        ).status_code
        == 403
    )
    created = client.post(
        "/api/v1/workout-plans",
        json=payload,
        headers={"X-API-Key": "trainer-key", "X-Trainer-ID": "1"},
    )
    assert created.status_code == 201
    exercise = client.post(
        "/api/v1/workout-plans/1/exercises",
        json={"name": "Squat", "sets": 3},
        headers=ADMIN,
    )
    assert exercise.status_code == 201
    assert [topic for topic, _ in publisher.events] == [
        "gym.workout_plan.created.v1",
        "gym.workout_exercise.created.v1",
    ]
