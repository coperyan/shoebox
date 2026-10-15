"""Make undetected-chromedriver work on Apple Silicon without Rosetta.

undetected-chromedriver 3.5.5 has two problems on an arm64 Mac:

1. ``Patcher._set_platform_name`` picks ``mac-x64`` for every ``darwin``
   machine, so it downloads an Intel chromedriver. Without Rosetta that fails
   with ``OSError: [Errno 86] Bad CPU type in executable``.
2. ``Patcher.patch_exe`` rewrites the binary in place to hide the ``cdc_``
   marker, which breaks the binary's code signature. macOS kills an arm64
   binary with a broken signature at launch, and selenium reports
   ``Service ... unexpectedly exited. Status code was: -9``.

``fix_apple_silicon`` patches ``Patcher`` before ``uc.Chrome(...)`` runs: it
downloads ``mac-arm64`` instead, and ad-hoc re-signs the binary
(``codesign --force --sign -``) after uc patches it. On any other machine it
does nothing. Call it before every ``uc.Chrome(...)``. Calling it more than
once is safe.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import sys

logger = logging.getLogger(__name__)

_MARKER = "_shoebox_apple_silicon"


def is_apple_silicon(system: str | None = None, machine: str | None = None) -> bool:
    system = sys.platform if system is None else system
    machine = platform.machine() if machine is None else machine
    return system == "darwin" and machine == "arm64"


def arm64_platform_name(platform_name: str) -> str:
    """The chromedriver download name for an arm64 Mac, given uc's pick."""
    return "mac-arm64" if platform_name == "mac-x64" else platform_name


def codesign(path: str) -> None:
    """Ad-hoc re-sign a binary so macOS will run it after an in-place edit."""
    result = subprocess.run(
        ["codesign", "--force", "--sign", "-", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("codesign failed for %s: %s", path, result.stderr.strip())


def fix_apple_silicon(patcher_cls=None, *, enabled: bool | None = None) -> bool:
    """Patch uc's ``Patcher`` for an arm64 Mac. Returns True if it is patched.

    ``patcher_cls`` and ``enabled`` are for tests; by default this patches
    ``undetected_chromedriver.patcher.Patcher`` when running on Apple Silicon.
    """
    if enabled is None:
        enabled = is_apple_silicon()
    if not enabled:
        return False
    if patcher_cls is None:
        from undetected_chromedriver.patcher import Patcher as patcher_cls
    if getattr(patcher_cls, _MARKER, False):
        return True

    set_platform_name = patcher_cls._set_platform_name
    patch_exe = patcher_cls.patch_exe

    def _set_platform_name(self):
        set_platform_name(self)
        self.platform_name = arm64_platform_name(self.platform_name)

    def _patch_exe(self):
        result = patch_exe(self)
        codesign(self.executable_path)
        return result

    patcher_cls._set_platform_name = _set_platform_name
    patcher_cls.patch_exe = _patch_exe
    setattr(patcher_cls, _MARKER, True)
    logger.debug("undetected-chromedriver patched for Apple Silicon")
    return True
