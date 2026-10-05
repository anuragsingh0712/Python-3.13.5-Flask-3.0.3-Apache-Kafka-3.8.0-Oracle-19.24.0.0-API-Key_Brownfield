"""Oracle repository contract tests using a driver-shaped fake pool.

These verify transaction commit/rollback callbacks and bound locking SQL without needing
an external database in the unit-test process. The compose migration path is verified
separately by the idempotent migration runner in scripts/init_schema.py.
"""

from __future__ import annotations

import pytest

from app.core import ConflictError
from app.repository import OracleRepository, TransactionalPublisher


class Cursor:
    def __init__(self, connection: "Connection") -> None:
        self.connection = connection
        self.description = [("ID",), ("STATUS",)]

    def execute(self, statement: str, binds: dict | None = None) -> None:
        self.connection.statements.append((statement, binds or {}))
        if self.connection.error is not None:
            raise self.connection.error

    def fetchall(self) -> list[tuple[int, str]]:
        return [(1, "ACTIVE")]

    def close(self) -> None:
        pass


class Connection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, dict]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.error: Exception | None = None

    def cursor(self) -> Cursor:
        return Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class Pool:
    def __init__(self) -> None:
        self.connections: list[Connection] = []

    def acquire(self) -> Connection:
        connection = Connection()
        self.connections.append(connection)
        return connection


class Capture:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish_after_commit(self, topic: str, payload: dict) -> None:
        self.events.append((topic, payload))

    def close(self) -> None:
        pass


def test_oracle_transaction_commits_and_publishes_only_after_commit():
    pool = Pool()
    repository = OracleRepository(pool)
    capture = Capture()
    publisher = TransactionalPublisher(repository, capture)
    with repository.transaction():
        publisher.publish_after_commit("gym.member.created.v1", {"id": 1})
        assert capture.events == []
    assert pool.connections[0].commits == 1
    assert capture.events == [("gym.member.created.v1", {"id": 1})]


def test_oracle_transaction_rolls_back_and_discards_deferred_events():
    pool = Pool()
    repository = OracleRepository(pool)
    capture = Capture()
    publisher = TransactionalPublisher(repository, capture)
    with pytest.raises(RuntimeError):
        with repository.transaction():
            publisher.publish_after_commit("gym.member.created.v1", {"id": 1})
            raise RuntimeError("force rollback")
    assert pool.connections[0].rollbacks == 1
    assert capture.events == []


def test_oracle_get_uses_bound_select_for_update_and_translates_constraints():
    pool = Pool()
    repository = OracleRepository(pool)
    row = repository.get("memberships", 1, for_update=True)
    statement, binds = pool.connections[0].statements[0]
    assert statement == "SELECT * FROM MEMBERSHIPS WHERE id = :id FOR UPDATE"
    assert binds == {"id": 1}
    assert row == {"id": 1, "status": "ACTIVE"}

    class UniqueViolation(Exception):
        code = 1

    pool = Pool()
    pool_connection = pool.acquire()
    pool_connection.error = UniqueViolation("ORA-00001")
    # Keep the configured error on the connection returned to the repository.
    pool.acquire = lambda: pool_connection  # type: ignore[method-assign]
    with pytest.raises(ConflictError):
        repository = OracleRepository(pool)
        repository.get("memberships", 1)
