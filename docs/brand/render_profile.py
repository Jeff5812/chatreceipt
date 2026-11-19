"""Draw the WhatsApp profile picture: Chit and the ChatReceipt wordmark on the dark ground.

usage (from the repo root): backend/.venv/Scripts/python docs/brand/render_profile.py

Uses the same pixel art and colours as frontend/components/chit.tsx and the self-hosted Tiny5 font, so
the picture matches the site. WhatsApp shows profile pictures in a circle; everything sits well inside it.
"""

from __future__ import annotations

import base64
from pathlib import Path

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
# Embedded as data: a page opened from about:blank may not load file:// fonts.
FONT = "data:font/woff2;base64," + base64.b64encode((ROOT / "frontend/public/fonts/tiny5-latin.woff2").read_bytes()).decode()

ART = [
    ".WWWWWWWWWS.",
    "WWWWWWWWWWWS",
    "WWWWWWWWWWWS",
    "WWWWWWWWWWWS",
    "WWWWWWWWWWWS",
    "WWWWWWWWWWWS",
    "WWWWWWWWWWWS",
    "WWGGGGGGGWWS",
    "WWWWWWWWWWWS",
    "WWGGGWWLLWWS",
    "WWWWWWWWWWWS",
    "SSSSSSSSSSSS",
    ".S..S..S..S.",
]
FILL = {"W": "#eef3ea", "S": "#c3ccc5", "G": "#9aa59d", "L": "#c4f25a"}
EYES = [(2, 3), (7, 3)]  # idle eyes: 2 wide, 3 tall


def chit_svg(cell: int) -> str:
    rects = []
    for y, row in enumerate(ART):
        for x, ch in enumerate(row):
            if ch in FILL:
                rects.append(f'<rect x="{x}" y="{y}" width="1" height="1" fill="{FILL[ch]}"/>')
    for x, y in EYES:
        rects.append(f'<rect x="{x}" y="{y}" width="2" height="3" fill="#0a0c0b"/>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 13" width="{12 * cell}" height="{13 * cell}" '
        f'shape-rendering="crispEdges">{"".join(rects)}</svg>'
    )


def page(size: int) -> str:
    scale = size / 640
    return f"""<!doctype html><html><head><style>
@font-face {{ font-family: Tiny5; src: url("{FONT}") format("woff2"); }}
html, body {{ margin: 0; background: #0a0c0b; }}
.tile {{ width: {size}px; height: {size}px; display: flex; flex-direction: column; align-items: center;
         justify-content: center; gap: {round(40 * scale)}px; background: #0a0c0b; }}
.word {{ font-family: Tiny5; font-size: {round(64 * scale)}px; line-height: 1; color: #e9eee9; letter-spacing: 0.01em;
         -webkit-font-smoothing: none; }}
</style></head><body><div class="tile">{chit_svg(round(20 * scale))}<div class="word">ChatReceipt</div></div></body></html>"""


def render(size: int, path: Path) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        tab = browser.new_page(viewport={"width": size, "height": size})
        tab.set_content(page(size))
        tab.evaluate("document.fonts.ready")
        if not tab.evaluate("document.fonts.check('64px Tiny5')"):
            raise SystemExit("Tiny5 didn't load; the wordmark would fall back to another font")
        tab.locator(".tile").screenshot(path=str(path))
        browser.close()


def circle_preview(source: Path, path: Path) -> None:
    image = Image.open(source).convert("RGBA")
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, image.size[0] - 1, image.size[1] - 1), fill=255)
    preview = Image.new("RGBA", image.size, (255, 255, 255, 255))
    preview.paste(image, mask=mask)
    preview.save(path)


if __name__ == "__main__":
    render(640, OUT / "whatsapp-profile.png")  # what WhatsApp gets (640 x 640 is its recommended size)
    render(1280, OUT / "whatsapp-profile-1280.png")  # the same picture at twice the size
    if "--preview" in __import__("sys").argv:  # how it looks in WhatsApp's round frame
        circle_preview(OUT / "whatsapp-profile.png", OUT / "whatsapp-profile-circle-preview.png")
    print("wrote", *(p.name for p in sorted(OUT.glob("whatsapp-profile*.png"))))
