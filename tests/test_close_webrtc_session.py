"""Regression tests for close_webrtc_session and async_create_stream privacy gating.

Root cause (2026-05-16):
  HA go2rtc provider's async_close_session calls dict.pop(session_id) without
  a default.  When privacy mode is ON, async_handle_async_webrtc_offer bails
  before inserting the session into go2rtc._sessions, but the websocket handler
  already registered partial(camera.close_webrtc_session, session_id) as a
  subscription cleanup.  On client disconnect async_handle_close calls that
  partial → KeyError → HA logs ERROR "Error unsubscribing from subscription"
  ~30+ times per session (seen 2026-05-16 10:50–10:56 in HA logs).

User report: Bosch Innenbereich in privacy mode, WebRTC client disconnects.
Tracker: internal — no GitHub issue (privacy-mode-only repro).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

CAM_ID = "22222222-0000-0000-0000-000000000000"


@pytest.fixture
def stub_coord() -> SimpleNamespace:
    """Minimal coordinator stub sufficient for BoschCamera construction."""
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Innenbereich",
                    "hardwareVersion": "HOME_Eyes_Indoor_II",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                },
                "events": [],
                "live": {},
            }
        },
        _live_connections={},
        _camera_entities={},
        _stream_fell_back={},
        _stream_error_count={},
        _shc_state_cache={},
        last_update_success=True,
        motion_settings=lambda cam_id: {},
        is_stream_warming=lambda cam_id: False,
    )


@pytest.fixture
def stub_entry() -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "fake-token"},
        options={"snapshot_interval": 1800},
    )


@pytest.fixture
def camera(stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> Any:
    """Construct a bare BoschCamera without HA lifecycle (no hass, no add_to_hass)."""
    from custom_components.bosch_shc_camera.camera import BoschCamera

    return BoschCamera(stub_coord, CAM_ID, stub_entry)


# ── close_webrtc_session ────────────────────────────────────────────────────


class TestCloseWebrtcSession:
    """close_webrtc_session must be idempotent / no-op for unknown session IDs.

    The go2rtc provider uses dict.pop(session_id) — raises KeyError when the
    session was never established (privacy mode blocked the offer).  Our override
    must catch that KeyError so HA's async_handle_close does not log ERROR.
    """

    def test_noop_when_no_provider(self, camera: Any) -> None:
        """No _webrtc_provider → close_webrtc_session must not raise."""
        # Base Camera sets _webrtc_provider via async_refresh_providers.
        # Without calling that, it is None.  Ensure no AttributeError.
        assert getattr(camera, "_webrtc_provider", None) is None
        # Must be a clean no-op — no exception of any kind.
        camera.close_webrtc_session("non-existent-session-id")

    def test_noop_on_keyerror_from_provider(self, camera: Any) -> None:
        """Provider raises KeyError (session never inserted) → must not propagate.

        This is the exact failure path seen in HA logs 2026-05-16:
          go2rtc async_close_session → self._sessions.pop(session_id) → KeyError
          → HA websocket_api connection.py async_handle_close → logs ERROR
        """
        mock_provider = MagicMock()
        mock_provider.async_close_session.side_effect = KeyError("unknown-session")
        camera._webrtc_provider = mock_provider  # inject provider

        # Must NOT raise — KeyError must be silently discarded.
        camera.close_webrtc_session("unknown-session-id")

        mock_provider.async_close_session.assert_called_once_with("unknown-session-id")

    def test_known_session_delegates_to_provider(self, camera: Any) -> None:
        """When session IS known, provider.async_close_session must be called."""
        mock_provider = MagicMock()
        # No side_effect → returns None (happy path)
        camera._webrtc_provider = mock_provider

        camera.close_webrtc_session("known-session-abc")

        mock_provider.async_close_session.assert_called_once_with("known-session-abc")

    def test_other_exceptions_from_provider_still_propagate(self, camera: Any) -> None:
        """Non-KeyError exceptions from the provider must still surface.

        Only KeyError is the expected "session not found" signal from go2rtc.
        Other errors (e.g. RuntimeError, TypeError) indicate real bugs and must
        not be swallowed.
        """
        mock_provider = MagicMock()
        mock_provider.async_close_session.side_effect = RuntimeError("unexpected")
        camera._webrtc_provider = mock_provider

        with pytest.raises(RuntimeError, match="unexpected"):
            camera.close_webrtc_session("some-session")

    def test_multiple_close_calls_are_idempotent(self, camera: Any) -> None:
        """Calling close_webrtc_session twice for the same ID must not raise.

        The second call will KeyError (session already popped on first call)
        and must be silently discarded.
        """
        mock_provider = MagicMock()
        call_count = 0

        def pop_session(session_id: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise KeyError(session_id)

        mock_provider.async_close_session.side_effect = pop_session
        camera._webrtc_provider = mock_provider

        camera.close_webrtc_session("dup-session")  # first call — OK
        camera.close_webrtc_session("dup-session")  # second call — KeyError → no-op


# ── async_create_stream with privacy mode ──────────────────────────────────


class TestAsyncCreateStreamPrivacy:
    """async_create_stream must raise HomeAssistantError when privacy is ON.

    Previously it called try_live_connection (which also gates on privacy),
    got None back, logged WARNING "play_stream — live connection failed", and
    returned None.  HA then raised HomeAssistantError("does not support play
    stream service") — an opaque ERROR that confused users.

    After the fix: when privacy mode is detected before the live-connection
    attempt, a HomeAssistantError with a descriptive message is raised
    immediately, skipping the pointless try_live_connection round-trip.
    """

    @pytest.mark.asyncio
    async def test_raises_home_assistant_error_when_privacy_on(
        self, camera: Any, stub_coord: SimpleNamespace
    ) -> None:
        """Privacy ON → HomeAssistantError with 'privacy mode' in message."""
        from homeassistant.exceptions import HomeAssistantError

        stub_coord._shc_state_cache[CAM_ID] = {"privacy_mode": True}
        stub_coord._live_connections = {}  # no active session

        with pytest.raises(HomeAssistantError, match="privacy mode"):
            await camera.async_create_stream()

    @pytest.mark.asyncio
    async def test_no_error_when_privacy_off_and_connection_exists(
        self, camera: Any, stub_coord: SimpleNamespace
    ) -> None:
        """Privacy OFF + active live_connection → delegates to super() without error."""
        from homeassistant.exceptions import HomeAssistantError

        stub_coord._shc_state_cache[CAM_ID] = {"privacy_mode": False}
        # Simulate an active live connection so the branch skips try_live_connection
        stub_coord._live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy.example.com:443/hash/rtsp_tunnel",
            "_connection_type": "REMOTE",
        }

        # super().async_create_stream() tries to call stream_source() which
        # returns a URL, then calls create_stream(hass, …) — we need hass.
        # Just assert we do NOT raise HomeAssistantError (i.e. we get past our
        # privacy gate).  The super() call may fail for other reasons (no hass)
        # but not because of our privacy check.
        try:
            await camera.async_create_stream()
        except HomeAssistantError as exc:
            assert "privacy mode" not in str(exc), (
                f"Got unexpected privacy-mode error with privacy OFF: {exc}"
            )
        except Exception:
            pass  # super() needs hass — AttributeError/TypeError is expected here

    @pytest.mark.asyncio
    async def test_no_error_when_privacy_state_unknown(
        self, camera: Any, stub_coord: SimpleNamespace
    ) -> None:
        """Privacy state not in cache (None/missing) → must not raise HAError.

        When the coordinator has not yet fetched privacy state, we must not
        block the stream — fail open (attempt live connection as usual).
        """
        from homeassistant.exceptions import HomeAssistantError

        stub_coord._shc_state_cache = {}  # no entry for this cam
        stub_coord._live_connections = {}

        # try_live_connection will be called; mock it to return None (unavailable)
        async def fake_try_live_connection(cam_id: str) -> None:
            return None

        stub_coord.try_live_connection = fake_try_live_connection

        # Should NOT raise HomeAssistantError (privacy-mode branch not taken)
        try:
            await camera.async_create_stream()
        except HomeAssistantError as exc:
            assert "privacy mode" not in str(exc), (
                f"Unexpected privacy-mode error when privacy state is unknown: {exc}"
            )
        except Exception:
            pass  # other errors from missing hass are fine
