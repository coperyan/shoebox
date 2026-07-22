from __future__ import annotations

from pathlib import Path

from shoebox.settings import get_settings


def handle_image_path(img: str) -> str:
    """Normalize an image reference.

    - If the provided string is an existing path, return it.
    - Otherwise, treat it as a scan number/id and build the expected path under
      scans_dir using the configured scan naming convention
      (``scan_prefix`` / ``scan_number_padding`` / ``scan_extension``).
    """

    p = Path(img)
    if p.exists():
        return str(p)

    paths = get_settings().paths

    number = img.split(".")[0]
    if paths.scan_number_padding:
        number = number.rjust(paths.scan_number_padding, "0")

    ext = paths.scan_extension
    if ext and not ext.startswith("."):
        ext = "." + ext

    guessed = Path(paths.scans_dir) / f"{paths.scan_prefix}{number}{ext}"
    return str(guessed)
