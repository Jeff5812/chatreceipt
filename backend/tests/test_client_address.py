"""Which address a request is counted against for rate limits, directly and behind a proxy."""

from __future__ import annotations

from starlette.requests import Request

from app.rate_limit import client_key


def request(headers: dict[str, str], client: str = "10.0.0.9") -> Request:
    raw = [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw, "client": (client, 443)})


def test_without_a_trusted_proxy_the_connecting_address_counts() -> None:
    forged = request({"X-Forwarded-For": "1.2.3.4"})
    assert client_key(forged, trust_proxy_headers=False) == "10.0.0.9"


def test_behind_a_proxy_a_visitor_cannot_choose_their_address() -> None:
    # The visitor wrote "1.2.3.4"; the proxy appended the address that really connected.
    forged = request({"X-Forwarded-For": "1.2.3.4, 102.89.1.7"})
    assert client_key(forged, trust_proxy_headers=True) == "102.89.1.7"


def test_a_header_the_proxy_sets_itself_wins() -> None:
    behind_cloudflare = request({"X-Forwarded-For": "1.2.3.4, 172.70.0.1", "CF-Connecting-IP": "102.89.1.7"})
    assert client_key(behind_cloudflare, trust_proxy_headers=True, client_ip_header="cf-connecting-ip") == "102.89.1.7"
    missing = request({"X-Forwarded-For": "102.89.1.7"})
    assert client_key(missing, trust_proxy_headers=True, client_ip_header="cf-connecting-ip") == "102.89.1.7"
