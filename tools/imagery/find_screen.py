"""Find a phone's blank screen in a photo, so real interface content can be placed on it.

usage: python find_screen.py <image> [--mode dark|light] [--debug out.jpg]
Works for phones seen straight-on or from directly above (the screen is a rotated rectangle, no perspective).
Prints JSON: image size, and the screen's centre, width, height and angle as fractions of the image width.
Pure Pillow, no numpy.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import deque

from PIL import Image, ImageDraw

WORK = 640  # analyse a copy this wide


def components(mask: list[list[bool]], w: int, h: int) -> list[list[tuple[int, int]]]:
    seen = [[False] * w for _ in range(h)]
    found = []
    for y in range(h):
        row = mask[y]
        for x in range(w):
            if not row[x] or seen[y][x]:
                continue
            queue = deque([(x, y)])
            seen[y][x] = True
            pixels = []
            while queue:
                cx, cy = queue.popleft()
                pixels.append((cx, cy))
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < w and 0 <= ny < h and mask[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        queue.append((nx, ny))
            if len(pixels) > w * h * 0.01:
                found.append(pixels)
    return found


def rotated_rect(pixels: list[tuple[int, int]]) -> dict:
    n = len(pixels)
    mx = sum(p[0] for p in pixels) / n
    my = sum(p[1] for p in pixels) / n
    sxx = sum((p[0] - mx) ** 2 for p in pixels) / n
    syy = sum((p[1] - my) ** 2 for p in pixels) / n
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pixels) / n
    angle = 0.5 * math.atan2(2 * sxy, sxx - syy)  # direction of the long axis
    ca, sa = math.cos(angle), math.sin(angle)
    along = sorted((p[0] - mx) * ca + (p[1] - my) * sa for p in pixels)
    across = sorted(-(p[0] - mx) * sa + (p[1] - my) * ca for p in pixels)

    def span(values: list[float]) -> tuple[float, float]:
        # Ignore the outermost 0.4% so rounded corners and stray pixels don't stretch the box.
        cut = max(1, int(len(values) * 0.004))
        return values[cut], values[-cut]

    a0, a1 = span(along)
    c0, c1 = span(across)
    # Re-centre on the box rather than the centroid.
    ox = (a0 + a1) / 2
    oy = (c0 + c1) / 2
    cx = mx + ox * ca - oy * sa
    cy = my + ox * sa + oy * ca
    long_side, short_side = a1 - a0, c1 - c0
    fill = n / max(1.0, long_side * short_side)
    # Report the angle of the long axis measured from vertical, in degrees (0 = upright phone).
    tilt = math.degrees(angle) - 90
    while tilt <= -90:
        tilt += 180
    while tilt > 90:
        tilt -= 180
    return {"cx": cx, "cy": cy, "long": long_side, "short": short_side, "tilt": tilt, "fill": fill, "area": n, "pixels": pixels}


def corner_radius(rect: dict) -> float:
    """Walk inward along each corner's diagonal until the screen starts; for a rounded corner that distance is r(sqrt2 - 1)."""
    inside = set(rect["pixels"])
    t = math.radians(rect["tilt"] + 90)
    ca, sa = math.cos(t), math.sin(t)
    hl, hs = rect["long"] / 2, rect["short"] / 2
    distances = []
    for sl, ss in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        for step in range(0, int(rect["short"] / 2)):
            d = step * 0.5
            dl, ds = sl * (hl - d / math.sqrt(2)), ss * (hs - d / math.sqrt(2))
            x = rect["cx"] + dl * ca - ds * sa
            y = rect["cy"] + dl * sa + ds * ca
            if (round(x), round(y)) in inside:
                distances.append(d)
                break
    if not distances:
        return 0.0
    distances.sort()
    return distances[len(distances) // 2] / (math.sqrt(2) - 1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--mode", choices=["dark", "light"], default="dark")
    parser.add_argument("--threshold", type=int, default=None)
    parser.add_argument("--debug", default=None)
    args = parser.parse_args()

    original = Image.open(args.image).convert("RGB")
    scale = WORK / original.width
    small = original.resize((WORK, max(1, round(original.height * scale))), Image.LANCZOS)
    w, h = small.size
    px = small.load()
    threshold = args.threshold if args.threshold is not None else (46 if args.mode == "dark" else 232)
    mask = []
    for y in range(h):
        row = []
        for x in range(w):
            r, g, b = px[x, y]
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            spread = max(r, g, b) - min(r, g, b)
            row.append((lum < threshold and spread < 40) if args.mode == "dark" else (lum > threshold and spread < 26))
        mask.append(row)

    candidates = []
    for pixels in components(mask, w, h):
        rect = rotated_rect(pixels)
        ratio = rect["long"] / max(1.0, rect["short"])
        if rect["fill"] > 0.86 and 1.6 < ratio < 2.6:
            candidates.append(rect)
    if not candidates:
        print(json.dumps({"found": False}))
        return
    best = max(candidates, key=lambda r: r["area"])
    out = {
        "found": True,
        "image": [original.width, original.height],
        "cx": round(best["cx"] / w, 5),
        "cy": round(best["cy"] / w, 5),  # also as a fraction of the WIDTH, so one unit applies to both axes
        "width": round(best["short"] / w, 5),
        "height": round(best["long"] / w, 5),
        "tilt_deg": round(best["tilt"], 2),
        "fill": round(best["fill"], 3),
        "radius": round(corner_radius(best) / w, 5),
        "others": len(candidates) - 1,
    }
    print(json.dumps(out))

    if args.debug:
        draw = ImageDraw.Draw(small)
        t = math.radians(best["tilt"] + 90)
        ca, sa = math.cos(t), math.sin(t)
        hl, hs = best["long"] / 2, best["short"] / 2
        corners = []
        for dl, ds in ((-hl, -hs), (hl, -hs), (hl, hs), (-hl, hs)):
            corners.append((best["cx"] + dl * ca - ds * sa, best["cy"] + dl * sa + ds * ca))
        draw.polygon(corners, outline=(255, 0, 90), width=2)
        small.save(args.debug, quality=88)


if __name__ == "__main__":
    main()
