"""Prepare a photo for the site: orient it, crop to a shape, resize, and save as a high-quality JPEG.

usage: python prepare.py <source> <out.jpg> --aspect 6:7 --width 1200 [--flip h|v|hv] [--focus 0.5,0.5] [--zoom 1.0]
                         [--saturation 1.0] [--brightness 1.0]
  --flip    mirror the photo first (a flat lay still reads naturally when mirrored)
  --saturation / --brightness   grade the colours toward the page palette (1.0 leaves them unchanged)
  --focus   centre of the crop window, as fractions of the (flipped) photo
  --zoom    1.0 keeps the largest window of that shape; above 1 crops in tighter
Next.js makes the smaller WebP versions from this file when the page is served.
"""
from __future__ import annotations

import argparse

from PIL import Image, ImageEnhance, ImageFilter, ImageOps


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("out")
    parser.add_argument("--aspect", required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--flip", default="")
    parser.add_argument("--focus", default="0.5,0.5")
    parser.add_argument("--zoom", type=float, default=1.0)
    parser.add_argument("--saturation", type=float, default=1.0)
    parser.add_argument("--brightness", type=float, default=1.0)
    args = parser.parse_args()

    image = Image.open(args.source).convert("RGB")
    if "h" in args.flip:
        image = ImageOps.mirror(image)
    if "v" in args.flip:
        image = ImageOps.flip(image)
    if args.saturation != 1.0:
        image = ImageEnhance.Color(image).enhance(args.saturation)
    if args.brightness != 1.0:
        image = ImageEnhance.Brightness(image).enhance(args.brightness)

    aw, ah = (float(v) for v in args.aspect.split(":"))
    target = aw / ah
    w, h = image.size
    crop_w, crop_h = (w, w / target) if w / h <= target else (h * target, h)
    crop_w, crop_h = crop_w / args.zoom, crop_h / args.zoom
    fx, fy = (float(v) for v in args.focus.split(","))
    left = min(max(fx * w - crop_w / 2, 0), w - crop_w)
    top = min(max(fy * h - crop_h / 2, 0), h - crop_h)
    box = (round(left), round(top), round(left + crop_w), round(top + crop_h))
    cropped = image.crop(box)

    out_h = round(args.width / target)
    scale = args.width / cropped.width
    resized = cropped.resize((args.width, out_h), Image.LANCZOS)
    if scale > 1.02:
        # A light sharpen so an enlarged photo keeps its fabric and edge detail.
        resized = resized.filter(ImageFilter.UnsharpMask(radius=1.4, percent=45, threshold=2))
    resized.save(args.out, "JPEG", quality=92, subsampling=0, optimize=True, progressive=True)
    print(f"{args.out}: source {w}x{h}, crop {box}, saved {args.width}x{out_h}, scale {scale:.2f}")


if __name__ == "__main__":
    main()
