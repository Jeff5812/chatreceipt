"""Replay the first real phone test through the current WhatsApp channel and print the conversation.

usage (from backend/): python -m evals.whatsapp_transcript

The screenshot's reading is fixed to what the AI returned in that test (a ₦20,000 transfer taken as
the price), so the replay shows how the channel now handles it. The customer's typed messages are
read by the real, configured AI provider (one request each). Uses a private, temporary PostgreSQL
server and a fake WhatsApp API: nothing is sent to anyone and no real data is touched.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings
from app.db.migrate import apply_migrations
from app.extraction.provider import ProviderResponse, ProviderUsage
from app.extraction.providers import create_provider
from tests.conftest import _free_port, _postgres_binaries, _quiet
from tests.samples import FakeRenderer, bag_conversation_output, field, image_bytes, line_item
from tests.whatsapp_harness import ALICE, FakeGraph, open_channel


class ReadAsInTheTest:
    """The screenshot reads as it did in the real test; typed messages go to the real AI."""

    name = "gemini"
    configured = True

    def __init__(self, real: Any) -> None:
        self.real = real

    async def extract(self, images: Any) -> ProviderResponse:
        data = bag_conversation_output(
            seller=field("Bola Studio"), buyer=field("Emeka Daniel Obi"), transaction_date=field("2026-11-10"),
            line_items=[line_item("Transfer", "1", "20000")], delivery_fee=field(""), amount_paid=field("20000"),
            payment_status=field("paid"), payment_terms=field(""),
            ambiguities=[{"field": "agreed_total", "description": "The buyer said “No” when asked if ₦20,000 was the full amount."}],
        )
        return ProviderResponse(data=data, usage=ProviderUsage(model="as-in-the-test"), duration_ms=1)

    async def generate(self, request: Any) -> ProviderResponse:
        return await self.real.generate(request)

    async def aclose(self) -> None:
        await self.real.aclose()


def show(graph: FakeGraph, start: int) -> int:
    for message in graph.sent[start:]:
        body = FakeGraph.body_of(message)
        buttons = list(FakeGraph.options_of(message))
        kind = message["type"]
        print("ChatReceipt:" + (f" [{kind}]" if kind in ("image", "document") else ""))
        print("  " + body.replace("\n", "\n  "))
        if buttons:
            print("  [" + "] [".join(buttons) + "]")
        print()
    return len(graph.sent)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    binaries = _postgres_binaries()
    if binaries is None:
        raise SystemExit("PostgreSQL isn't installed.")
    initdb, pg_ctl = binaries
    real = create_provider(get_settings())
    if not getattr(real, "configured", False):
        raise SystemExit("No AI provider is configured.")
    with tempfile.TemporaryDirectory() as folder:
        data, port = Path(folder) / "data", _free_port()
        _quiet(initdb, "-D", str(data), "-U", "chatreceipt", "--auth=trust", "-E", "UTF8", "--no-locale")
        _quiet(pg_ctl, "-D", str(data), "-o", f"-p {port} -c listen_addresses=127.0.0.1 -c fsync=off", "-l", str(Path(folder) / "log"), "-w", "start")
        url = f"postgresql://chatreceipt@127.0.0.1:{port}/postgres"
        try:
            apply_migrations(url)
            client, channel = open_channel(url, ReadAsInTheTest(real), renderer=FakeRenderer())
            channel.graph.add_image("media-1", image_bytes())
            seen = 0
            steps: list[tuple[str, str]] = [
                ("say", "Hi"), ("image", "media-1"), ("say", "can you try again"),
                ("say", "It's an advance work. 40k was still remaining"), ("tap", "Yes, ₦60,000"), ("tap", "Confirm"),
            ]
            for kind, value in steps:
                print(f"Customer: {'[screenshot]' if kind == 'image' else ('[taps] ' + value if kind == 'tap' else value)}\n")
                if kind == "say":
                    channel.say(ALICE, value)
                elif kind == "image":
                    channel.send_image(ALICE, value)
                else:
                    channel.tap(ALICE, value)
                seen = show(channel.graph, seen)
            client.__exit__(None, None, None)
        finally:
            _quiet(pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop")


if __name__ == "__main__":
    main()
