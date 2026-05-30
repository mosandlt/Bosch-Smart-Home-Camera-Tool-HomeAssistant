"""Regression test for Bug #3 (2026-05-26): empty exception messages in
slow-tier fetch logs ("iconLedBrightness fetch error: " — 12× on Innenbereich)
hid the actual problem. Cause: `str(asyncio.TimeoutError())` returns "".

Fix: `_err_str` helper falls back to `repr(err)` when `str(err)` is empty.
"""

from __future__ import annotations

import asyncio

import aiohttp

from custom_components.bosch_shc_camera import BoschCameraCoordinator


class TestErrStrHelper:
    def test_non_empty_str_passes_through(self) -> None:
        err = ValueError("bad value")
        assert BoschCameraCoordinator._err_str(err) == "bad value"

    def test_empty_str_falls_back_to_repr(self) -> None:
        err = TimeoutError()
        # str(TimeoutError()) == ""
        assert str(err) == ""
        out = BoschCameraCoordinator._err_str(err)
        assert "TimeoutError" in out

    def test_aiohttp_clienterror_empty_falls_back(self) -> None:
        err = aiohttp.ClientError()
        out = BoschCameraCoordinator._err_str(err)
        assert "ClientError" in out

    def test_handles_arbitrary_base_exception(self) -> None:
        class Custom(BaseException):
            pass

        err = Custom()
        out = BoschCameraCoordinator._err_str(err)
        assert "Custom" in out
