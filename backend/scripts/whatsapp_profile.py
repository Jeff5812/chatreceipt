"""Set the WhatsApp number's profile picture, business profile, welcome message, suggestions and commands.

usage (from backend/, with the WHATSAPP_* settings in .env):
    python -m scripts.whatsapp_profile show
    python -m scripts.whatsapp_profile apply --picture ../docs/brand/whatsapp-profile.png
    python -m scripts.whatsapp_profile apply --website https://chatreceipt.vercel.app

The text comes from app/whatsapp/profile.py. "apply" turns on Meta's welcome message: when someone opens
the chat for the first time, Meta sends a request_welcome event and the bot answers with its welcome.
The picture is uploaded through Meta's resumable upload API, which needs the Meta app's ID; it is read
from the access token unless WHATSAPP_APP_ID is set. Tokens are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings
from app.whatsapp import profile

GRAPH = "https://graph.facebook.com"


class Meta:
    def __init__(self) -> None:
        settings = get_settings()
        if not (settings.whatsapp_access_token and settings.whatsapp_phone_number_id):
            raise SystemExit("WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID must be set.")
        token = settings.whatsapp_access_token
        self.token = token.get_secret_value() if hasattr(token, "get_secret_value") else str(token)
        self.phone = settings.whatsapp_phone_number_id
        self.base = f"{GRAPH}/{settings.whatsapp_graph_api_version}"
        self.http = httpx.Client(timeout=60, headers={"Authorization": f"Bearer {self.token}"})

    def call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.http.request(method, f"{self.base}/{path.lstrip('/')}", **kwargs)
        body = response.json() if response.content else {}
        if response.status_code >= 400:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            raise SystemExit(f"{method} {path.split('?')[0]} failed ({response.status_code}): {error.get('message', body)}")
        return body

    def app_id(self) -> str:
        configured = os.environ.get("WHATSAPP_APP_ID", "").strip()
        if configured:
            return configured
        data = self.call("GET", "debug_token", params={"input_token": self.token}).get("data", {})
        if not data.get("app_id"):
            raise SystemExit("Couldn't tell which Meta app the token belongs to; set WHATSAPP_APP_ID.")
        return str(data["app_id"])

    def upload_picture(self, path: Path) -> str:
        data = path.read_bytes()
        kind = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        session = self.call(
            "POST", f"{self.app_id()}/uploads", params={"file_name": path.name, "file_length": len(data), "file_type": kind}
        )["id"]
        response = self.http.post(
            f"{self.base}/{session}", content=data, headers={"Authorization": f"OAuth {self.token}", "file_offset": "0"}
        )
        if response.status_code >= 400 or "h" not in response.json():
            raise SystemExit(f"Uploading the picture failed ({response.status_code}).")
        return response.json()["h"]


def check_limits() -> None:
    limits = profile.LIMITS
    assert len(profile.ABOUT) <= limits["about"] and len(profile.DESCRIPTION) <= limits["description"]
    assert len(profile.PROMPTS) <= limits["prompts"] and all(len(p) <= limits["prompt"] for p in profile.PROMPTS)
    assert len(profile.COMMANDS) <= limits["commands"]
    assert all(len(n) <= limits["command_name"] and len(d) <= limits["command_description"] for n, d in profile.COMMANDS)


def show(meta: Meta) -> None:
    fields = "about,description,vertical,websites,profile_picture_url"
    business = meta.call("GET", f"{meta.phone}/whatsapp_business_profile", params={"fields": fields}).get("data", [{}])[0]
    number = meta.call("GET", meta.phone, params={"fields": "verified_name,name_status,new_name_status,display_phone_number,conversational_automation"})
    business["profile_picture_url"] = "set" if business.get("profile_picture_url") else "not set"
    print(json.dumps({"business_profile": business, "number": number}, indent=2, ensure_ascii=False))


def apply(meta: Meta, picture: Path | None, website: str | None) -> None:
    check_limits()
    body: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "about": profile.ABOUT,
        "description": profile.DESCRIPTION,
        "vertical": profile.VERTICAL,
    }
    if website:
        body["websites"] = [website]
    if picture is not None:
        body["profile_picture_handle"] = meta.upload_picture(picture)
        print("Picture uploaded.")
    meta.call("POST", f"{meta.phone}/whatsapp_business_profile", json=body)
    print("Business profile updated.")
    meta.call(
        "POST",
        f"{meta.phone}/conversational_automation",
        json={
            "enable_welcome_message": True,
            "prompts": list(profile.PROMPTS),
            "commands": [{"command_name": name, "command_description": text} for name, text in profile.COMMANDS],
        },
    )
    print("Welcome message, suggestions and commands set.")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("show", help="Print what is set now.")
    do = sub.add_parser("apply", help="Set everything from app/whatsapp/profile.py.")
    do.add_argument("--picture", type=Path, help="A square PNG or JPEG, 640x640 recommended.")
    do.add_argument("--website", help="The website address to list on the profile.")
    args = parser.parse_args()
    meta = Meta()
    if args.action == "apply":
        apply(meta, args.picture, args.website)
    show(meta)


if __name__ == "__main__":
    main()
