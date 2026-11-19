"""A disposable PostgreSQL server for the tests that need a real database.

Set TEST_DATABASE_URL to use an existing empty database. Otherwise a private server is created in a
temporary folder from the PostgreSQL binaries on this machine, on a free port, and removed afterwards.
It never touches any other PostgreSQL server. Without PostgreSQL those tests are skipped.
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from app.db.migrate import apply_migrations


def _postgres_binaries() -> tuple[str, str] | None:
    folders = [
        *sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin"), reverse=True),
        *sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    for candidate in (shutil.which("initdb"), *[str(Path(folder) / "initdb") for folder in folders]):
        if not candidate:
            continue
        initdb = Path(candidate)
        for path in (initdb, initdb.with_suffix(".exe")):
            if path.exists():
                pg_ctl = path.with_name(path.name.replace("initdb", "pg_ctl"))
                if pg_ctl.exists():
                    return str(path), str(pg_ctl)
    return None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _quiet(*command: str) -> None:
    # No inherited pipes: the server outlives this call and must not hold the test run's output open.
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)


@pytest.fixture(scope="session")
def postgres_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    configured = os.environ.get("TEST_DATABASE_URL")
    if configured:
        apply_migrations(configured)
        yield configured
        return
    binaries = _postgres_binaries()
    if binaries is None:
        pytest.skip("PostgreSQL isn't installed and TEST_DATABASE_URL isn't set.")
    initdb, pg_ctl = binaries
    root = tmp_path_factory.mktemp("postgres")
    data = root / "data"
    port = _free_port()
    _quiet(initdb, "-D", str(data), "-U", "chatreceipt", "--auth=trust", "-E", "UTF8", "--no-locale")
    options = f"-p {port} -c listen_addresses=127.0.0.1 -c fsync=off -c synchronous_commit=off -c full_page_writes=off"
    _quiet(pg_ctl, "-D", str(data), "-o", options, "-l", str(root / "server.log"), "-w", "-t", "60", "start")
    url = f"postgresql://chatreceipt@127.0.0.1:{port}/postgres"
    try:
        apply_migrations(url)
        yield url
    finally:
        _quiet(pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop")


@pytest.fixture
def clean_database(postgres_url: str) -> str:
    """The database URL, with every table emptied."""
    with psycopg.connect(postgres_url, autocommit=True) as connection:
        connection.execute(
            """
            TRUNCATE chatreceipt.customers, chatreceipt.whatsapp_outbound, chatreceipt.storage_erasures,
                     chatreceipt.worker_heartbeats
            CASCADE
            """
        )
    return postgres_url
