"""Render the evaluation conversations as phone-sized chat screenshots.

usage (from backend/): python -m evals.render
Writes evals/screenshots/<case>-<n>.png. Uses the browser set in BROWSER_CHANNEL, like the exports.
"""

from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

from app.config import get_settings

HERE = Path(__file__).resolve().parent
SCREENSHOTS = HERE / "screenshots"


def load_cases() -> list[dict]:
    return json.loads((HERE / "cases.json").read_text(encoding="utf-8"))


def main() -> None:
    template = Environment(loader=FileSystemLoader(HERE), autoescape=select_autoescape(["html"])).get_template("chat.html")
    SCREENSHOTS.mkdir(exist_ok=True)
    channel = get_settings().browser_channel or None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 760}, device_scale_factor=2)
        for case in load_cases():
            for index, messages in enumerate(case["screenshots"], start=1):
                html = template.render(
                    contact=case["contact"],
                    presence="online",
                    status_time=messages[-1]["time"],
                    show_day=index == 1,
                    messages=messages,
                )
                page.set_content(html, wait_until="load")
                path = SCREENSHOTS / f"{case['id']}-{index}.png"
                page.screenshot(path=str(path), full_page=True)
                print(f"wrote {path.relative_to(HERE.parent)}")
        browser.close()


if __name__ == "__main__":
    main()
