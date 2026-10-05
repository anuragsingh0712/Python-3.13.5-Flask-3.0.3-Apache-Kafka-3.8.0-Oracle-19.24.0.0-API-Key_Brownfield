"""Repository implementations and transaction-aware event publishing."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from threading import RLock
from typing import Any, Iterator

from app.core import ConflictError, DomainError, NotFoundError


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

    def after_commit(self, callback: Any) -> None:
        callback()


class InMemoryRepository(Repository):
    """Deterministic, rollback-capable test double."""
    def __init__(self) -> None:
        self._tables: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
        self._ids: dict[str, int] = defaultdict(int)
        self._lock = RLock()
        self._depth = 0
        self._callbacks: list[Any] = []

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            outer = self._depth == 0
            snapshot = deepcopy((self._tables, self._ids)) if outer else None
            if outer:
                self._callbacks = []
            self._depth += 1
            try:
                yield
            except Exception:
                self._depth -= 1
                if outer and snapshot is not None:
                    self._tables, self._ids = snapshot
                    self._callbacks = []
                raise
            else:
                self._depth -= 1
                if outer:
                    callbacks, self._callbacks = self._callbacks, []
                    for callback in callbacks:
                        callback()

    def after_commit(self, callback: Any) -> None:
        if self._depth:
            self._callbacks.append(callback)
        else:
            callback()

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
        return [deepcopy(item) for item in self._tables[table].values()
                if all(item.get(key) == value for key, value in filters.items() if value is not None)]

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


# Logical names are explicitly mapped: no client-controlled SQL identifiers are ever interpolated.
_TABLES: dict[str, tuple[str, dict[str, str]]] = {
    "members": ("MEMBERS", {"id":"id","name":"name","email":"email","phone":"phone","dob":"dob","address":"address","emergency_contact":"emergency_contact","joining_date":"joining_date","status":"status"}),
    "membership_plans": ("MEMBERSHIP_PLANS", {"id":"id","name":"name","duration_days":"duration_days","price":"price","visit_limit":"visit_limit","is_active":"is_active","active":"is_active"}),
    "memberships": ("MEMBERSHIPS", {"id":"id","member_id":"member_id","plan_id":"plan_id","start_date":"start_date","starts_on":"start_date","end_date":"end_date","ends_on":"end_date","price_paid":"price_paid","price_snapshot":"price_paid","remaining_visits":"remaining_visits","status":"status","visit_limit_snapshot":"remaining_visits"}),
    "trainers": ("TRAINERS", {"id":"id","name":"name","email":"email","phone":"phone","specialization":"specialization","status":"status","max_members":"max_members"}),
    "trainer_assignments": ("TRAINER_ASSIGNMENTS", {"id":"id","trainer_id":"trainer_id","member_id":"member_id","assigned_at":"assigned_at"}),
    "attendance": ("ATTENDANCE", {"id":"id","member_id":"member_id","membership_id":"membership_id","check_in_at":"check_in_at","check_out_at":"check_out_at","duration_minutes":"duration_minutes"}),
    "workout_plans": ("WORKOUT_PLANS", {"id":"id","member_id":"member_id","trainer_id":"trainer_id","name":"name","description":"description","created_at":"created_at","updated_at":"updated_at"}),
    "workout_exercises": ("WORKOUT_EXERCISES", {"id":"id","workout_plan_id":"workout_plan_id","name":"name","sets":"sets","repetitions":"repetitions","weight":"weight","duration_seconds":"duration_seconds","rest_seconds":"rest_seconds","difficulty":"difficulty"}),
    "gym_classes": ("GYM_CLASSES", {"id":"id","name":"name","trainer_id":"trainer_id","scheduled_at":"scheduled_at","capacity":"capacity","status":"status"}),
    "class_enrollments": ("CLASS_ENROLLMENTS", {"id":"id","class_id":"class_id","gym_class_id":"class_id","member_id":"member_id","enrolled_at":"enrolled_at","status":"status"}),
    "payments": ("PAYMENTS", {"id":"id","member_id":"member_id","membership_id":"membership_id","amount":"amount","method":"method","reference_id":"reference_id","status":"status","refunded_amount":"refunded_amount","created_at":"created_at"}),
}


class OracleRepository(Repository):
    """Oracle implementation using a driver pool, bind variables, and real transactions."""
    def __init__(self, pool: Any) -> None:
        self.pool = pool
        self._connection: Any | None = None
        self._depth = 0
        self._callbacks: list[Any] = []

    @staticmethod
    def _definition(table: str) -> tuple[str, dict[str, str]]:
        try:
            return _TABLES[table]
        except KeyError as error:
            raise DomainError("invalid_resource", "Unknown repository table.", 400) from error

    @staticmethod
    def _translate(error: Exception) -> DomainError:
        code = getattr(error, "code", None)
        message = str(error)
        if code in {1, 2291, 2292} or "ORA-00001" in message or "ORA-02291" in message:
            return ConflictError("The requested change conflicts with existing data.")
        return DomainError("database_error", "Database operation failed.", 500)

    def _connection_for_operation(self) -> tuple[Any, bool]:
        if self._connection is not None:
            return self._connection, False
        return self.pool.acquire(), True

    @staticmethod
    def _row(cursor: Any, row: Any) -> dict[str, Any]:
        return {description[0].lower(): value for description, value in zip(cursor.description, row)}

    @staticmethod
    def _logical_row(table: str, row: dict[str, Any]) -> dict[str, Any]:
        # Transitional aliases preserve existing resource seams while the database
        # itself remains strictly on the canonical migration names.
        aliases = {"membership_plans": {"is_active": "active"}, "memberships": {"start_date": "starts_on", "end_date": "ends_on", "price_paid": "price_snapshot"}, "class_enrollments": {"class_id": "gym_class_id"}}
        for database_name, logical_name in aliases.get(table, {}).items():
            if database_name in row:
                row[logical_name] = row[database_name]
        return row

    @staticmethod
    def _items(columns: dict[str, str], values: dict[str, Any]) -> list[tuple[str, str]]:
        if any(key not in columns or key == "id" for key in values):
            raise DomainError("invalid_resource", "Unsupported database column.", 400)
        seen: set[str] = set()
        return [(key, column) for key, column in ((key, columns[key]) for key in values)
                if not (column in seen or seen.add(column))]

    @contextmanager
    def transaction(self) -> Iterator[None]:
        outer = self._depth == 0
        if outer:
            self._connection = self.pool.acquire()
            self._callbacks = []
        self._depth += 1
        try:
            yield
        except Exception:
            self._depth -= 1
            if outer:
                try: self._connection.rollback()
                finally:
                    self._connection.close(); self._connection = None; self._callbacks = []
            raise
        else:
            self._depth -= 1
            if outer:
                connection, self._connection = self._connection, None
                callbacks, self._callbacks = self._callbacks, []
                try:
                    connection.commit()
                except Exception as error:
                    connection.rollback()
                    raise self._translate(error) from error
                finally:
                    connection.close()
                for callback in callbacks:
                    callback()

    def after_commit(self, callback: Any) -> None:
        if self._depth:
            self._callbacks.append(callback)
        else:
            callback()

    def _execute(self, sql: str, binds: dict[str, Any] | None = None, *, fetch: bool = False) -> list[dict[str, Any]]:
        connection, temporary = self._connection_for_operation()
        cursor = connection.cursor()
        try:
            cursor.execute(sql, binds or {})
            result = [self._row(cursor, row) for row in cursor.fetchall()] if fetch else []
            if temporary:
                connection.commit()
            return result
        except Exception as error:
            if temporary: connection.rollback()
            raise self._translate(error) from error
        finally:
            cursor.close()
            if temporary: connection.close()

    def create(self, table: str, values: dict[str, Any]) -> dict[str, Any]:
        physical, columns = self._definition(table)
        items = self._items(columns, values)
        names = ", ".join(column.upper() for _, column in items)
        binds = {f"v{i}": values[key] for i, (key, _) in enumerate(items)}
        placeholders = ", ".join(f":v{i}" for i in range(len(items)))
        connection, temporary = self._connection_for_operation()
        cursor = connection.cursor()
        try:
            # cursor.var is the supported python-oracledb RETURNING target and avoids
            # unsafe MAX(id) races under concurrent inserts.
            new_id = cursor.var(int)
            cursor.execute(f"INSERT INTO {physical} ({names}) VALUES ({placeholders}) RETURNING id INTO :new_id", {**binds, "new_id": new_id})
            identifier = new_id.getvalue()
            if isinstance(identifier, list):
                identifier = identifier[0]
            if temporary: connection.commit()
        except Exception as error:
            if temporary: connection.rollback()
            raise self._translate(error) from error
        finally:
            cursor.close()
            if temporary: connection.close()
        return self.get(table, int(identifier))

    def get(self, table: str, identifier: int, for_update: bool = False) -> dict[str, Any]:
        physical, _ = self._definition(table)
        rows = self._execute(f"SELECT * FROM {physical} WHERE id = :id" + (" FOR UPDATE" if for_update else ""), {"id": identifier}, fetch=True)
        if not rows: raise NotFoundError(table.rstrip("s"))
        return self._logical_row(table, rows[0])

    def list(self, table: str, **filters: Any) -> list[dict[str, Any]]:
        physical, columns = self._definition(table)
        clauses, binds = [], {}
        for index, (key, value) in enumerate(filters.items()):
            if value is None: continue
            if key not in columns: raise DomainError("invalid_resource", "Unsupported database column.", 400)
            clauses.append(f"{columns[key].upper()} = :f{index}"); binds[f"f{index}"] = value
        rows = self._execute(f"SELECT * FROM {physical}" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id", binds, fetch=True)
        return [self._logical_row(table, row) for row in rows]

    def update(self, table: str, identifier: int, values: dict[str, Any]) -> dict[str, Any]:
        physical, columns = self._definition(table)
        items = self._items(columns, values)
        if not items: raise DomainError("invalid_resource", "Unsupported database column.", 400)
        binds = {f"v{i}": values[key] for i, (key, _) in enumerate(items)} | {"id": identifier}
        self._execute(f"UPDATE {physical} SET " + ", ".join(f"{column.upper()} = :v{i}" for i, (_, column) in enumerate(items)) + " WHERE id = :id", binds)
        return self.get(table, identifier)

    def delete(self, table: str, identifier: int) -> None:
        physical, _ = self._definition(table)
        self.get(table, identifier, for_update=True)
        self._execute(f"DELETE FROM {physical} WHERE id = :id", {"id": identifier})

    def assert_unique(self, table: str, values: dict[str, Any], ignore_id: int | None = None) -> None:
        matches = self.list(table, **values)
        if any(row["id"] != ignore_id for row in matches): raise ConflictError("A record with these values already exists.")


class EventPublisher(ABC):
    @abstractmethod
    def publish_after_commit(self, topic: str, payload: dict[str, Any]) -> None: ...
    def close(self) -> None: return None


class NullEventPublisher(EventPublisher):
    def publish_after_commit(self, topic: str, payload: dict[str, Any]) -> None: return None


class CapturingPublisher(NullEventPublisher):
    def __init__(self) -> None: self.events: list[tuple[str, dict[str, Any]]] = []
    def publish_after_commit(self, topic: str, payload: dict[str, Any]) -> None: self.events.append((topic, deepcopy(payload)))


class TransactionalPublisher(EventPublisher):
    """Defers delivery until its repository commits; rollback discards callbacks."""
    def __init__(self, repository: Repository, delegate: EventPublisher) -> None:
        self.repository, self.delegate = repository, delegate
    def publish_after_commit(self, topic: str, payload: dict[str, Any]) -> None:
        saved = deepcopy(payload)
        self.repository.after_commit(lambda: self.delegate.publish_after_commit(topic, saved))
    def close(self) -> None: self.delegate.close()
