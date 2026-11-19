"""Apply the SQL migrations in ``app/db/migrations`` in order.

usage (from backend/): python -m app.db.migrate

Each file runs once, in its own transaction, and is recorded with a checksum. Changing a migration
that has already run is refused: add a new file instead. An advisory lock stops two deployments
from migrating at the same time.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg

from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_LOCK_ID = 7_210_443_118


class MigrationError(Exception):
    pass


def apply_migrations(conninfo: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations and return the names of those applied."""
    applied: list[str] = []
    with psycopg.connect(conninfo, autocommit=True, prepare_threshold=None) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
        try:
            connection.execute("CREATE SCHEMA IF NOT EXISTS chatreceipt")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS chatreceipt.schema_migrations (
                    name text PRIMARY KEY,
                    checksum text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            done = dict(connection.execute("SELECT name, checksum FROM chatreceipt.schema_migrations").fetchall())
            for path in sorted(directory.glob("*.sql")):
                sql = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                if path.name in done:
                    if done[path.name] != checksum:
                        raise MigrationError(f"{path.name} changed after it was applied. Add a new migration instead.")
                    continue
                with connection.transaction():
                    connection.execute(sql.encode("utf-8"))
                    connection.execute(
                        "INSERT INTO chatreceipt.schema_migrations (name, checksum) VALUES (%s, %s)",
                        (path.name, checksum),
                    )
                applied.append(path.name)
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
    return applied


def main() -> None:
    settings = get_settings()
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL isn't set.")
    names = apply_migrations(settings.database_url.get_secret_value())
    print("Applied: " + ", ".join(names) if names else "The database is up to date.")


if __name__ == "__main__":
    main()
