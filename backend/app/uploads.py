"""Validate uploaded screenshots and prepare them for the extraction model.

Screenshots are untrusted input. Each file is identified by its content (never its name or the
browser's declared type), fully decoded to catch damaged files, stripped of metadata and
re-encoded. Nothing is written to disk: prepared images exist only for the request.
"""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

ACCEPTED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
MIN_SIDE_PX = 100
MAX_SIDE_PX = 20_000
MAX_PIXELS = 50_000_000

# Sizing for the model. Current Claude models read up to 2576 px on the long edge, so a phone
# screenshot fits whole; very tall "scrolling" screenshots are cut into overlapping parts so
# their text isn't shrunk to an unreadable size.
MODEL_MAX_WIDTH_PX = 1280
SINGLE_MAX_HEIGHT_PX = 2560
PART_HEIGHT_PX = 2400
PART_OVERLAP_PX = 160
MAX_PARTS_PER_SCREENSHOT = 8
MAX_IMAGES_PER_REQUEST = 20
JPEG_QUALITY = 88

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class UploadRejected(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        file_index: int | None = None,
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.file_index = file_index
        self.status_code = status_code


@dataclass(frozen=True)
class UploadedFile:
    name: str
    data: bytes


@dataclass(frozen=True)
class PreparedImage:
    screenshot: int  # 1-based position in the upload order
    part: int  # 1-based; tall screenshots have several parts
    parts: int
    media_type: str
    data: bytes
    width: int
    height: int


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):g} MB"


def prepare_screenshots(
    files: list[UploadedFile],
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> list[PreparedImage]:
    if not files:
        raise UploadRejected("no_files", "Add at least one screenshot.")
    if len(files) > max_files:
        raise UploadRejected("too_many_files", f"Upload up to {max_files} screenshots at a time.")
    if sum(len(file.data) for file in files) > max_total_bytes:
        raise UploadRejected(
            "upload_too_large",
            f"These screenshots add up to more than {_megabytes(max_total_bytes)}. "
            "Upload fewer or smaller files.",
            status_code=413,
        )

    prepared: list[PreparedImage] = []
    for index, file in enumerate(files, start=1):
        label = f"Screenshot {index}"
        if not file.data:
            raise UploadRejected("empty_file", f"{label} is empty.", file_index=index)
        if len(file.data) > max_file_bytes:
            raise UploadRejected(
                "file_too_large",
                f"{label} is larger than {_megabytes(max_file_bytes)}.",
                file_index=index,
                status_code=413,
            )
        image = _normalise(_decode(file.data, label, index), label, index)
        parts = _split_for_model(image)
        if len(parts) > MAX_PARTS_PER_SCREENSHOT:
            raise UploadRejected(
                "screenshot_too_long",
                f"{label} is too long to read in one go. Split it into shorter screenshots.",
                file_index=index,
            )
        for number, part in enumerate(parts, start=1):
            prepared.append(
                PreparedImage(
                    screenshot=index,
                    part=number,
                    parts=len(parts),
                    media_type="image/jpeg",
                    data=_encode_jpeg(part),
                    width=part.width,
                    height=part.height,
                )
            )
        if len(prepared) > MAX_IMAGES_PER_REQUEST:
            raise UploadRejected(
                "too_much_content",
                "That's more than can be read at once. Upload fewer or shorter screenshots.",
            )
    return prepared


def inspect_image(data: bytes, *, max_file_bytes: int, label: str = "This file") -> tuple[str, int, int]:
    """Check one file with the same rules as ``prepare_screenshots``: (content type, width, height).

    The type comes from the file's content, never from its name or the browser's declared type.
    """
    if not data:
        raise UploadRejected("empty_file", f"{label} is empty.")
    if len(data) > max_file_bytes:
        raise UploadRejected("file_too_large", f"{label} is larger than {_megabytes(max_file_bytes)}.", status_code=413)
    image = _decode(data, label, 1)
    return ACCEPTED_FORMATS[image.format or ""], image.width, image.height


def _decode(data: bytes, label: str, index: int) -> Image.Image:
    unsupported = UploadRejected(
        "unsupported_type",
        f"{label} isn't a JPEG, PNG or WebP image.",
        file_index=index,
        status_code=415,
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format not in ACCEPTED_FORMATS:
                    raise unsupported
                _check_dimensions(*probe.size, label=label, index=index)
                probe.verify()
            image = Image.open(io.BytesIO(data))
            image.load()
            return image
    except UploadRejected:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise UploadRejected(
            "image_too_large", f"{label} has too many pixels to process.", file_index=index
        ) from None
    except UnidentifiedImageError:
        raise unsupported from None
    except Exception:  # Pillow raises many different errors for damaged files.
        raise UploadRejected(
            "unreadable_image", f"{label} couldn't be read. It may be damaged.", file_index=index
        ) from None


def _check_dimensions(width: int, height: int, *, label: str, index: int) -> None:
    if min(width, height) < MIN_SIDE_PX:
        raise UploadRejected("image_too_small", f"{label} is too small to read.", file_index=index)
    if max(width, height) > MAX_SIDE_PX:
        raise UploadRejected(
            "image_too_large", f"{label} is too large to process.", file_index=index
        )


def _normalise(image: Image.Image, label: str, index: int) -> Image.Image:
    """Apply EXIF rotation, flatten transparency onto white, convert to RGB, limit width."""
    try:
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            flattened = Image.new("RGB", rgba.size, (255, 255, 255))
            flattened.paste(rgba, mask=rgba.getchannel("A"))
            image = flattened
        elif image.mode != "RGB":
            image = image.convert("RGB")
        if image.width > MODEL_MAX_WIDTH_PX:
            height = max(1, round(image.height * MODEL_MAX_WIDTH_PX / image.width))
            image = image.resize((MODEL_MAX_WIDTH_PX, height), Image.Resampling.LANCZOS)
        return image
    except Exception:
        raise UploadRejected(
            "unreadable_image", f"{label} couldn't be read. It may be damaged.", file_index=index
        ) from None


def _split_for_model(image: Image.Image) -> list[Image.Image]:
    if image.height <= SINGLE_MAX_HEIGHT_PX:
        return [image]
    parts: list[Image.Image] = []
    step = PART_HEIGHT_PX - PART_OVERLAP_PX
    top = 0
    while len(parts) <= MAX_PARTS_PER_SCREENSHOT:
        bottom = min(top + PART_HEIGHT_PX, image.height)
        parts.append(image.crop((0, top, image.width, bottom)))
        if bottom >= image.height:
            break
        top += step
    return parts


def _encode_jpeg(image: Image.Image) -> bytes:
    # Saving without exif/icc_profile arguments drops all metadata.
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True, subsampling=0)
    return buffer.getvalue()
