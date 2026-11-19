"""Prepare a 3D object rendered on a dark backdrop so it can sit on a black panel with no visible edge.

usage: python cutout_on_black.py <source> <out.jpg> [--black 30] [--pad 0.14] [--size 900]
The backdrop is pushed down to pure black and the picture is cropped to a square around the object.
On the page the image is drawn with `mix-blend-mode: lighten`, so only the object shows on the panel.
"""
from __future__ import annotations

import argparse

from PIL import Image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("out")
    parser.add_argument("--black", type=int, default=30, help="values at or below this become pure black")
    parser.add_argument("--pad", type=float, default=0.14, help="margin around the object, as a share of its size")
    parser.add_argument("--size", type=int, default=900)
    args = parser.parse_args()

    image = Image.open(args.source).convert("RGB")
    floor = args.black
    scale = 255 / (255 - floor)
    crushed = image.point(lambda v: max(0, min(255, round((v - floor) * scale))))

    # The object is everything clearly brighter than the backdrop.
    bright = crushed.convert("L").point(lambda v: 255 if v > 34 else 0)
    box = bright.getbbox()
    if box is None:
        raise SystemExit("no object found")
    left, top, right, bottom = box
    side = max(right - left, bottom - top) * (1 + args.pad * 2)
    cx, cy = (left + right) / 2, (top + bottom) / 2
    half = side / 2
    crop = (round(cx - half), round(cy - half), round(cx + half), round(cy + half))
    square = Image.new("RGB", (crop[2] - crop[0], crop[3] - crop[1]), (0, 0, 0))
    square.paste(crushed, (-crop[0], -crop[1]))
    square = square.resize((args.size, args.size), Image.LANCZOS)
    square.save(args.out, "JPEG", quality=90, optimize=True, progressive=True)
    print(f"{args.out}: object box {box}, saved {args.size}x{args.size}")


if __name__ == "__main__":
    main()
