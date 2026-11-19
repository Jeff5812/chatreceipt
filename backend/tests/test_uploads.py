from __future__ import annotations

import io

import pytest
from PIL import Image

from app.uploads import (
    MAX_PARTS_PER_SCREENSHOT,
    MODEL_MAX_WIDTH_PX,
    PART_HEIGHT_PX,
    PART_OVERLAP_PX,
    PreparedImage,
    UploadedFile,
    UploadRejected,
    prepare_screenshots,
)
from tests.samples import image_bytes

LIMITS = {"max_files": 10, "max_file_bytes": 10 * 1024 * 1024, "max_total_bytes": 40 * 1024 * 1024}


def prepare(*files: bytes, **limits: int) -> list[PreparedImage]:
    return prepare_screenshots(
        [UploadedFile(f"file{i}", data) for i, data in enumerate(files)], **{**LIMITS, **limits}
    )


def rejection(*files: bytes, **limits: int) -> UploadRejected:
    with pytest.raises(UploadRejected) as caught:
        prepare(*files, **limits)
    return caught.value


def test_accepts_png_jpeg_and_webp_and_reencodes_them() -> None:
    images = prepare(image_bytes("PNG"), image_bytes("JPEG"), image_bytes("WEBP"))
    assert [image.screenshot for image in images] == [1, 2, 3]
    assert all(image.media_type == "image/jpeg" for image in images)
    assert all(image.data.startswith(b"\xff\xd8\xff") for image in images)


def test_upload_order_is_preserved() -> None:
    images = prepare(image_bytes(size=(400, 600)), image_bytes(size=(400, 900)), image_bytes(size=(400, 700)))
    assert [image.height for image in images] == [600, 900, 700]


def test_files_are_identified_by_content_not_name() -> None:
    error = rejection(b"%PDF-1.4 definitely not an image")
    assert error.code == "unsupported_type"
    assert error.status_code == 415
    assert error.file_index == 1


def test_other_image_formats_are_rejected() -> None:
    assert rejection(image_bytes("GIF")).code == "unsupported_type"


def test_damaged_images_are_rejected() -> None:
    data = image_bytes("PNG", size=(800, 1600), noise=True)
    assert rejection(data[: len(data) // 2]).code == "unreadable_image"


def test_tiny_images_are_rejected() -> None:
    assert rejection(image_bytes(size=(60, 60))).code == "image_too_small"


def test_decompression_bombs_are_rejected() -> None:
    assert rejection(image_bytes("PNG", size=(8000, 8000), mode="1")).code == "image_too_large"


def test_size_and_count_limits() -> None:
    assert rejection(b"").code == "empty_file"
    oversized = rejection(image_bytes(), max_file_bytes=100)
    assert (oversized.code, oversized.status_code) == ("file_too_large", 413)
    assert rejection(*[image_bytes()] * 3, max_files=2).code == "too_many_files"
    assert rejection(image_bytes(noise=True), image_bytes(noise=True), max_total_bytes=1000).code == "upload_too_large"


def test_metadata_is_stripped() -> None:
    exif = Image.Exif()
    exif[0x010F] = "SecretPhoneMaker"
    image = prepare(image_bytes("JPEG", exif=exif.tobytes()))[0]
    assert b"SecretPhoneMaker" not in image.data
    with Image.open(io.BytesIO(image.data)) as reread:
        assert len(reread.getexif()) == 0


def test_exif_rotation_is_applied() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise
    image = prepare(image_bytes("JPEG", size=(400, 800), exif=exif.tobytes()))[0]
    assert (image.width, image.height) == (800, 400)


def test_transparency_is_flattened() -> None:
    image = prepare(image_bytes("PNG", mode="RGBA"))[0]
    with Image.open(io.BytesIO(image.data)) as reread:
        assert reread.mode == "RGB"


def test_wide_images_are_downscaled() -> None:
    image = prepare(image_bytes(size=(2560, 1440)))[0]
    assert (image.width, image.height) == (MODEL_MAX_WIDTH_PX, 720)


def test_tall_screenshots_are_split_into_overlapping_parts_in_order() -> None:
    images = prepare(image_bytes(size=(1080, 6000)), image_bytes(size=(400, 800)))
    first = [image for image in images if image.screenshot == 1]
    assert [image.part for image in first] == [1, 2, 3]
    assert all(image.parts == 3 and image.height <= PART_HEIGHT_PX for image in first)
    assert sum(image.height for image in first) > 6000  # parts overlap
    assert images[-1].screenshot == 2


def test_extremely_long_screenshots_are_rejected() -> None:
    # Within the 20,000 px side limit, but it would need more than MAX_PARTS_PER_SCREENSHOT parts.
    covered_by_max_parts = PART_HEIGHT_PX + (PART_HEIGHT_PX - PART_OVERLAP_PX) * (MAX_PARTS_PER_SCREENSHOT - 1)
    height = covered_by_max_parts + 500
    assert height < 20_000
    error = rejection(image_bytes(size=(400, height)))
    assert error.code == "screenshot_too_long"


def test_too_many_parts_in_one_request_are_rejected() -> None:
    tall = image_bytes(size=(400, 13000))
    assert rejection(tall, tall, tall, tall).code == "too_much_content"
