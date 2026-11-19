"""Create an API account and a session token for the /api/v1 endpoints, or revoke a token.

usage (from backend/, with DATABASE_URL set):
    python -m scripts.api_session create demo-shop            # account "demo-shop", 12-hour session
    python -m scripts.api_session create demo-shop --hours 72
    python -m scripts.api_session revoke                      # asks for the token to revoke

API accounts are customers identified by an operator-chosen reference instead of a WhatsApp number,
so their records stay separate from every WhatsApp customer's. The token is printed once: only its
hash is stored, so it can't be shown again. Treat it like a password. Sessions use the same table,
expiry and revocation as website sign-ins.
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from uuid import UUID

from app.config import get_settings
from app.db.pool import Connection, Database
from app.whatsapp import store

_REF = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


def account(conn: Connection, reference: str) -> tuple[UUID, bool]:
    """The customer for this reference, created when it doesn't exist yet. Returns (id, created)."""
    row = conn.execute(
        """
        INSERT INTO chatreceipt.customers (external_ref) VALUES (%s)
        ON CONFLICT (external_ref) DO NOTHING
        RETURNING id
        """,
        (reference,),
    ).fetchone()
    if row is not None:
        return row["id"], True
    existing = conn.execute("SELECT id FROM chatreceipt.customers WHERE external_ref = %s", (reference,)).fetchone()
    assert existing is not None
    return existing["id"], False


def create(database: Database, reference: str, hours: int) -> tuple[str, str, bool]:
    with database.transaction() as conn:
        customer_id, created = account(conn, reference)
        # The same path as a website sign-in: a one-time code, exchanged at once for a session.
        code = store.issue_sign_in_code(conn, customer_id, ttl_minutes=1)
        session = store.exchange_sign_in_code(conn, code, session_ttl_hours=hours)
    assert session is not None
    token, expires_at = session
    return token, expires_at.isoformat(timespec="minutes"), created


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("create", help="Create (or reuse) an account and print a new session token.")
    make.add_argument("reference", help="Account reference: 3-64 lowercase letters, digits, dots, dashes or underscores.")
    make.add_argument("--hours", type=int, default=12, help="How long the session lasts (1-720 hours, default 12).")
    commands.add_parser("revoke", help="Revoke a session token (read from the prompt, not the command line).")
    args = parser.parse_args()

    settings = get_settings()
    if settings.database_url is None:
        print("DATABASE_URL isn't set.", file=sys.stderr)
        return 2
    database = Database(settings.database_url.get_secret_value(), min_size=0, max_size=1)
    database.open()
    try:
        if args.command == "create":
            if not _REF.match(args.reference):
                print("The reference must be 3-64 lowercase letters, digits, dots, dashes or underscores.", file=sys.stderr)
                return 2
            if not 1 <= args.hours <= 720:
                print("--hours must be between 1 and 720.", file=sys.stderr)
                return 2
            token, expires, created = create(database, args.reference, args.hours)
            print(f"{'Created' if created else 'Using existing'} account {args.reference!r}.")
            print(f"Session token (shown once, valid until {expires} UTC):\n\n    {token}\n")
            print('Use it as:  Authorization: Bearer <token>')
        else:
            token = getpass.getpass("Token to revoke: ").strip()
            with database.transaction() as conn:
                store.revoke_session(conn, token)
            print("Revoked, if that token existed.")
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
