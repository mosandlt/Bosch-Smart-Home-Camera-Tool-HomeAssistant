"""Regression: a privacy-mode toggle must NOT trigger a full coordinator refresh.

Incident 2026-05-29 (live-reproduced via browser + TLS-proxy logs): toggling
Terrasse privacy ON/OFF made the UNRELATED Innenbereich live stream blip — the
card showed an "HLS reload" overlay. Root cause (path C): after the cloud PUT
/privacy succeeds, async_cloud_set_privacy_mode scheduled
`coordinator.async_request_refresh()`. That forced (un-throttled) coordinator
tick re-touches go2rtc stream registration for every active camera, so go2rtc
sent an RTSP TEARDOWN + reconnect on the Innenbereich session (~1 s drop):

    06:48:38.775 cloud_set_privacy_mode: 11111111 (Terrasse) -> ON
    06:48:38.799 TLS proxy 22222222 (Innenbereich) [C→CAM] TEARDOWN ...
    06:48:39.3-8 → reconnect DESCRIBE→SETUP→PLAY

The privacy state is already pushed to the UI optimistically: the cache is
updated and `async_update_listeners()` is called immediately before the refresh.
The full refresh is therefore redundant for the toggled camera and destructive
for the others. Decouple it (option A): drop the forced refresh; the regular
60 s coordinator tick confirms the state, and the privacy-OFF snapshot trigger
still refreshes the image.

Pins: cloud-success path updates the cache + notifies listeners + stamps the
write-lock, and does NOT schedule async_request_refresh (ON and OFF).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _put_204_session():
    """aiohttp session whose .put(...) async-context-manager yields HTTP 204."""
    resp = MagicMock()
    resp.status = 204
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.put.return_value = cm
    return session


def _coord():
    return SimpleNamespace(
        token="tok-AAA",
        _cached_status={},
        _privacy_set_at={},
        _shc_state_cache={CAM_ID: {"device_id": "shc-dev-1"}},
        async_update_listeners=MagicMock(),
        async_request_refresh=AsyncMock(),
        hass=SimpleNamespace(async_create_task=MagicMock()),
    )


@pytest.mark.asyncio
async def test_privacy_on_does_not_request_full_refresh():
    from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

    coord = _coord()
    with patch(
        "custom_components.bosch_shc_camera.shc.async_get_clientsession",
        return_value=_put_204_session(),
    ):
        result = await async_cloud_set_privacy_mode(coord, CAM_ID, True)
    assert result is True
    assert coord._shc_state_cache[CAM_ID]["privacy_mode"] is True
    assert CAM_ID in coord._privacy_set_at
    coord.async_update_listeners.assert_called()  # optimistic UI push kept
    coord.async_request_refresh.assert_not_called()  # path C: no forced refresh
    coord.hass.async_create_task.assert_not_called()  # nothing scheduled


@pytest.mark.asyncio
async def test_privacy_off_does_not_request_full_refresh():
    from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

    coord = _coord()
    with (
        patch(
            "custom_components.bosch_shc_camera.shc.async_get_clientsession",
            return_value=_put_204_session(),
        ),
        patch(
            "custom_components.bosch_shc_camera.shc._schedule_privacy_off_snapshot"
        ) as mock_snap,
    ):
        result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)
    assert result is True
    assert coord._shc_state_cache[CAM_ID]["privacy_mode"] is False
    coord.async_update_listeners.assert_called()
    coord.async_request_refresh.assert_not_called()
    # The lightweight privacy-OFF snapshot trigger is still scheduled — it
    # refreshes the still image only, it is NOT a full coordinator refresh.
    mock_snap.assert_called_once()
