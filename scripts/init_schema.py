#!/usr/bin/env python3
"""Apply local Oracle migrations exactly once, using environment configuration."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def split_oracle_sql(source: str) -> list[str]:
    """Split current semicolon-delimited DDL without splitting quoted literals."""
    statements, buffer, quote = [], [], None
    for char in source:
        if char in {"'", '"'}:
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        if char == ";" and quote is None:
            statement = "".join(buffer).strip()
            if statement and not statement.startswith("--"):
                # migration comments may precede their first statement
                statement = "\n".join(
                    line
                    for line in statement.splitlines()
                    if not line.lstrip().startswith("--")
                ).strip()
                if statement:
                    statements.append(statement)
            buffer = []
        else:
            buffer.append(char)
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def main() -> None:
    import oracledb

    root = Path(__file__).resolve().parents[1]
    dsn = (
        os.getenv("ORACLE_DSN")
        or f"{os.getenv('ORACLE_HOST', 'oracle')}:{os.getenv('ORACLE_PORT', '1521')}/{os.getenv('ORACLE_SERVICE_NAME', 'FREEPDB1')}"
    )
    connection = oracledb.connect(
        user=os.getenv("ORACLE_USERNAME", "gym"),
        password=os.environ["ORACLE_PASSWORD"],
        dsn=dsn,
    )
    try:
        cursor = connection.cursor()
        cursor.execute(
            "CREATE TABLE SCHEMA_MIGRATIONS (name VARCHAR2(255) PRIMARY KEY, checksum VARCHAR2(64) NOT NULL, applied_at TIMESTAMP NOT NULL)"
        )
        connection.commit()
    except oracledb.DatabaseError as error:
        if "ORA-00955" not in str(error):
            raise
    finally:
        try:
            cursor.close()
        except UnboundLocalError:
            pass

    try:
        for migration in sorted((root / "migrations").glob("*.sql")):
            content = migration.read_text(encoding="utf-8")
            checksum = hashlib.sha256(content.encode()).hexdigest()
            cursor = connection.cursor()
            cursor.execute(
                "SELECT checksum FROM SCHEMA_MIGRATIONS WHERE name = :name",
                {"name": migration.name},
            )
            previous = cursor.fetchone()
            cursor.close()
            if previous:
                if previous[0] != checksum:
                    raise RuntimeError(
                        f"Migration {migration.name} changed after application."
                    )
                continue
            cursor = connection.cursor()
            try:
                for statement in split_oracle_sql(content):
                    cursor.execute(statement)
                cursor.execute(
                    "INSERT INTO SCHEMA_MIGRATIONS (name, checksum, applied_at) VALUES (:name, :checksum, SYSTIMESTAMP)",
                    {"name": migration.name, "checksum": checksum},
                )
                connection.commit()
                print(f"Applied {migration.name}")
            except Exception:
                connection.rollback()
                raise
            finally:
                cursor.close()
    finally:
        connection.close()


if __name__ == "__main__":
    main()
