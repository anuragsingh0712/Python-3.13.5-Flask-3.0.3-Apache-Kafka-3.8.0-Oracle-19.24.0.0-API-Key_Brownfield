"""Production Oracle repository factory (driver import remains lazy for test environments)."""
from __future__ import annotations

from typing import Any

from app.core import Settings
from app.repository import OracleRepository


def create_oracle_repository(settings: Settings) -> OracleRepository:
    import oracledb

    pool: Any = oracledb.create_pool(
        user=settings.oracle_username,
        password=settings.oracle_password,
        dsn=settings.oracle_dsn,
        min=1,
        max=8,
        increment=1,
    )
    return OracleRepository(pool)
