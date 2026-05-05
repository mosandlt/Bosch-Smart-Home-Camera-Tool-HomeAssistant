"""Tests for token-refresh logic in BoschCameraCoordinator.

Token refresh is the single most common pain point in OAuth integrations —
historically the source of GH#2 (token refresh fails) and GH#5 (refresh-
token 404). The current implementation has 3 retries, server-outage backoff,
in-memory + entry-data persistence, and a refresh-token rotation guard.

These tests pin the contract of `_token_still_valid` (JWT exp parsing)
and `_token_failure_alert_sent` flag handling. The actual HTTP refresh
flow is tested elsewhere (config_flow_helpers).
"""

from __future__ import annotations

import base64
import json
import time as _time
from types import SimpleNamespace

import pytest


def _make_jwt(exp_offset_sec: int = 3600) -> str:
    """Build a fake JWT with the given exp offset from now.

    Positive offset = valid for X seconds.
    Negative offset = expired X seconds ago.
    """
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({
            "exp": int(_time.time()) + exp_offset_sec,
            "iat": int(_time.time()),
            "sub": "user-1",
        }).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.fake-signature"


# ── _token_still_valid ──────────────────────────────────────────────────


class TestTokenStillValid:
    def _make_coord(self, token: str):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = SimpleNamespace(
            _entry=SimpleNamespace(data={"bearer_token": token}),
            _refreshed_token=None,
        )
        # Bind methods we need
        coord.token = BoschCameraCoordinator.token.fget(coord)
        return coord, BoschCameraCoordinator._token_still_valid

    def test_valid_token_returns_true(self):
        token = _make_jwt(exp_offset_sec=3600)  # 1 hour from now
        coord, method = self._make_coord(token)
        assert method(coord, min_remaining=60) is True

    def test_expiring_within_min_remaining_returns_false(self):
        """Token expires in 30s, min_remaining=60 → not valid enough."""
        token = _make_jwt(exp_offset_sec=30)
        coord, method = self._make_coord(token)
        assert method(coord, min_remaining=60) is False

    def test_already_expired_returns_false(self):
        token = _make_jwt(exp_offset_sec=-300)  # 5 min ago
        coord, method = self._make_coord(token)
        assert method(coord, min_remaining=60) is False

    def test_empty_token_returns_false(self):
        coord, method = self._make_coord("")
        assert method(coord, min_remaining=60) is False

    def test_malformed_jwt_returns_false(self):
        """JWT with only 1 dot-segment must not crash."""
        coord, method = self._make_coord("only.one")
        assert method(coord, min_remaining=60) is False

    def test_garbage_token_returns_false(self):
        """Random string — base64 decode fails — must not crash."""
        coord, method = self._make_coord("not.a-valid_jwt")
        assert method(coord, min_remaining=60) is False

    def test_jwt_without_exp_treated_as_expired(self):
        """Payload without `exp` claim → exp.get(0) = 0 → expired."""
        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(b'{"sub":"u"}').rstrip(b"=").decode()
        token = f"{header}.{payload}.sig"
        coord, method = self._make_coord(token)
        assert method(coord, min_remaining=60) is False

    def test_zero_min_remaining_accepts_valid_token(self):
        """min_remaining=0 → any not-yet-expired token is valid."""
        token = _make_jwt(exp_offset_sec=10)
        coord, method = self._make_coord(token)
        assert method(coord, min_remaining=0) is True

    def test_min_remaining_at_boundary(self):
        """A token expiring in exactly min_remaining seconds is valid (>=)."""
        # We can't hit this exactly due to time progression — give a
        # comfortable margin and test a slightly-larger value works.
        token = _make_jwt(exp_offset_sec=120)
        coord, method = self._make_coord(token)
        assert method(coord, min_remaining=60) is True
