"""Focused regression tests for the canonical members contract."""

from datetime import date

import pytest
from pydantic import ValidationError

from app.members import MemberCreateDTO, MemberPatchDTO, MemberService
from app.repository import CapturingPublisher, InMemoryRepository


def _create_payload(**overrides):
    payload = {
        "name": "Ada Lovelace",
        "email": "ADA@EXAMPLE.TEST",
        "phone": "+1 (555) 123-4567",
        "joining_date": "2024-01-15",
        "date_of_birth": "1815-12-10",
        "address": "12 Analytical Engine Way",
        "emergency_contact": "Charles Babbage",
    }
    payload.update(overrides)
    return payload


def test_create_uses_client_joining_date_and_only_canonical_name():
    repository = InMemoryRepository()
    publisher = CapturingPublisher()
    service = MemberService(repository, publisher)

    created = service.create(MemberCreateDTO.model_validate(_create_payload()))

    assert created["name"] == "Ada Lovelace"
    assert created["joining_date"] == "2024-01-15"
    assert created["email"] == "ada@example.test"
    assert created["phone"] == "+15551234567"
    assert "first_name" not in created
    assert "last_name" not in created
    assert publisher.events == [("gym.member.created.v1", created)]


def test_member_creation_requires_name_email_phone_and_joining_date():
    for field in ("name", "email", "phone", "joining_date"):
        payload = _create_payload()
        del payload[field]
        with pytest.raises(ValidationError):
            MemberCreateDTO.model_validate(payload)

    with pytest.raises(ValidationError):
        MemberCreateDTO.model_validate(
            _create_payload(first_name="Ada", last_name="Lovelace")
        )


def test_update_and_search_use_canonical_name_and_profile_fields():
    repository = InMemoryRepository()
    service = MemberService(repository, CapturingPublisher())
    created = service.create(MemberCreateDTO.model_validate(_create_payload()))
    service.create(
        MemberCreateDTO.model_validate(
            _create_payload(
                name="Grace Hopper",
                email="grace@example.test",
                phone="555-987-6543",
                joining_date=date(2024, 2, 1),
            )
        )
    )

    updated = service.update(
        created["id"],
        MemberPatchDTO.model_validate(
            {"name": "Ada King", "address": None, "joining_date": "2024-01-16"}
        ),
    )
    rows, _ = service.list(
        cursor=None,
        limit=10,
        status=None,
        search="king",
        sort="id",
        direction="asc",
    )

    assert updated["name"] == "Ada King"
    assert updated["joining_date"] == "2024-01-16"
    assert updated["address"] is None
    assert [row["id"] for row in rows] == [created["id"]]
    assert "first_name" not in updated
    assert "last_name" not in updated
