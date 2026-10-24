from __future__ import annotations

from pathlib import Path

from shoebox.settings import get_settings


def handle_image_path(img: str) -> str:
    """Normalize an image reference.

    - If the provided string is an existing path, return it.
    - If it is a scan number (``"123"``, or ``"123.0"`` from Excel), build the
      expected path under scans_dir using the configured scan naming convention
      (``scan_prefix`` / ``scan_number_padding`` / ``scan_extension``).
    - Otherwise (a path that does not exist here) return it unchanged.
    """

    p = Path(img)
    if p.exists():
        return str(p)

    number = img.strip().split(".")[0]
    if not number.isdigit():
        # A path that is not on this machine (e.g. a workstation path in the
        # cloud): keep it, so callers can still fetch it by its file name.
        return img

    paths = get_settings().paths

    if paths.scan_number_padding:
        number = number.rjust(paths.scan_number_padding, "0")

    ext = paths.scan_extension
    if ext and not ext.startswith("."):
        ext = "." + ext

    guessed = Path(paths.scans_dir) / f"{paths.scan_prefix}{number}{ext}"
    return str(guessed)
