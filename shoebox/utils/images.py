"""Combine card scans into a single composite image.

Ported from the standalone ``combine_images.py`` script. The listing pipelines
use :func:`combine_front_back` to build the front+back picture that follows
the two scans on a listing.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageColor, ImageOps

RGBA = tuple[int, int, int, int]

# Defaults for the listing composite. Scans come off the scanner at several
# thousand pixels a side; capping the width keeps the JPEG well under eBay's
# per-picture size limit while staying far above its 500px minimum.
COMBINED_MAX_WIDTH = 3200
COMBINED_GAP = 24
COMBINED_PADDING = 24
COMBINED_QUALITY = 92


def parse_color(value: str) -> RGBA:
    """Turn 'white', '#222', 'none' etc. into an RGBA tuple."""
    if value.lower() in {"none", "transparent"}:
        return (0, 0, 0, 0)
    rgb = ImageColor.getrgb(value)
    return rgb if len(rgb) == 4 else (*rgb, 255)


def open_image(source: Path | bytes) -> Image.Image:
    """Open a file or raw bytes as RGBA, honoring EXIF orientation."""
    im = Image.open(io.BytesIO(source) if isinstance(source, bytes) else source)
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    return im.convert("RGBA")


def scale_to_height(im: Image.Image, height: int) -> Image.Image:
    if im.height == height:
        return im
    width = max(1, round(im.width * height / im.height))
    return im.resize((width, height), Image.LANCZOS)


def scale_to_width(im: Image.Image, width: int) -> Image.Image:
    if im.width == width:
        return im
    height = max(1, round(im.height * width / im.width))
    return im.resize((width, height), Image.LANCZOS)


def offset(free_space: int, align: str) -> int:
    """Where to place an image inside the free space of its cell."""
    if align in {"start", "top", "left"}:
        return 0
    if align in {"end", "bottom", "right"}:
        return free_space
    return free_space // 2  # center


def compose_linear(
    images: list[Image.Image],
    *,
    horizontal: bool = True,
    gap: int = COMBINED_GAP,
    padding: int = COMBINED_PADDING,
    background: RGBA = (255, 255, 255, 255),
    align: str = "center",
    match: bool = True,
) -> Image.Image:
    """Lay images out in a row (or column), optionally matching heights (widths)."""
    if match:
        if horizontal:
            target = max(im.height for im in images)
            images = [scale_to_height(im, target) for im in images]
        else:
            target = max(im.width for im in images)
            images = [scale_to_width(im, target) for im in images]

    span = sum(im.width if horizontal else im.height for im in images)
    span += gap * (len(images) - 1)
    cross = max(im.height if horizontal else im.width for im in images)

    if horizontal:
        size = (span + 2 * padding, cross + 2 * padding)
    else:
        size = (cross + 2 * padding, span + 2 * padding)

    canvas = Image.new("RGBA", size, background)
    cursor = padding
    for im in images:
        if horizontal:
            canvas.alpha_composite(im, (cursor, padding + offset(cross - im.height, align)))
            cursor += im.width + gap
        else:
            canvas.alpha_composite(im, (padding + offset(cross - im.width, align), cursor))
            cursor += im.height + gap
    return canvas


def to_jpeg_bytes(canvas: Image.Image, quality: int = COMBINED_QUALITY) -> bytes:
    """Flatten onto white and encode as JPEG."""
    flat = Image.new("RGB", canvas.size, (255, 255, 255))
    flat.paste(canvas, mask=canvas.split()[3])
    buf = io.BytesIO()
    flat.save(buf, format="JPEG", quality=quality, subsampling=0)
    return buf.getvalue()


def combine_front_back(
    front: Path | bytes,
    back: Path | bytes,
    *,
    max_width: int | None = COMBINED_MAX_WIDTH,
) -> bytes:
    """Front and back side by side at matched heights, as JPEG bytes."""
    canvas = compose_linear([open_image(front), open_image(back)])
    if max_width and canvas.width > max_width:
        canvas = scale_to_width(canvas, max_width)
    return to_jpeg_bytes(canvas)
