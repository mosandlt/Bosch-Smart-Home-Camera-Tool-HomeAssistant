"""Tests for the security/SSRF helpers in __init__.py.

`_is_safe_bosch_url` is the SSRF gate — every image/video URL the
integration fetches goes through it. A regression here could let a
malicious cloud response point HA at an internal IP and blackhole it
into reading arbitrary URLs. High-value test target.

`_redact_creds` is used in log lines + diagnostics — must keep the
ephemeral Digest password short, never plain-text.

`get_options` is the canonical way to read config-entry options with
defaults applied — every platform reads it on entity setup.
"""

from __future__ import annotations

import pytest

from custom_components.bosch_shc_camera import (
    _is_safe_bosch_url,
    _redact_creds,
    get_options,
)
from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS


class TestSSRFGuard:
    """`_is_safe_bosch_url` SSRF allowlist — only HTTPS + Bosch domains pass."""

    @pytest.mark.parametrize("url", [
        "https://residential.cbs.boschsecurity.com/v11/video_inputs",
        "https://proxy-37.live.cbs.boschsecurity.com/abc/snap.jpg",
        "https://smarthome.authz.bosch.com/auth/realms/home_auth_provider/protocol/openid-connect/token",
        "https://www.bosch.com/boschcam?code=abc",
        "https://media.boschsecurity.com/rcpplus-over-cgi.pdf",
    ])
    def test_legitimate_bosch_urls_allowed(self, url: str) -> None:
        assert _is_safe_bosch_url(url) is True, f"Legit Bosch URL rejected: {url}"

    @pytest.mark.parametrize("url", [
        # Wrong domain
        "https://attacker.example.com/abc",
        "https://evil.com/snap.jpg",
        "https://boschsecurity.com.attacker.com/snap.jpg",  # subdomain attack
        # Non-HTTPS
        "http://residential.cbs.boschsecurity.com/v11/video_inputs",
        "ftp://residential.cbs.boschsecurity.com/file",
        # Internal IPs (SSRF target)
        "https://127.0.0.1/snap.jpg",
        "https://192.168.1.1/admin",
        "https://169.254.169.254/latest/meta-data/",  # AWS metadata
        "https://10.0.0.1/admin",
        # Localhost variants
        "https://localhost/admin",
        # Empty / malformed
        "",
        "not-a-url",
        "javascript:alert(1)",
        "file:///etc/passwd",
    ])
    def test_unsafe_urls_rejected(self, url: str) -> None:
        assert _is_safe_bosch_url(url) is False, f"Unsafe URL accepted: {url}"

    def test_homoglyph_domain_rejected(self) -> None:
        """A domain that ends with `.boschsecurity.com` after a hostile prefix
        must still be rejected unless prefix matches `.bosch.com` / `.boschsecurity.com`."""
        # The check uses endswith on the hostname with leading dot.
        # `evilboschsecurity.com` — endswith `.boschsecurity.com` is False (no leading dot)
        assert _is_safe_bosch_url("https://evilboschsecurity.com/x") is False
        # `xbosch.com` — endswith `.bosch.com` is False
        assert _is_safe_bosch_url("https://xbosch.com/x") is False


class TestRedactCreds:
    """`_redact_creds` redacts ephemeral Bosch Digest passwords for logs."""

    def test_password_redacted(self) -> None:
        out = _redact_creds({"username": "cbs-user-abc", "password": "supersecret123"})
        assert out["username"] == "cbs-user-abc"  # username is not redacted
        assert "supersecret123" not in out["password"]
        assert out["password"].startswith("sup")  # 3-char prefix preserved
        assert "(14 chars)" in out["password"]

    def test_short_password(self) -> None:
        out = _redact_creds({"password": "abc"})
        assert "(3 chars)" in out["password"]

    def test_empty_password(self) -> None:
        out = _redact_creds({"password": ""})
        assert "(0 chars)" in out["password"]

    def test_non_string_password_passes_through(self) -> None:
        """Non-string password (None / int / etc.) is left alone."""
        out = _redact_creds({"password": None})
        assert out["password"] is None
        out = _redact_creds({"password": 12345})
        assert out["password"] == 12345

    def test_other_fields_unchanged(self) -> None:
        out = _redact_creds({
            "rtspsUrl": "rtsps://...",
            "expires": 3600,
            "type": "REMOTE",
        })
        assert out["rtspsUrl"] == "rtsps://..."
        assert out["expires"] == 3600
        assert out["type"] == "REMOTE"

    def test_returns_copy_not_mutation(self) -> None:
        original = {"password": "abc123"}
        _redact_creds(original)
        assert original["password"] == "abc123", "Original dict was mutated"


class TestGetOptions:
    """`get_options` merges entry options on top of DEFAULT_OPTIONS."""

    def test_defaults_only_when_options_empty(self) -> None:
        from pytest_homeassistant_custom_component.common import MockConfigEntry
        from custom_components.bosch_shc_camera.const import DOMAIN
        entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
        opts = get_options(entry)
        for key, val in DEFAULT_OPTIONS.items():
            assert opts[key] == val, f"Default {key}={val} not applied"

    def test_user_options_override_defaults(self) -> None:
        from pytest_homeassistant_custom_component.common import MockConfigEntry
        from custom_components.bosch_shc_camera.const import DOMAIN
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={},
            options={"interval_status": 999, "enable_fcm_push": True},
        )
        opts = get_options(entry)
        assert opts["interval_status"] == 999
        assert opts["enable_fcm_push"] is True
        # Other defaults still present
        assert "interval_events" in opts

    def test_unknown_option_passes_through(self) -> None:
        """Unknown options aren't filtered out — kept as-is for forward compat."""
        from pytest_homeassistant_custom_component.common import MockConfigEntry
        from custom_components.bosch_shc_camera.const import DOMAIN
        entry = MockConfigEntry(
            domain=DOMAIN, data={},
            options={"some_future_option": "future_value"},
        )
        opts = get_options(entry)
        assert opts.get("some_future_option") == "future_value"
