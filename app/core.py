"""Cross-cutting configuration, errors, validation, and API response helpers."""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping

from flask import Flask, Response, g, jsonify, request
from pydantic import BaseModel, ValidationError

LOG = logging.getLogger(__name__)


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class NotFoundError(DomainError):
    def __init__(self, resource: str) -> None:
        super().__init__("not_found", f"{resource} was not found.", 404)


class ConflictError(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__("conflict", message, 409)


class ForbiddenError(DomainError):
    def __init__(self) -> None:
        super().__init__("forbidden", "You are not authorized for this action.", 403)


@dataclass(frozen=True)
class Settings:
    api_keys: Mapping[str, frozenset[str]]
    oracle_dsn: str
    oracle_username: str
    oracle_password: str
    kafka_servers: str
    kafka_client_id: str

    @classmethod
    def from_env(cls) -> "Settings":
        raw = os.getenv("API_KEY", "{}")
        try:
            keys = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("API_KEY must be a JSON role mapping.") from error
        allowed_roles = {"ADMIN", "STAFF", "TRAINER"}
        if (not isinstance(keys, dict) or any(
            not isinstance(key, str) or not key.strip() or not isinstance(roles, list)
            or any(not isinstance(role, str) or role.upper() not in allowed_roles for role in roles)
            for key, roles in keys.items()
        )):
            raise RuntimeError("API_KEY must map nonempty keys to nonempty ADMIN, STAFF, or TRAINER role lists.")
        return cls(
            api_keys={key: frozenset(role.upper() for role in roles) for key, roles in keys.items()},
            oracle_dsn=f"{os.getenv('ORACLE_HOST', 'oracle')}:{os.getenv('ORACLE_PORT', '1521')}/{os.getenv('ORACLE_SERVICE_NAME', 'FREEPDB1')}",
            oracle_username=os.getenv("ORACLE_USERNAME", "gym"), oracle_password=os.getenv("ORACLE_PASSWORD", ""),
            kafka_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"), kafka_client_id=os.getenv("KAFKA_CLIENT_ID", "gym-api"),
        )


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported response value: {type(value).__name__}")


def success(data: Any, status: int = 200, meta: dict[str, Any] | None = None) -> Response:
    payload: dict[str, Any] = {"data": data}
    if meta is not None:
        payload["meta"] = meta
    return Response(json.dumps(payload, default=json_default), status=status, mimetype="application/json")


def parse_model(model: type[BaseModel]) -> BaseModel:
    try:
        body = request.get_json(force=False, silent=False)
    except Exception as error:
        raise DomainError("invalid_json", "Request body must be valid JSON.", 400) from error
    if body is None:
        raise DomainError("invalid_json", "Request body must be valid JSON.", 400)
    try:
        return model.model_validate(body)
    except ValidationError as error:
        raise DomainError("validation_error", "Request data is invalid.", 422) from error


def cursor_params() -> tuple[int | None, int]:
    raw, limit_raw = request.args.get("cursor"), request.args.get("limit", "20")
    try:
        limit = int(limit_raw)
        if not 1 <= limit <= 100:
            raise ValueError
        cursor = int(base64.urlsafe_b64decode(raw.encode()).decode()) if raw else None
    except (ValueError, UnicodeError, TypeError):
        raise DomainError("validation_error", "cursor or limit is invalid.", 422) from None
    return cursor, limit


def encode_cursor(identifier: int) -> str:
    return base64.urlsafe_b64encode(str(identifier).encode()).decode()


def roles_required(*roles: str) -> Callable:
    def decorate(function: Callable) -> Callable:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            caller_roles = getattr(g, "roles", frozenset())
            if not caller_roles.intersection(roles):
                raise ForbiddenError()
            return function(*args, **kwargs)
        wrapped.__name__ = function.__name__
        return wrapped
    return decorate


def configure_app(app: Flask, settings: Settings) -> None:
    @app.before_request
    def correlation_and_auth() -> None:
        g.correlation_id = request.headers.get("X-Correlation-ID") or request.headers.get("X-Request-ID") or secrets.token_hex(12)
        if request.path in {"/health", "/docs", "/openapi.json"}:
            return

    @app.errorhandler(DomainError)
    def domain_error(error: DomainError) -> Response:
        return jsonify(error={"code": error.code, "message": error.message, "timestamp": datetime.now(timezone.utc).isoformat(), "correlation_id": g.get("correlation_id", "")}), error.status

    @app.errorhandler(Exception)
    def unexpected(error: Exception) -> Response:
        LOG.exception("Unhandled request failure correlation_id=%s", g.get("correlation_id", ""))
        return jsonify(error={"code": "internal_error", "message": "An unexpected error occurred.", "timestamp": datetime.now(timezone.utc).isoformat(), "correlation_id": g.get("correlation_id", "")}), 500
