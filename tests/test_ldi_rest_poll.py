"""Local REST reads in the coordinator tick for local-data-interface cameras.

Pins: at most one read per minute per camera (firmware hourly), only for
cameras reached over the local interface (the Terrasse case — interface
active, no password — is never asked and stays on the cloud path), REST as
the truth for reachability / wrong password / privacy, and the Repairs issue
driven by it.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import (
    BoschCameraCoordinator,
    ldi_local,
    ldi_rest,
)
from custom_components.bosch_shc_camera.ldi_local import (
    LDI_REST_POLL_SEC,
    RESULT_AUTH,
    RESULT_NO_GO2RTC,
    RESULT_UNREACHABLE,
    ldi_local_firmware,
    ldi_privacy_on,
    record_ldi_result,
    refresh_ldi_rest,
)
from custom_components.bosch_shc_camera.repairs import (
    LDI_UNREACHABLE_GRACE_SEC,
    refresh_local_data_interface_auth_issue,
)
from tests.test_ldi_lifecycle import _repairs_coord
from tests.test_ldi_local import CAM, IP, PW, _coord, _probe

MODULE = "custom_components.bosch_shc_camera"


def _patched_probe(result: ldi_rest.LdiProbe) -> Any:
    return patch(
        f"{MODULE}.ldi_local.ldi_rest.probe_camera",
        new=AsyncMock(return_value=result),
    )


class TestThrottle:
    @pytest.mark.asyncio
    async def test_first_read_asks_camera_and_stores_the_truth(self) -> None:
        c = _coord()
        with _patched_probe(_probe(200, True)) as probe:
            result = await refresh_ldi_rest(c, CAM)  # type: ignore[arg-type]
        assert result is not None and result.privacy_on is True
        probe.assert_awaited_once_with(c.hass, IP, "localuser", PW, want_version=True)
        assert ldi_privacy_on(c, CAM) is True  # type: ignore[arg-type]
        assert ldi_local_firmware(c, CAM) == "9.40.0202"  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_second_read_within_a_minute_is_skipped(self) -> None:
        c = _coord()
        with _patched_probe(_probe(200)) as probe:
            await refresh_ldi_rest(c, CAM)  # type: ignore[arg-type]
            assert await refresh_ldi_rest(c, CAM) is None  # type: ignore[arg-type]
        assert probe.await_count == 1

    @pytest.mark.asyncio
    async def test_overlapping_ticks_do_not_stack_requests(self) -> None:
        """The stamp is taken before the request, not after the answer."""
        c = _coord()
        seen: list[float] = []

        async def slow_probe(*_a: Any, **_k: Any) -> ldi_rest.LdiProbe:
            seen.append(c.ldi_rest_state[CAM]["checked_at"])
            return _probe(200)

        with patch(f"{MODULE}.ldi_local.ldi_rest.probe_camera", new=slow_probe):
            await refresh_ldi_rest(c, CAM)  # type: ignore[arg-type]
        assert seen[0] > float("-inf")

    @pytest.mark.asyncio
    async def test_reads_again_after_the_interval_without_firmware(self) -> None:
        c = _coord()
        with _patched_probe(_probe(200)) as probe:
            await refresh_ldi_rest(c, CAM)  # type: ignore[arg-type]
            c.ldi_rest_state[CAM]["checked_at"] -= LDI_REST_POLL_SEC + 1
            await refresh_ldi_rest(c, CAM)  # type: ignore[arg-type]
        assert probe.await_count == 2
        assert probe.await_args_list[0].kwargs["want_version"] is True
        assert probe.await_args_list[1].kwargs["want_version"] is False

    @pytest.mark.asyncio
    async def test_firmware_is_read_again_hourly(self) -> None:
        c = _coord()
        with _patched_probe(_probe(200)) as probe:
            await refresh_ldi_rest(c, CAM)  # type: ignore[arg-type]
            c.ldi_rest_state[CAM]["checked_at"] -= 3700
            c.ldi_rest_state[CAM]["version_at"] -= 3700
            await refresh_ldi_rest(c, CAM)  # type: ignore[arg-type]
        assert probe.await_args_list[1].kwargs["want_version"] is True

    @pytest.mark.asyncio
    async def test_firmware_retried_each_poll_until_known(self) -> None:
        c = _coord()
        no_fw = ldi_rest.LdiProbe(ldi_rest.PROBE_OK, False, None)
        with _patched_probe(no_fw) as probe:
            await refresh_ldi_rest(c, CAM)  # type: ignore[arg-type]
            c.ldi_rest_state[CAM]["checked_at"] -= LDI_REST_POLL_SEC + 1
            c.ldi_rest_state[CAM]["version_at"] -= LDI_REST_POLL_SEC + 1
            await refresh_ldi_rest(c, CAM)  # type: ignore[arg-type]
            # version_at is young, but firmware is still unknown
        assert probe.await_args_list[1].kwargs["want_version"] is True
        assert ldi_local_firmware(c, CAM) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_unknown_firmware_retries_are_capped_then_hourly(self) -> None:
        """A model without the version endpoint must not cost 2 GETs/minute."""
        c = _coord()
        no_fw = ldi_rest.LdiProbe(ldi_rest.PROBE_OK, False, None)
        with _patched_probe(no_fw) as probe:
            for _ in range(6):
                await refresh_ldi_rest(c, CAM, force=True)  # type: ignore[arg-type]
        flags = [x.kwargs["want_version"] for x in probe.await_args_list]
        assert flags == [True, True, True, False, False, False]

    @pytest.mark.asyncio
    async def test_force_ignores_the_throttle(self) -> None:
        c = _coord()
        with _patched_probe(_probe(200)) as probe:
            await refresh_ldi_rest(c, CAM)  # type: ignore[arg-type]
            await refresh_ldi_rest(c, CAM, force=True)  # type: ignore[arg-type]
        assert probe.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kw",
        [
            {"passwords": {}},  # interface active, no password: Terrasse case
            {"state": "inactive"},
            {"state": "unsupported"},
            {"ip": None},  # no address known
        ],
    )
    async def test_cameras_not_on_the_local_path_are_never_asked(
        self, kw: dict[str, Any]
    ) -> None:
        c = _coord(**kw)
        with _patched_probe(_probe(200)) as probe:
            assert await refresh_ldi_rest(c, CAM) is None  # type: ignore[arg-type]
        probe.assert_not_awaited()
        assert getattr(c, "ldi_rest_state", {}) == {}


class TestApplyProbe:
    def test_wrong_password_is_recorded_as_auth(self) -> None:
        c = _coord()
        ldi_local._apply_probe(c, CAM, _probe(401))  # type: ignore[arg-type]
        assert c.ldi_open_status[CAM]["reason"] == RESULT_AUTH

    @pytest.mark.parametrize("status", [None, 500])
    def test_no_answer_or_error_is_recorded_as_unreachable(
        self, status: int | None
    ) -> None:
        c = _coord()
        ldi_local._apply_probe(c, CAM, _probe(status))  # type: ignore[arg-type]
        assert c.ldi_open_status[CAM]["reason"] == RESULT_UNREACHABLE

    @pytest.mark.parametrize("earlier", [RESULT_AUTH, RESULT_UNREACHABLE])
    def test_camera_answering_again_clears_reachability_failures(
        self, earlier: str
    ) -> None:
        c = _coord()
        record_ldi_result(c, CAM, earlier)  # type: ignore[arg-type]
        ldi_local._apply_probe(c, CAM, _probe(200))  # type: ignore[arg-type]
        assert CAM not in c.ldi_open_status

    def test_a_go2rtc_failure_is_not_cleared_by_the_camera_answering(self) -> None:
        c = _coord()
        record_ldi_result(c, CAM, RESULT_NO_GO2RTC)  # type: ignore[arg-type]
        ldi_local._apply_probe(c, CAM, _probe(200))  # type: ignore[arg-type]
        assert c.ldi_open_status[CAM]["reason"] == RESULT_NO_GO2RTC

    @pytest.mark.parametrize("status", [None, 401, 500])
    def test_failed_read_forgets_privacy_but_keeps_firmware(
        self, status: int | None
    ) -> None:
        c = _coord()
        c.shc_state_cache = {CAM: {"privacy_mode": False}}
        ldi_local._apply_probe(c, CAM, _probe(200, True))  # type: ignore[arg-type]
        assert ldi_privacy_on(c, CAM) is True  # type: ignore[arg-type]
        ldi_local._apply_probe(c, CAM, _probe(status))  # type: ignore[arg-type]
        # a stale "on" must not hide an unreachable camera: cloud flag again
        assert ldi_privacy_on(c, CAM) is False  # type: ignore[arg-type]
        assert ldi_local_firmware(c, CAM) == "9.40.0202"  # type: ignore[arg-type]

    def test_stub_without_status_map(self) -> None:
        c = SimpleNamespace()
        ldi_local._apply_probe(c, CAM, _probe(200))  # type: ignore[arg-type]
        assert c.ldi_rest_state[CAM]["result"] == ldi_rest.PROBE_OK


class TestPrivacyTruth:
    @pytest.mark.parametrize(
        ("local", "cloud", "expected"),
        [
            (True, False, True),  # the camera wins over a stale cloud flag
            (False, True, False),
            (None, True, True),
            (None, False, False),
            (None, None, None),
            (True, None, True),
        ],
    )
    def test_local_answer_first_else_cloud(
        self, local: bool | None, cloud: bool | None, expected: bool | None
    ) -> None:
        c = _coord()
        c.shc_state_cache = {CAM: {"privacy_mode": cloud}}
        c.ldi_rest_state = {CAM: {"privacy_on": local}}
        assert ldi_privacy_on(c, CAM) is expected  # type: ignore[arg-type]

    def test_garbage_values_are_unknown(self) -> None:
        c = _coord()
        c.shc_state_cache = {CAM: {"privacy_mode": "yes"}}
        c.ldi_rest_state = {CAM: {"privacy_on": "on"}}
        assert ldi_privacy_on(c, CAM) is None  # type: ignore[arg-type]

    def test_bare_stub(self) -> None:
        assert ldi_privacy_on(SimpleNamespace(), CAM) is None  # type: ignore[arg-type]
        assert ldi_local_firmware(SimpleNamespace(), CAM) is None  # type: ignore[arg-type]

    def test_firmware_garbage_is_unknown(self) -> None:
        c = SimpleNamespace(ldi_rest_state={CAM: {"firmware": 9}})
        assert ldi_local_firmware(c, CAM) is None  # type: ignore[arg-type]


class TestCoordinatorTick:
    def _tick(self, coord: SimpleNamespace, wanted: set[str]) -> list[str]:
        spawned: list[str] = []

        def spawn(coro: Any, name: str) -> None:
            coro.close()
            spawned.append(name)

        coord.spawn_tracked = spawn
        with patch(
            f"{MODULE}.coordinator.ldi_wanted",
            side_effect=lambda _c, cid: cid in wanted,
        ):
            BoschCameraCoordinator._poll_ldi_rest(coord)  # type: ignore[arg-type]
        return spawned

    def test_only_local_cameras_are_polled(self) -> None:
        other = "22222222-2222-2222-2222-222222222222"
        coord = SimpleNamespace(data={CAM: {}, other: {}})
        assert self._tick(coord, {CAM}) == ["bosch_shc_camera_ldi_rest_11111111"]

    def test_cloud_cameras_only_means_no_requests_at_all(self) -> None:
        coord = SimpleNamespace(data={CAM: {}})
        assert self._tick(coord, set()) == []

    def test_no_data_yet(self) -> None:
        assert self._tick(SimpleNamespace(data=None), {CAM}) == []

    def test_is_part_of_the_tick_and_purged_with_the_camera(self) -> None:
        names = BoschCameraCoordinator._PURGE_CAM_DICT_ATTRS
        assert "ldi_rest_state" in names and "ldi_snapshot_state" in names


class TestRepairsFromRest:
    @patch(f"{MODULE}.ir")
    def test_wrong_password_from_rest_raises_the_issue(self, ir: MagicMock) -> None:
        c = _repairs_coord()
        ldi_local._apply_probe(c, CAM, _probe(401))  # type: ignore[arg-type]
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        assert (
            ir.async_create_issue.call_args.kwargs["translation_key"]
            == "local_data_interface_wrong_password"
        )

    @patch(f"{MODULE}.ir")
    def test_fixed_password_clears_it_on_the_next_read(self, ir: MagicMock) -> None:
        c = _repairs_coord()
        ldi_local._apply_probe(c, CAM, _probe(401))  # type: ignore[arg-type]
        ldi_local._apply_probe(c, CAM, _probe(200))  # type: ignore[arg-type]
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        ir.async_create_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_long_silence_from_rest_raises_unreachable(self, ir: MagicMock) -> None:
        c = _repairs_coord()
        ldi_local._apply_probe(c, CAM, _probe(None))  # type: ignore[arg-type]
        c.ldi_open_status[CAM]["since"] = (
            time.monotonic() - LDI_UNREACHABLE_GRACE_SEC - 5
        )
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        assert (
            ir.async_create_issue.call_args.kwargs["translation_key"]
            == "local_data_interface_unreachable"
        )

    @patch(f"{MODULE}.ir")
    def test_privacy_from_the_camera_is_never_a_failure(self, ir: MagicMock) -> None:
        """Cloud flag says off (stale), the camera itself says privacy is on."""
        c = _repairs_coord(
            status={
                "reason": RESULT_UNREACHABLE,
                "since": time.monotonic() - 10 * LDI_UNREACHABLE_GRACE_SEC,
            },
            privacy=False,
            offline_for=9999,
        )
        c.ldi_rest_state = {CAM: {"privacy_on": True}}
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        ir.async_create_issue.assert_not_called()
