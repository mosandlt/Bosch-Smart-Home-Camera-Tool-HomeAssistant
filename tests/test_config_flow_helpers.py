"""Tests for config_flow.py helper functions (PKCE, JWT parsing, auth-URL building).

These are pure functions extracted from the OAuth2 flow — no aiohttp,
no HA fixtures needed. Catches:
  - PKCE verifier/challenge contract (RFC 7636)
  - Auth URL has the right params
  - _extract_code parses both `?code=...` and `https://x?code=...` shapes
  - JWT azp-claim detection for the legacy-vs-OSS client gate
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest


# ── _pkce_pair ──────────────────────────────────────────────────────────


class TestPkcePair:
    def test_returns_two_strings(self):
        from custom_components.bosch_shc_camera.config_flow import _pkce_pair
        verifier, challenge = _pkce_pair()
        assert isinstance(verifier, str)
        assert isinstance(challenge, str)

    def test_each_call_produces_unique_pair(self):
        """RFC 7636: verifier must be unique per request."""
        from custom_components.bosch_shc_camera.config_flow import _pkce_pair
        v1, _ = _pkce_pair()
        v2, _ = _pkce_pair()
        assert v1 != v2

    def test_challenge_is_sha256_of_verifier(self):
        """Verify the cryptographic relationship — RFC 7636 S256 method."""
        from custom_components.bosch_shc_camera.config_flow import _pkce_pair
        verifier, challenge = _pkce_pair()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        assert challenge == expected

    def test_verifier_length_meets_rfc_min(self):
        """RFC 7636 says verifier must be 43-128 chars (after url-safe base64)."""
        from custom_components.bosch_shc_camera.config_flow import _pkce_pair
        verifier, _ = _pkce_pair()
        assert 43 <= len(verifier) <= 128

    def test_challenge_no_padding(self):
        """url-safe base64 must have no `=` padding (RFC 7636 §4.2)."""
        from custom_components.bosch_shc_camera.config_flow import _pkce_pair
        _, challenge = _pkce_pair()
        assert "=" not in challenge


# ── _build_auth_url ─────────────────────────────────────────────────────


class TestBuildAuthUrl:
    def test_contains_required_params(self):
        from custom_components.bosch_shc_camera.config_flow import _build_auth_url
        url = _build_auth_url("test-challenge", "test-state")
        for param in (
            "client_id=", "response_type=code", "scope=",
            "code_challenge=test-challenge",
            "code_challenge_method=S256",
            "state=test-state",
        ):
            assert param in url, f"Missing {param!r} in auth URL"

    def test_uses_keycloak_base(self):
        from custom_components.bosch_shc_camera.config_flow import _build_auth_url
        url = _build_auth_url("c", "s")
        assert "smarthome.authz.bosch.com" in url


# ── _extract_code ──────────────────────────────────────────────────────


class TestExtractCode:
    def test_full_url_with_code(self):
        from custom_components.bosch_shc_camera.config_flow import _extract_code
        url = "https://www.bosch.com/boschcam?code=ABC123&state=xyz"
        assert _extract_code(url) == "ABC123"

    def test_query_string_only(self):
        from custom_components.bosch_shc_camera.config_flow import _extract_code
        assert _extract_code("?code=DEF456") == "DEF456"

    def test_strips_whitespace(self):
        from custom_components.bosch_shc_camera.config_flow import _extract_code
        assert _extract_code("  https://x?code=GHI789  ") == "GHI789"

    def test_returns_none_on_error_param(self):
        """If the URL has `error=...` in the query, treat as failed flow."""
        from custom_components.bosch_shc_camera.config_flow import _extract_code
        assert _extract_code("https://x?error=access_denied") is None

    def test_returns_none_when_no_code(self):
        from custom_components.bosch_shc_camera.config_flow import _extract_code
        assert _extract_code("https://x?something=else") is None

    def test_returns_none_on_garbage(self):
        from custom_components.bosch_shc_camera.config_flow import _extract_code
        # No query string at all → no code
        assert _extract_code("not-a-url") is None


# ── _detect_token_client_id ─────────────────────────────────────────────


def _make_jwt(payload: dict) -> str:
    """Build a fake JWT for the azp-detection test (signature ignored)."""
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.fake-signature"


class TestDetectTokenClientId:
    def test_returns_oss_residential_app(self):
        from custom_components.bosch_shc_camera.config_flow import _detect_token_client_id
        token = _make_jwt({"azp": "oss_residential_app", "sub": "user-1"})
        assert _detect_token_client_id(token) == "oss_residential_app"

    def test_returns_legacy_residential_app(self):
        from custom_components.bosch_shc_camera.config_flow import _detect_token_client_id
        token = _make_jwt({"azp": "residential_app"})
        assert _detect_token_client_id(token) == "residential_app"

    def test_returns_none_for_empty_token(self):
        from custom_components.bosch_shc_camera.config_flow import _detect_token_client_id
        assert _detect_token_client_id("") is None
        assert _detect_token_client_id(None) is None

    def test_returns_none_for_malformed_token(self):
        """JWT must have at least 2 dot-separated parts; malformed ones yield None."""
        from custom_components.bosch_shc_camera.config_flow import _detect_token_client_id
        assert _detect_token_client_id("not-a-jwt") is None
        assert _detect_token_client_id("only.one") is None

    def test_returns_none_when_azp_missing(self):
        from custom_components.bosch_shc_camera.config_flow import _detect_token_client_id
        token = _make_jwt({"sub": "user-1"})  # no azp claim
        assert _detect_token_client_id(token) is None

    def test_returns_none_for_garbled_payload(self):
        """Non-JSON payload base64 — must not raise."""
        from custom_components.bosch_shc_camera.config_flow import _detect_token_client_id
        garbled = "header.bm90LWpzb24=.signature"  # base64 of "not-json"
        assert _detect_token_client_id(garbled) is None
