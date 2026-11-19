"""The operator command that creates API accounts and session tokens."""

from __future__ import annotations

from app.db.pool import Database
from app.whatsapp import store
from scripts import api_session


def test_an_api_account_gets_a_working_session_and_keeps_its_records_separate(clean_database: str) -> None:
    database = Database(clean_database)
    database.open()
    try:
        token, expires, created = api_session.create(database, "demo-shop", hours=2)
        again, _, created_again = api_session.create(database, "demo-shop", hours=2)
        assert created and not created_again
        assert token != again and expires
        with database.transaction() as conn:
            first = store.session_customer(conn, token)
            assert first is not None and store.session_customer(conn, again) == first
            row = conn.execute("SELECT external_ref, whatsapp_user_id, whatsapp_phone FROM chatreceipt.customers WHERE id = %s", (first,)).fetchone()
            assert row == {"external_ref": "demo-shop", "whatsapp_user_id": None, "whatsapp_phone": None}
            stored = conn.execute("SELECT token_hash FROM chatreceipt.web_sessions").fetchall()
            assert token not in {r["token_hash"] for r in stored}  # only the hash is kept
            store.revoke_session(conn, token)
            assert store.session_customer(conn, token) is None
            assert store.session_customer(conn, again) == first
        other, _, _ = api_session.create(database, "another-shop", hours=1)
        with database.transaction() as conn:
            assert store.session_customer(conn, other) != first
    finally:
        database.close()
