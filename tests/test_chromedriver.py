"""Tests for the Apple Silicon fix to undetected-chromedriver's Patcher.

Each test patches a throwaway subclass of uc's real ``Patcher``, so the class
the rest of the suite sees is left alone.
"""

from __future__ import annotations

import pytest
from undetected_chromedriver.patcher import Patcher

from shoebox.clients import chromedriver
from shoebox.clients.chromedriver import (
    arm64_platform_name,
    fix_apple_silicon,
    is_apple_silicon,
)


class _Stub(Patcher):
    # uc's __del__ cleans up download dirs these bare instances never made
    def __del__(self):
        pass


@pytest.fixture
def patcher_cls():
    class P(_Stub):
        pass

    return P


def _bare(cls, platform: str = "darwin", old: bool = False):
    """A Patcher with just what ``_set_platform_name`` reads (no download dirs)."""
    p = cls.__new__(cls)
    p.platform = platform
    p.is_old_chromedriver = old
    p.exe_name = "chromedriver%s"
    return p


class TestIsAppleSilicon:
    @pytest.mark.parametrize(
        "system,machine,expected",
        [
            ("darwin", "arm64", True),
            ("darwin", "x86_64", False),
            ("linux", "arm64", False),
            ("win32", "AMD64", False),
        ],
    )
    def test_only_an_arm64_mac(self, system, machine, expected):
        assert is_apple_silicon(system, machine) is expected


class TestArm64PlatformName:
    def test_intel_mac_becomes_arm(self):
        assert arm64_platform_name("mac-x64") == "mac-arm64"

    @pytest.mark.parametrize("name", ["linux64", "win32", "mac64", "mac-arm64"])
    def test_everything_else_is_untouched(self, name):
        assert arm64_platform_name(name) == name


class TestFixAppleSilicon:
    def test_downloads_the_arm64_driver_on_a_mac(self, patcher_cls):
        assert fix_apple_silicon(patcher_cls, enabled=True) is True
        p = _bare(patcher_cls)
        p._set_platform_name()
        assert p.platform_name == "mac-arm64"
        assert p.exe_name == "chromedriver"

    def test_unpatched_uc_picks_intel(self, patcher_cls):
        # the bug being fixed: stock uc 3.5.5 picks mac-x64 on every Mac
        p = _bare(patcher_cls)
        p._set_platform_name()
        assert p.platform_name == "mac-x64"

    def test_linux_is_unaffected_even_when_patched(self, patcher_cls):
        fix_apple_silicon(patcher_cls, enabled=True)
        p = _bare(patcher_cls, platform="linux")
        p._set_platform_name()
        assert p.platform_name == "linux64"

    def test_noop_off_apple_silicon(self, patcher_cls):
        original = patcher_cls._set_platform_name
        assert fix_apple_silicon(patcher_cls, enabled=False) is False
        assert patcher_cls._set_platform_name is original

    def test_patching_twice_wraps_once(self, patcher_cls):
        fix_apple_silicon(patcher_cls, enabled=True)
        first = patcher_cls._set_platform_name
        fix_apple_silicon(patcher_cls, enabled=True)
        assert patcher_cls._set_platform_name is first

    def test_the_real_patcher_is_left_alone(self, patcher_cls):
        fix_apple_silicon(patcher_cls, enabled=True)
        assert Patcher._set_platform_name is not patcher_cls._set_platform_name

    def test_re_signs_the_driver_after_patching_it(self, patcher_cls, monkeypatch):
        calls = []
        monkeypatch.setattr(Patcher, "patch_exe", lambda self: calls.append("patch"))
        monkeypatch.setattr(chromedriver, "codesign", lambda path: calls.append(("sign", path)))

        class Q(_Stub):
            pass

        fix_apple_silicon(Q, enabled=True)
        p = Q.__new__(Q)
        p.executable_path = "/tmp/undetected_chromedriver"
        p.patch_exe()
        assert calls == ["patch", ("sign", "/tmp/undetected_chromedriver")]
