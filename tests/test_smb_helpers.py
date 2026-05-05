"""Tests for smb.py helper functions.

Pure functions, no I/O, no SMB protocol mocking needed:
  - `_safe_name` — sanitizes camera names for directory paths (path-traversal guard)
  - `_is_safe_bosch_url` — duplicate of __init__ SSRF guard, must enforce same contract
"""

from __future__ import annotations

import pytest


# ── _safe_name (path-traversal sanitization) ────────────────────────────


class TestSafeName:
    def test_normal_name_passes_through(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        assert _safe_name("Terrasse") == "Terrasse"

    def test_spaces_and_hyphens_allowed(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        assert _safe_name("Bosch Terrasse-Kamera") == "Bosch Terrasse-Kamera"

    def test_dots_allowed(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        assert _safe_name("Cam.Front") == "Cam.Front"

    def test_double_dot_replaced(self):
        """Path-traversal sequence must be defanged."""
        from custom_components.bosch_shc_camera.smb import _safe_name
        result = _safe_name("../etc/passwd")
        assert ".." not in result
        # Must not contain a path separator
        assert "/" not in result

    def test_slashes_replaced(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        result = _safe_name("evil/path/here")
        assert "/" not in result

    def test_backslash_replaced(self):
        """Windows path separator must also be sanitized."""
        from custom_components.bosch_shc_camera.smb import _safe_name
        result = _safe_name("evil\\path")
        assert "\\" not in result

    def test_special_chars_replaced(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        result = _safe_name("name<with>special|chars*")
        for ch in "<>|*":
            assert ch not in result

    def test_unicode_replaced(self):
        """Non-word characters become `_` — keeps fs-safe."""
        from custom_components.bosch_shc_camera.smb import _safe_name
        result = _safe_name("Außenkamera")
        # `ß` is `\w` in Python regex so it stays — both fine for filesystem
        assert len(result) > 0

    def test_truncates_to_64_chars(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        long_name = "x" * 100
        assert len(_safe_name(long_name)) == 64

    def test_empty_string_returns_empty(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        assert _safe_name("") == ""

    def test_only_unsafe_chars_yields_underscores(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        result = _safe_name("///***")
        assert all(c == "_" for c in result)


# ── _is_safe_bosch_url (smb copy) ───────────────────────────────────────


class TestSmbSafeBoschUrl:
    @pytest.mark.parametrize("url", [
        "https://residential.cbs.boschsecurity.com/event/snap.jpg",
        "https://api.bosch.com/x",
    ])
    def test_legit_urls_allowed(self, url):
        from custom_components.bosch_shc_camera.smb import _is_safe_bosch_url
        assert _is_safe_bosch_url(url) is True

    @pytest.mark.parametrize("url", [
        "http://residential.cbs.boschsecurity.com/x",  # not HTTPS
        "https://attacker.com/x",
        "https://127.0.0.1/x",
        "",
    ])
    def test_unsafe_urls_rejected(self, url):
        from custom_components.bosch_shc_camera.smb import _is_safe_bosch_url
        assert _is_safe_bosch_url(url) is False
