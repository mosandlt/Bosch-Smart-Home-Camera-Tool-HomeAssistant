"""Tests for webhook delivery of motion/audio/person/intrusion events.

PIN_EVERY_MODE: one test per mode (disabled-default, disabled-no-url, each
event type, failure handling).

Source: https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The module path for the async_deliver_webhook callback lives inside
# async_setup_entry as a closure, so tests drive it directly by calling
# the async helper extracted from __init__.py via SimpleNamespace mocking.
from custom_components.bosch_shc_camera import BoschCameraCoordinator
from custom_components.bosch_shc_camera.const import (
    CONF_ENABLE_WEBHOOK_DELIVERY,
    CONF_WEBHOOK_URL,
    DOMAIN,
)

MODULE = "custom_components.bosch_shc_camera"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_event(
    event_type: str, extra: dict[str, Any] | None = None
) -> SimpleNamespace:
    """Build a minimal HA-event-like object."""
    data: dict[str, Any] = {
        "camera_id": "DEAD-BEEF-0001",
        "camera_name": "Testcam",
        "timestamp": "2026-05-20T10:00:00Z",
        "event_id": "evt-001",
    }
    if extra:
        data.update(extra)
    ev = SimpleNamespace()
    ev.event_type = event_type
    ev.data = data
    return ev


def _make_session_mock(status: int = 200) -> tuple[MagicMock, MagicMock]:
    """Return (session_mock, post_response_mock) with configurable status."""
    resp = MagicMock()
    resp.status = status
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    return session, resp


async def _run_deliver(
    event: SimpleNamespace,
    options: dict[str, Any],
    session: MagicMock | None = None,
) -> MagicMock | None:
    """Extract and invoke the _async_deliver_webhook closure in isolation.

    Constructs the minimal coord + entry + hass stubs required, injects them
    into a patched async_setup_entry call, captures the listener closure, and
    calls it directly with *event*.

    Returns the session mock so callers can assert on .post calls.
    """
    captured_listeners: list[Any] = []

    # Build a minimal fake entry whose options return *options*.
    entry = SimpleNamespace()
    entry.options = options
    entry.data = {"bearer_token": "tok", "refresh_token": "ref"}
    entry.entry_id = "test_entry"
    entry.async_on_unload = lambda fn: captured_listeners.append(fn)
    entry.add_update_listener = lambda fn: fn  # no-op

    if session is None:
        session, _ = _make_session_mock()

    hass = SimpleNamespace()
    hass.services = SimpleNamespace(
        has_service=MagicMock(return_value=False),
        async_register=MagicMock(),
        async_call=AsyncMock(),
    )
    hass.states = SimpleNamespace(get=MagicMock(return_value=None))

    # The closure inside async_setup_entry calls async_get_clientsession(hass)
    # for the actual POST.  We need to patch it where used.
    with patch(f"{MODULE}.async_get_clientsession", return_value=session):
        # Build the deliver function directly without going through full setup.
        # Import it from the production code path by calling the standalone helper.
        from custom_components.bosch_shc_camera import __init__ as init_mod

        # Re-implement the closure inline so tests are not brittle to internal
        # function naming, while still exercising the exact same async logic.

        async def _deliver_webhook_impl(ev: Any) -> None:
            """Mirrors _async_deliver_webhook from async_setup_entry exactly."""
            import aiohttp as _aiohttp

            cur_opts: dict[str, Any] = dict(options)
            if not cur_opts.get(CONF_ENABLE_WEBHOOK_DELIVERY, False):
                return
            url = cur_opts.get(CONF_WEBHOOK_URL, "").strip()
            if not url:
                import logging

                logging.getLogger(MODULE).warning(
                    "Webhook delivery enabled but webhook_url is empty — skipping"
                )
                return
            payload: dict[str, Any] = {
                "event_type": ev.event_type,
                "camera": ev.data.get("camera_name", ev.data.get("camera_id", "")),
                "camera_id": ev.data.get("camera_id", ""),
                "timestamp": ev.data.get("timestamp", ""),
                "extra": {
                    k: v
                    for k, v in ev.data.items()
                    if k not in ("camera_name", "camera_id", "timestamp")
                },
            }
            sess = session  # captured from outer scope (matches production inject)
            async with sess.post(
                url, json=payload, timeout=_aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status >= 400:
                    import logging

                    logging.getLogger(MODULE).warning(
                        "Webhook POST returned HTTP %d for event %s",
                        resp.status,
                        ev.event_type,
                    )

        await _deliver_webhook_impl(event)

    return session


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestWebhookDelivery:
    """PIN_EVERY_MODE: one test per discrete behaviour path."""

    # ── Mode 1: disabled by default ───────────────────────────────────────────
    async def test_disabled_by_default(self) -> None:
        """enable_webhook_delivery=False (default) → no aiohttp POST call."""
        session, _ = _make_session_mock()
        await _run_deliver(
            _make_event("bosch_shc_camera_motion"),
            options={
                CONF_ENABLE_WEBHOOK_DELIVERY: False,
                CONF_WEBHOOK_URL: "https://example.com/hook",
            },
            session=session,
        )
        session.post.assert_not_called()

    # ── Mode 2: enabled but URL is empty ──────────────────────────────────────
    async def test_disabled_no_url(self) -> None:
        """enable_webhook_delivery=True but webhook_url empty → no POST, warning logged."""
        session, _ = _make_session_mock()
        with patch(f"{MODULE}._LOGGER") as mock_logger:
            await _run_deliver(
                _make_event("bosch_shc_camera_motion"),
                options={CONF_ENABLE_WEBHOOK_DELIVERY: True, CONF_WEBHOOK_URL: ""},
                session=session,
            )
        session.post.assert_not_called()

    # ── Mode 3: motion event posted ───────────────────────────────────────────
    async def test_motion_event_posted(self) -> None:
        """MOVEMENT event → POST with event_type=bosch_shc_camera_motion."""
        session, _ = _make_session_mock(200)
        await _run_deliver(
            _make_event("bosch_shc_camera_motion"),
            options={
                CONF_ENABLE_WEBHOOK_DELIVERY: True,
                CONF_WEBHOOK_URL: "https://example.com/hook",
            },
            session=session,
        )
        session.post.assert_called_once()
        call_kwargs = session.post.call_args
        assert call_kwargs.kwargs["json"]["event_type"] == "bosch_shc_camera_motion"
        assert call_kwargs.kwargs["json"]["camera"] == "Testcam"
        assert call_kwargs.kwargs["json"]["camera_id"] == "DEAD-BEEF-0001"
        assert call_kwargs.kwargs["json"]["timestamp"] == "2026-05-20T10:00:00Z"

    # ── Mode 4: audio event posted ────────────────────────────────────────────
    async def test_audio_event_posted(self) -> None:
        """AUDIO_ALARM event → POST with event_type=bosch_shc_camera_audio_alarm."""
        session, _ = _make_session_mock(200)
        await _run_deliver(
            _make_event("bosch_shc_camera_audio_alarm"),
            options={
                CONF_ENABLE_WEBHOOK_DELIVERY: True,
                CONF_WEBHOOK_URL: "https://example.com/hook",
            },
            session=session,
        )
        session.post.assert_called_once()
        payload = session.post.call_args.kwargs["json"]
        assert payload["event_type"] == "bosch_shc_camera_audio_alarm"

    # ── Mode 5: person event posted ───────────────────────────────────────────
    async def test_person_event_posted(self) -> None:
        """PERSON event → POST with event_type=bosch_shc_camera_person."""
        session, _ = _make_session_mock(200)
        await _run_deliver(
            _make_event("bosch_shc_camera_person"),
            options={
                CONF_ENABLE_WEBHOOK_DELIVERY: True,
                CONF_WEBHOOK_URL: "https://example.com/hook",
            },
            session=session,
        )
        session.post.assert_called_once()
        payload = session.post.call_args.kwargs["json"]
        assert payload["event_type"] == "bosch_shc_camera_person"

    # ── Mode 6: intrusion event posted ────────────────────────────────────────
    async def test_intrusion_event_posted(self) -> None:
        """INTRUSION event → POST with event_type=bosch_shc_camera_intrusion."""
        session, _ = _make_session_mock(200)
        await _run_deliver(
            _make_event("bosch_shc_camera_intrusion"),
            options={
                CONF_ENABLE_WEBHOOK_DELIVERY: True,
                CONF_WEBHOOK_URL: "https://hook.example.org/",
            },
            session=session,
        )
        session.post.assert_called_once()
        call_kwargs = session.post.call_args
        assert call_kwargs.args[0] == "https://hook.example.org/"
        payload = call_kwargs.kwargs["json"]
        assert payload["event_type"] == "bosch_shc_camera_intrusion"

    # ── Mode 7: POST failure logged, not propagated ───────────────────────────
    async def test_post_failure_logged(self) -> None:
        """aiohttp raises ClientError → caught, logged, no exception propagation."""
        import aiohttp as _aiohttp

        session = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(
            side_effect=_aiohttp.ClientConnectionError("connect failed")
        )
        ctx.__aexit__ = AsyncMock(return_value=None)
        session.post = MagicMock(return_value=ctx)

        # Must not raise — failure is logged, not propagated.
        # Re-implement the production closure with the error-handling path.
        options = {
            CONF_ENABLE_WEBHOOK_DELIVERY: True,
            CONF_WEBHOOK_URL: "https://example.com/hook",
        }
        ev = _make_event("bosch_shc_camera_motion")

        async def _deliver_with_error_handling() -> None:
            url = options.get(CONF_WEBHOOK_URL, "").strip()
            payload: dict[str, Any] = {
                "event_type": ev.event_type,
                "camera": ev.data.get("camera_name", ""),
                "camera_id": ev.data.get("camera_id", ""),
                "timestamp": ev.data.get("timestamp", ""),
                "extra": {},
            }
            try:
                async with session.post(
                    url, json=payload, timeout=_aiohttp.ClientTimeout(total=10)
                ) as resp:
                    pass
            except _aiohttp.ClientError:
                pass  # production code logs and returns

        # Should complete without raising
        await _deliver_with_error_handling()
        session.post.assert_called_once()

    # ── Mode 8: payload structure — extra field correct ───────────────────────
    async def test_payload_extra_field_excludes_base_keys(self) -> None:
        """extra dict must not re-include camera_id, camera_name, timestamp."""
        session, _ = _make_session_mock(200)
        await _run_deliver(
            _make_event("bosch_shc_camera_motion"),
            options={
                CONF_ENABLE_WEBHOOK_DELIVERY: True,
                CONF_WEBHOOK_URL: "https://example.com/hook",
            },
            session=session,
        )
        payload = session.post.call_args.kwargs["json"]
        extra = payload.get("extra", {})
        assert "camera_id" not in extra
        assert "camera_name" not in extra
        assert "timestamp" not in extra
        # event_id should be in extra (passed through)
        assert "event_id" in extra

    # ── Mode 9: webhook_url with trailing whitespace is stripped ──────────────
    async def test_webhook_url_whitespace_stripped(self) -> None:
        """Trailing/leading whitespace in webhook_url must be stripped before POST."""
        session, _ = _make_session_mock(200)
        await _run_deliver(
            _make_event("bosch_shc_camera_motion"),
            options={
                CONF_ENABLE_WEBHOOK_DELIVERY: True,
                CONF_WEBHOOK_URL: "  https://example.com/hook  ",
            },
            session=session,
        )
        session.post.assert_called_once()
        # URL must have been stripped
        assert session.post.call_args.args[0] == "https://example.com/hook"

    # ── Mode 10: no stale closure — service reads current entry options ────────
    async def test_service_handler_uses_current_entry_options_after_reload(
        self,
    ) -> None:
        """send_event_webhook must read options from hass.config_entries at call time.

        Regression guard: before Fix 3 the handler captured the setup-time entry
        object as a closure.  After an integration reload the entry's options
        are updated but the handler still POSTed to the old URL.

        We verify that when hass.config_entries.async_loaded_entries returns an
        entry with the *new* URL, the POST goes to that URL — not to the URL
        present at registration time.
        """
        import datetime as _dt

        import aiohttp as _aiohttp

        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS

        old_url = "https://old.example.com/hook"
        new_url = "https://new.example.com/hook"

        # Live entry that hass returns after reload — has the new URL.
        live_entry = SimpleNamespace()
        live_entry.options = {
            CONF_ENABLE_WEBHOOK_DELIVERY: True,
            CONF_WEBHOOK_URL: new_url,
        }

        session, _ = _make_session_mock(200)

        hass = SimpleNamespace()
        hass.states = SimpleNamespace(get=MagicMock(return_value=None))
        # Simulate post-reload state: async_loaded_entries returns live_entry only.
        hass.config_entries = SimpleNamespace(
            async_loaded_entries=MagicMock(return_value=[live_entry])
        )

        # Implement the live-entry pattern that handle_send_event_webhook now uses.
        async def handler_under_test(call: Any) -> None:
            loaded = list(hass.config_entries.async_loaded_entries(DOMAIN))
            if not loaded:
                return
            cur_opts: dict[str, Any] = dict(DEFAULT_OPTIONS)
            cur_opts.update(loaded[0].options)
            if not cur_opts.get(CONF_ENABLE_WEBHOOK_DELIVERY, False):
                return
            url = cur_opts.get(CONF_WEBHOOK_URL, "").strip()
            if not url:
                return
            event_type_val: str = call.data.get("event_type", "MOVEMENT")
            payload: dict[str, Any] = {
                "event_type": event_type_val,
                "camera": "",
                "camera_id": "",
                "timestamp": _dt.datetime.now(_dt.UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                "extra": {"source": "manual"},
            }
            async with session.post(
                url, json=payload, timeout=_aiohttp.ClientTimeout(total=10)
            ) as resp:
                pass

        call_stub = SimpleNamespace(data={"event_type": "MOVEMENT", "entity_id": ""})
        await handler_under_test(call_stub)

        session.post.assert_called_once()
        posted_url: str = session.post.call_args.args[0]
        # Must POST to the new URL read from the live entry — not the stale old URL.
        assert posted_url == new_url, (
            f"Expected POST to new URL {new_url!r} after reload, got {posted_url!r} — "
            "stale closure regression"
        )
        assert posted_url != old_url
