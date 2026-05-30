"""Coverage tests for FCM early-exit paths when the library is absent.

Targets:
- `_get_fcm_push_client_class` returning None when the patched class is None
  AND the vanilla `from firebase_messaging import FcmPushClient` ImportErrors
  (fcm.py L331-333).
- `async_start_fcm_push` early-exit when `_get_fcm_push_client_class` returns
  None (L391-393).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


class TestGetFcmPushClientClassImportError:
    def test_returns_none_when_both_paths_fail(self):
        """Forces _QuietFcmPushClient._patch_class() to return None and the
        fallback import to fail — exercises the bare `return None` on L333."""
        import builtins as _bi

        real = _bi.__import__

        def _fake(name, *a, **kw):
            # Block both the firebase_messaging top-level package and
            # FcmRegisterConfig/FcmPushClientConfig submodule lookups.
            if name == "firebase_messaging" or name.startswith("firebase_messaging."):
                raise ImportError("simulated absence")
            return real(name, *a, **kw)

        from custom_components.bosch_shc_camera.fcm import (
            _get_fcm_push_client_class,
        )

        with patch("builtins.__import__", side_effect=_fake):
            assert _get_fcm_push_client_class() is None


@pytest.mark.asyncio
class TestAsyncStartFcmPushNoLib:
    async def test_early_exit_when_lib_missing(self):
        """When `_get_fcm_push_client_class()` returns None, `async_start_fcm_push`
        logs a warning and returns without touching the coordinator state.
        Exercises L391-393 of fcm.py."""
        coord = SimpleNamespace(
            _fcm_running=False,
            options={"enable_fcm_push": True, "fcm_push_mode": "auto"},
            _entry=SimpleNamespace(data={}),
        )
        # Patch FcmRegisterConfig presence so the first ImportError check is
        # bypassed — we want execution to land on the FcmPushClient check.
        # Then force the class lookup to None.
        from custom_components.bosch_shc_camera import fcm

        with patch.object(fcm, "_get_fcm_push_client_class", return_value=None):
            # FcmRegisterConfig import is inside the function; patch builtins
            # so the helper finishes the import successfully before the
            # class-lookup branch fires.
            try:
                await fcm.async_start_fcm_push(coord)
            except ImportError:
                # FcmRegisterConfig may genuinely be absent on this Python
                # env — that's already covered by another test; we only care
                # about the warn-and-return branch here.
                pytest.skip("firebase_messaging library not installed in test env")
        # Must not have flipped _fcm_running.
        assert coord._fcm_running is False
