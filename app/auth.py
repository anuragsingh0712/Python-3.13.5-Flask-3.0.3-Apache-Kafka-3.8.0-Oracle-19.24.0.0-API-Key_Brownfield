"""Constant-time API-key authentication middleware."""

from __future__ import annotations

import secrets

from flask import Flask, g, request

from app.core import DomainError, Settings


def install_api_key_auth(app: Flask, settings: Settings) -> None:
    @app.before_request
    def api_key_authentication() -> None:
        if not request.path.startswith("/api/v1/"):
            return
        supplied = request.headers.get("X-API-Key", "")
        matched: frozenset[str] | None = None
        # Do not use a dict lookup: compare every configured key before deciding.
        for configured, roles in settings.api_keys.items():
            equal = secrets.compare_digest(supplied, configured)
            if equal:
                matched = roles
        if matched is None:
            raise DomainError("unauthorized", "Authentication is required.", 401)
        g.roles = matched
