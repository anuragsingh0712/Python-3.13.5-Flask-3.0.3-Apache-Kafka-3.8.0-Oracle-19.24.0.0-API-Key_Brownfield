"""Repository abstractions with a locking-aware in-memory implementation for tests."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from copy import deepcopy
from threading import RLock
from typing import Any

from app.core import ConflictError, NotFoundError


class Repository(ABC):
    @abstractmethod
    def create(self, table: str, values: dict[str, Any]) -> dict[str, Any]: ...
    @abstractmethod
    def get(self, table: str, identifier: int, for_update: bool = False) -> dict[str, Any]: ...
    @abstractmethod
    def list(self, table: str, **filters: Any) -> list[dict[str, Any]]: ...
    @abstractmethod
    def update(self, table: str, identifier: int, values: dict[str, Any]) -> dict[str, Any]: ...
    @abstractmethod
    def delete(self, table: str, identifier: int) -> None: ...
    @abstractmethod
    def transaction(self): ...


class InMemoryRepository(Repository):
    """Deterministic test double; one lock models SELECT ... FOR UPDATE workflows."""
    def __init__(self) -> None:
        self._tables: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
        self._ids: dict[str, int] = defaultdict(int)
        self._lock = RLock()

    def transaction(self):
        return self._lock

    def create(self, table: str, values: dict[str, Any]) -> dict[str, Any]:
        self._ids[table] += 1
        row = {"id": self._ids[table], **deepcopy(values)}
        self._tables[table][row["id"]] = row
        return deepcopy(row)

    def get(self, table: str, identifier: int, for_update: bool = False) -> dict[str, Any]:
        try:
            return deepcopy(self._tables[table][identifier])
        except KeyError as error:
            raise NotFoundError(table.rstrip("s")) from error

    def list(self, table: str, **filters: Any) -> list[dict[str, Any]]:
        return [deepcopy(item) for item in self._tables[table].values() if all(item.get(key) == value for key, value in filters.items() if value is not None)]

    def update(self, table: str, identifier: int, values: dict[str, Any]) -> dict[str, Any]:
        self.get(table, identifier)
        self._tables[table][identifier].update(deepcopy(values))
        return deepcopy(self._tables[table][identifier])

    def delete(self, table: str, identifier: int) -> None:
        self.get(table, identifier)
        del self._tables[table][identifier]

    def assert_unique(self, table: str, values: dict[str, Any], ignore_id: int | None = None) -> None:
        for row in self._tables[table].values():
            if row["id"] != ignore_id and all(row.get(key) == value for key, value in values.items()):
                raise ConflictError("A record with these values already exists.")


class OracleRepository(InMemoryRepository):
    """Oracle adapter seam. Production deployments replace CRUD methods with bound SQL calls.

    Transactional service code is deliberately independent of the driver; SQL migration uses
    Oracle `SELECT ... FOR UPDATE` for critical write paths.
    """


class EventPublisher(ABC):
    @abstractmethod
    def publish_after_commit(self, topic: str, payload: dict[str, Any]) -> None: ...


class NullEventPublisher(EventPublisher):
    def publish_after_commit(self, topic: str, payload: dict[str, Any]) -> None:
        return None


class CapturingPublisher(NullEventPublisher):
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
    def publish_after_commit(self, topic: str, payload: dict[str, Any]) -> None:
        self.events.append((topic, deepcopy(payload)))
