"""Regression tests for the 2026-06-02 bug-hunt fix batch.

Each test pins the concrete defect → corrected behaviour.

- B  fcm._safe_path_segment: snapshot filename can't escape the alert dir.
- C  select.BoschFcmPushModeSelect: the FCM-restart task is tracked on the
     coordinator (cancelled on unload) instead of fire-and-forget.

(A privacy-cooldown sentinel lives in tests/test_switches.py; D card escaping
 in test/e2e/card-smoke.spec.mjs; E webhook scheme guard in
 tests/test_webhook_delivery.py.)

Source: https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


# ── B: fcm path-traversal guard ───────────────────────────────────────────────
class TestSafePathSegment:
    def test_normal_names_unchanged(self) -> None:
        from custom_components.bosch_shc_camera.fcm import _safe_path_segment

        assert _safe_path_segment("Terrasse") == "Terrasse"
        assert _safe_path_segment("Eyes Outdoor II") == "Eyes Outdoor II"

    def test_traversal_tokens_neutralised(self) -> None:
        from custom_components.bosch_shc_camera.fcm import _safe_path_segment

        out = _safe_path_segment("../../config/secrets")
        assert "/" not in out
        assert "\\" not in out
        assert ".." not in out

    def test_join_cannot_escape_alert_dir(self) -> None:
        """The concrete attack: a camera titled '../../config/secrets' must not
        resolve outside the alert directory."""
        from custom_components.bosch_shc_camera.fcm import _safe_path_segment

        alert_dir = "/config/www/bosch_alerts"
        seg = _safe_path_segment("../../config/secrets")
        path = os.path.join(alert_dir, f"{seg}_ts_MOVEMENT.jpg")
        assert os.path.abspath(path).startswith(os.path.abspath(alert_dir) + os.sep)

    def test_backslash_variant(self) -> None:
        from custom_components.bosch_shc_camera.fcm import _safe_path_segment

        assert "\\" not in _safe_path_segment("..\\..\\windows")


# ── C: FCM-restart task is tracked, not fire-and-forget ────────────────────────
@pytest.mark.asyncio
async def test_fcm_mode_select_tracks_restart_task() -> None:
    """Regression (bug-hunt 2026-06-02): selecting a new FCM push mode must
    register the async_start_fcm_push() task in coordinator._bg_tasks so
    async_unload_entry can cancel it — an untracked fire-and-forget task could
    keep running (and re-establish FCM) after the entry was unloaded."""
    from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

    coordinator = SimpleNamespace(
        data={CAM_ID: {"info": {"title": "Terrasse"}}},
        options={"enable_fcm_push": True},
        _fcm_push_mode="auto",
        _bg_tasks=set(),
        async_stop_fcm_push=AsyncMock(),
        async_start_fcm_push=AsyncMock(),
        last_update_success=True,
    )
    entry = SimpleNamespace(entry_id="01ENTRY", options={"fcm_push_mode": "auto"})

    sel = BoschFcmPushModeSelect(coordinator, CAM_ID, entry)
    # Stand in for the HA-managed attributes the entity would get once added.
    sel.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=MagicMock()),
        async_create_task=lambda coro, **kw: asyncio.ensure_future(coro),
    )
    sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]

    await sel.async_select_option("all")

    # Task is registered before it completes.
    assert len(coordinator._bg_tasks) == 1
    coordinator.async_stop_fcm_push.assert_awaited_once()

    # Let the scheduled task run, then a further tick for the done-callback
    # (add_done_callback fires via call_soon on the next loop iteration).
    for _ in range(5):
        await asyncio.sleep(0)
        if not coordinator._bg_tasks:
            break
    coordinator.async_start_fcm_push.assert_awaited_once()
    assert len(coordinator._bg_tasks) == 0
