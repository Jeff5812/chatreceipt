"""Try the WhatsApp channel locally without Meta: a fake Cloud API and signed webhook deliveries.

Start the API with WHATSAPP_GRAPH_BASE_URL=http://127.0.0.1:8099 (see docs/whatsapp-integration.md), then
from backend/:

    python -m scripts.whatsapp_sim chat                       type messages by hand
    python -m scripts.whatsapp_sim demo --image shot.png      run the whole journey once

In chat, /image PATH sends a screenshot, /tap TITLE taps a button or list row, and /quit leaves.
Everything else is sent as a text message.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import itertools
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

from app.config import get_settings

FAKE_PORT = 8099


class FakeCloudAPI:
    """Receives what the backend sends to WhatsApp, and serves the images it asks for."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.media: dict[str, bytes] = {}
        self._ids = itertools.count(1)
        self.app = FastAPI()
        self.app.post("/{version}/{phone_id}/messages")(self._send)
        self.app.post("/{version}/{phone_id}/media")(self._upload)
        self.app.get("/media/{media_id}")(self._download)
        self.app.get("/{version}/{media_id}")(self._lookup)

    async def _send(self, request: Request) -> dict[str, Any]:
        body = await request.json()
        if body.get("status") != "read":
            self.sent.append(body)
            show(body)
        return {"messaging_product": "whatsapp", "messages": [{"id": f"wamid.sim{next(self._ids)}"}]}

    async def _upload(self) -> dict[str, str]:
        return {"id": f"sim-upload-{next(self._ids)}"}

    async def _lookup(self, media_id: str) -> Any:
        data = self.media.get(media_id)
        if data is None:
            return Response(status_code=404)
        return {
            "messaging_product": "whatsapp",
            "url": f"http://127.0.0.1:{FAKE_PORT}/media/{media_id}",
            "mime_type": "image/png",
            "file_size": len(data),
            "id": media_id,
        }

    async def _download(self, media_id: str) -> Response:
        data = self.media.get(media_id)
        return Response(data, media_type="image/png") if data is not None else Response(status_code=404)

    def start(self) -> None:
        server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=FAKE_PORT, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(100):
            if server.started:
                return
            time.sleep(0.05)
        raise SystemExit(f"Couldn't start the fake WhatsApp API on port {FAKE_PORT}.")


def options_of(message: dict[str, Any]) -> dict[str, str]:
    if message.get("type") != "interactive":
        return {}
    action = message["interactive"]["action"]
    if "buttons" in action:
        return {b["reply"]["title"]: b["reply"]["id"] for b in action["buttons"]}
    return {row["title"]: row["id"] for section in action["sections"] for row in section["rows"]}


def body_of(message: dict[str, Any]) -> str:
    kind = message.get("type")
    if kind in ("image", "document"):
        return f"[{kind} attached]\n{message[kind]['caption']}"
    return message["text"]["body"] if kind == "text" else message["interactive"]["body"]["text"]


def show(message: dict[str, Any]) -> None:
    print(f"\n--- ChatReceipt ---\n{body_of(message)}")
    options = options_of(message)
    if options:
        print("[" + "] [".join(options) + "]")


class Customer:
    """One simulated person messaging the business number."""

    def __init__(self, api: FakeCloudAPI, webhook_url: str) -> None:
        self.api = api
        self.settings = get_settings()
        self.webhook_url = webhook_url
        self.http = httpx.Client(timeout=15)
        self._ids = itertools.count(int(time.time()))
        self._read = 0

    def _deliver(self, kind: str, **fields: Any) -> None:
        number = next(self._ids)
        message = {
            "from": "2348000000001",
            "from_user_id": "NG.1000000000000000001",
            "id": f"wamid.simulated{number}",
            "timestamp": str(int(time.time())),
            "type": kind,
            **fields,
        }
        body = json.dumps(
            {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "id": "simulated",
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "messaging_product": "whatsapp",
                                    "metadata": {"display_phone_number": "0", "phone_number_id": self.settings.whatsapp_phone_number_id},
                                    "messages": [message],
                                },
                            }
                        ],
                    }
                ],
            }
        ).encode()
        secret = self.settings.whatsapp_app_secret.get_secret_value()  # type: ignore[union-attr]
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        response = self.http.post(
            self.webhook_url, content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature}
        )
        if response.status_code != 200:
            raise SystemExit(f"The webhook answered {response.status_code}: {response.text}")

    def text(self, body: str) -> None:
        self._deliver("text", text={"body": body})

    def image(self, path: Path) -> None:
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        media_id = f"media-{digest[:16]}"
        self.api.media[media_id] = data
        self._deliver("image", image={"id": media_id, "mime_type": "image/png", "sha256": digest})

    def tap(self, title: str) -> None:
        for message in reversed(self.api.sent):
            options = options_of(message)
            # Some titles carry a date, such as "Today · 19 Nov": the start of a title is enough.
            option = options.get(title) or next((value for name, value in options.items() if name.startswith(title)), None)
            if option:
                self._deliver("interactive", interactive={"type": "button_reply", "button_reply": {"id": option, "title": title}})
                return
        print(f"No message offers “{title}”.")

    def wait_for(self, *phrases: str, timeout: float = 240) -> dict[str, Any]:
        """Wait for the next reply containing one of these phrases."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while self._read < len(self.api.sent):
                message = self.api.sent[self._read]
                self._read += 1
                if any(phrase in body_of(message) for phrase in phrases):
                    return message
            time.sleep(0.2)
        raise SystemExit(f"Timed out waiting for a reply containing {phrases}.")


def chat(customer: Customer) -> None:
    print(__doc__)
    for line in sys.stdin:
        text = line.strip()
        if text == "/quit":
            return
        if text.startswith("/image "):
            customer.image(Path(text.removeprefix("/image ").strip()))
        elif text.startswith("/tap "):
            customer.tap(text.removeprefix("/tap ").strip())
        elif text:
            customer.text(text)


def demo(customer: Customer, image: Path) -> None:
    def step(title: str) -> None:
        print(f"\n===== {title} =====", flush=True)

    step("Customer says hello")
    customer.text("Hi ChatReceipt, I'd like to turn a screenshot into a record.")
    customer.wait_for("Hi, I'm ChatReceipt", "What would you like to do?")

    step("Customer sends a screenshot")
    customer.image(image)
    customer.wait_for("Here's what I found")

    step("Customer corrects the seller")
    customer.tap("Correct")
    customer.wait_for("What needs correcting?")
    customer.tap("Seller")
    customer.wait_for("seller's name")
    customer.text("Tola Bags Ltd")
    customer.wait_for("Updated.")

    step("Customer confirms")
    customer.tap("Confirm")
    if "What date was this?" in body_of(customer.wait_for("Saved as", "What date was this?")):
        customer.tap("Today")
        customer.wait_for("Saved as")

    step("Customer asks for recent records")
    customer.text("recent")
    customer.wait_for("Your recent records")

    step("Customer asks for the website link")
    customer.text("web")
    customer.wait_for("Open your records here")
    time.sleep(1)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description="Simulate a WhatsApp customer against a local ChatReceipt API.")
    parser.add_argument("mode", choices=("chat", "demo"))
    parser.add_argument("--image", type=Path, help="Screenshot for the demo.")
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="Where the ChatReceipt API is running.")
    args = parser.parse_args()
    api = FakeCloudAPI()
    api.start()
    customer = Customer(api, f"{args.api.rstrip('/')}/api/whatsapp/webhook")
    if args.mode == "chat":
        chat(customer)
    else:
        if args.image is None:
            parser.error("demo needs --image")
        demo(customer, args.image)


if __name__ == "__main__":
    main()
