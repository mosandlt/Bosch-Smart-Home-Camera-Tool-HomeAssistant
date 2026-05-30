"""Cover the `_died_callback` bridge inside `BoschCameraCoordinator._start_tls_proxy`.

This callback hops from the proxy daemon thread back to the HA event loop
via `hass.loop.call_soon_threadsafe` + schedules `_on_tls_proxy_died` as
a task. The `except RuntimeError: pass` arm catches "event loop closed"
during HA shutdown. Pins __init__.py L4245-4252.
"""

from __future__ import annotations

import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator


def _coord_stub():
    coord = SimpleNamespace()
    coord._tls_ssl_ctx = ssl.create_default_context()
    coord._tls_proxy_ports = {}
    coord._on_tls_proxy_died = AsyncMock(return_value=None)
    coord.hass = MagicMock()
    coord.hass.async_add_executor_job = AsyncMock()
    coord.hass.async_create_task = MagicMock()
    coord.hass.loop = MagicMock()
    # Default call_soon_threadsafe runs the callback synchronously.
    coord.hass.loop.call_soon_threadsafe = MagicMock(
        side_effect=lambda cb, *a: cb(*a),
    )
    return coord


@pytest.mark.asyncio
class TestDiedCallbackBridge:
    async def test_callback_hops_to_event_loop_and_schedules_task(self):
        """When the proxy fires the circuit breaker, the daemon-thread
        callback must `call_soon_threadsafe` a lambda that schedules
        `_on_tls_proxy_died(cam_id)`. Pins L4245-4246."""
        captured = {}

        def _fake_start(
            ssl_ctx,
            cam_id,
            cam_host,
            cam_port,
            ports,
            *,
            is_renewal=False,
            on_proxy_died=None,
        ):
            captured["cb"] = on_proxy_died
            return 50000

        coord = _coord_stub()
        with patch(
            "custom_components.bosch_shc_camera.start_tls_proxy",
            side_effect=_fake_start,
        ):
            port = await BoschCameraCoordinator._start_tls_proxy(
                coord,
                "CAM-A",
                "1.2.3.4",
                443,
            )
        assert port == 50000
        # Invoke the captured callback as if the proxy thread fired it.
        captured["cb"]()
        coord.hass.loop.call_soon_threadsafe.assert_called_once()
        coord.hass.async_create_task.assert_called_once()

    async def test_callback_swallows_runtime_error_during_shutdown(self):
        """`call_soon_threadsafe` raises RuntimeError("event loop is
        closed") when HA is shutting down. The bridge must swallow it
        silently so the proxy thread does not crash. Pins L4251-4252."""
        captured = {}

        def _fake_start(
            ssl_ctx,
            cam_id,
            cam_host,
            cam_port,
            ports,
            *,
            is_renewal=False,
            on_proxy_died=None,
        ):
            captured["cb"] = on_proxy_died
            return 50001

        coord = _coord_stub()
        coord.hass.loop.call_soon_threadsafe = MagicMock(
            side_effect=RuntimeError("event loop is closed"),
        )
        with patch(
            "custom_components.bosch_shc_camera.start_tls_proxy",
            side_effect=_fake_start,
        ):
            await BoschCameraCoordinator._start_tls_proxy(
                coord,
                "CAM-A",
                "1.2.3.4",
                443,
            )
        # Must NOT raise.
        captured["cb"]()
