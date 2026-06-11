"""Regression: async_handle_fcm_push must not crash when coordinator.data is None.

Race window observed in production logs (HA 2026, FW 9.40.102):
FCM push arrives during integration setup, before the first coordinator
refresh has populated `self.data`. Pre-fix the handler raised
``AttributeError: 'NoneType' object has no attribute 'keys'`` at fcm.py:889
(4× shortly after `ha core restart`).

Source: production HA logs 2026-05-24, system_log entries name=homeassistant,
source=custom_components/bosch_shc_camera/fcm.py:889, count=4.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera.fcm"


def _coord(data: Any) -> Any:
    return SimpleNamespace(
        token="tok",
        hass=MagicMock(),
        data=data,
        _last_event_ids={},
        _alert_sent_ids={},
        _camera_entities={},
        _image_entities={},
        _shc_state_cache={},
        _cached_events={},
        _bg_tasks=set(),
        _hw_version={},
        options={},
    )


class TestPushDataNoneGuard:
    @pytest.mark.asyncio
    async def test_returns_early_when_data_is_none(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _coord(data=None)
        session = MagicMock()
        session.get = MagicMock(side_effect=AssertionError("must not call cloud API"))

        with patch(f"{MODULE}.async_get_bosch_cloud_session", new=AsyncMock(return_value=session)):
            await async_handle_fcm_push(coord)

        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_when_data_is_empty_dict(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _coord(data={})
        session = MagicMock()
        session.get = MagicMock(side_effect=AssertionError("must not call cloud API"))

        with patch(f"{MODULE}.async_get_bosch_cloud_session", new=AsyncMock(return_value=session)):
            await async_handle_fcm_push(coord)

        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_when_token_missing(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _coord(data={"cam-id": {"info": {}, "events": []}})
        coord.token = ""
        session = MagicMock()
        session.get = MagicMock(side_effect=AssertionError("must not call cloud API"))

        with patch(f"{MODULE}.async_get_bosch_cloud_session", new=AsyncMock(return_value=session)):
            await async_handle_fcm_push(coord)

        session.get.assert_not_called()
