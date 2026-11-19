"""The connection pool.

Blocking psycopg is used on purpose: its async mode can't run on Windows' default event loop, and
every query here is short. Async code calls these methods through ``asyncio.to_thread``.

Prepared statements are turned off so the same code works through a transaction-mode pooler such as
Supabase's. No network call may ever happen inside a transaction.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

Connection = psycopg.Connection[dict[str, Any]]


class DatabaseUnavailable(Exception):
    """The database can't be reached right now. Callers should retry later."""


class Database:
    def __init__(
        self,
        conninfo: str,
        *,
        min_size: int = 1,
        max_size: int = 8,
        statement_timeout_ms: int = 15_000,
        acquire_timeout_seconds: float = 8.0,
    ) -> None:
        self._statement_timeout_ms = int(statement_timeout_ms)
        self._pool: ConnectionPool[Connection] = ConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            timeout=acquire_timeout_seconds,
            open=False,
            kwargs={"row_factory": dict_row, "prepare_threshold": None, "connect_timeout": 5},
        )

    def open(self) -> None:
        # Don't wait: the API must start even while the database is briefly down.
        self._pool.open(wait=False)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """A connection in a transaction: committed on success, rolled back on any exception."""
        try:
            with self._pool.connection() as connection:
                connection.execute(f"SET LOCAL statement_timeout = {self._statement_timeout_ms}")
                yield connection
        except (PoolTimeout, psycopg.OperationalError) as exc:
            raise DatabaseUnavailable(type(exc).__name__) from exc

    @contextmanager
    def tenant(self, customer_id: UUID) -> Iterator[Connection]:
        """A transaction in which row-level security limits every query to one customer's rows.

        The application's own filters are the first line of defence; this is the second, so a query
        that forgets its filter still can't return or change another customer's data.
        """
        with self.transaction() as connection:
            connection.execute("SET LOCAL ROLE chatreceipt_tenant")
            connection.execute("SELECT set_config('chatreceipt.customer_id', %s, true)", (str(customer_id),))
            yield connection

    def ping(self) -> bool:
        try:
            with self.transaction() as connection:
                connection.execute("SELECT 1")
            return True
        except DatabaseUnavailable:
            return False
