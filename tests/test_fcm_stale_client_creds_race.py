"""Regression test (bug-hunt 2026-07-03): stale FCM client credential race.

`_on_creds_updated` fires from the Firebase SDK's own background thread (not
the HA event loop) whenever the client refreshes its credentials. A hard-heal
purges credentials and starts a brand-new client; if the OLD, now-replaced
client's callback fires late (after the new client already persisted fresh
credentials), it used to silently overwrite them with stale ones — defeating
the hard-heal it was meant to recover from.

Fix: `_try_fcm()` in fcm.py captures the client instance it created in a
closure variable (`_this_client`), and `_on_creds_updated`'s inner `_persist`
only proceeds if `coordinator._fcm_client is _this_client` at call time —
detecting that the coordinator has since moved on to a newer client.

This test drives the REAL `_async_start_fcm_push_locked` (not a mock of it)
twice, simulating two consecutive client generations, and fires the FIRST
generation's stale credentials_updated_callback after the second generation
is already active — pinning that the stale callback's persist is skipped.
"""

from __future__ import annotations

import asyncio
from threading import Lock
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera.fcm"


class _StubFcmClient:
    """Stub replacing the real (patched) FcmPushClient class.

    Records the credentials_updated_callback it was constructed with so the
    test can invoke it directly, simulating the Firebase SDK's background
    thread calling back into HA-land.
    """

    instances: ClassVar[list[_StubFcmClient]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.creds_cb = kwargs["credentials_updated_callback"]
        _StubFcmClient.instances.append(self)

    async def checkin_or_register(self) -> str:
        return "fake-token-" + str(len(_StubFcmClient.instances))

    async def start(self) -> None:
        return None

    def is_started(self) -> bool:
        return True


def _make_coord() -> SimpleNamespace:
    hass = MagicMock()
    # call_soon_threadsafe runs the callback immediately (synchronously) so
    # the test doesn't need a real event loop turn to observe the effect.
    hass.loop = SimpleNamespace(call_soon_threadsafe=lambda fn: fn())
    # Actually schedule the coroutine as a real task (not close() it) so the
    # test can await one loop tick and observe whether _fake_persist ran.
    hass.async_create_task = lambda coro: asyncio.get_event_loop().create_task(coro)
    hass.config_entries = SimpleNamespace(async_update_entry=MagicMock())

    return SimpleNamespace(
        token="tok-A",
        _fcm_token=None,
        _fcm_push_mode="unknown",
        _fcm_lock=Lock(),
        _fcm_running=False,
        _fcm_healthy=False,
        _fcm_client=None,
        _fcm_started_at=float("-inf"),
        _entry=SimpleNamespace(
            data={
                "fcm_config": {
                    "api_key": "fake-key",
                    "project_id": "fake-project",
                    "app_id": "fake-app",
                },
            }
        ),
        options={"enable_fcm_push": True, "fcm_push_mode": "auto"},
        hass=hass,
    )


@pytest.mark.asyncio
async def test_stale_client_creds_callback_is_ignored_after_hard_heal() -> None:
    from custom_components.bosch_shc_camera import fcm

    _StubFcmClient.instances = []
    coord = _make_coord()

    persisted: list[dict[str, Any]] = []

    async def _fake_persist(_coord: object, creds: dict[str, Any]) -> None:
        persisted.append(creds)

    with (
        patch.object(fcm, "_get_fcm_push_client_class", return_value=_StubFcmClient),
        patch.object(fcm, "register_fcm_with_bosch", new=AsyncMock(return_value=True)),
        patch.object(fcm, "_async_persist_fcm_creds", new=_fake_persist),
    ):
        # Generation 1 (e.g. the client running before a hard-heal).
        started_1 = await fcm._async_start_fcm_push_locked(coord)
        assert started_1 is True
        assert len(_StubFcmClient.instances) == 1
        gen1 = _StubFcmClient.instances[0]

        # Simulate a hard-heal: coordinator moves on to a fresh client
        # (mirrors async_stop_fcm_push + a second _async_start_fcm_push_locked
        # call in _async_run_fcm_supervisor).
        coord._fcm_running = False
        started_2 = await fcm._async_start_fcm_push_locked(coord)
        assert started_2 is True
        assert len(_StubFcmClient.instances) == 2
        gen2 = _StubFcmClient.instances[1]
        assert coord._fcm_client is gen2  # sanity: coordinator points at gen2
        assert gen2 is not gen1  # sanity: distinct client instances

        # Generation 2 is fresh: its own callback persists normally.
        gen2.creds_cb({"gen": 2})
        await asyncio.sleep(0)  # let the scheduled persist task run
        assert persisted == [{"gen": 2}]

        # The STALE generation-1 callback fires late (its own SDK thread was
        # never guaranteed to be covered by async_stop_fcm_push's drain-wait).
        # Without the fix this would silently overwrite the fresh gen-2 creds.
        gen1.creds_cb({"gen": 1, "stale": True})
        await asyncio.sleep(0)
        assert persisted == [{"gen": 2}], (
            "stale generation-1 credentials_updated_callback must NOT persist "
            "after the coordinator has moved on to generation 2"
        )
