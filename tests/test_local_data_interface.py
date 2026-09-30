"""Local data interface status: parsing, slow-tier polling, entity, Repairs hint.

Endpoint contract pinned here (fake values only):
  200 {"username": str} -> active | 404 -> inactive | 449 -> unsupported
  anything else (garbage body, other status, network error) -> keep last value.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera.binary_sensor import (
    BoschLocalDataInterfaceBinarySensor,
    async_setup_entry,
)
from custom_components.bosch_shc_camera.local_data_interface import (
    LDI_ENDPOINT,
    firmware_supports_ldi,
    parse_firmware,
    state_from_response,
)
from custom_components.bosch_shc_camera.repairs import (
    refresh_local_data_interface_issues,
)
from tests.test_slow_tier import (
    CAM_A,
    _make_resp,
    _make_slow_coord,
    _slow_ctx,
)

COMP = Path(__file__).parent.parent / "custom_components" / "bosch_shc_camera"
MODULE = "custom_components.bosch_shc_camera"


# ── pure helpers ────────────────────────────────────────────────────────────
class TestParsing:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("9.40.105", True),
            ("9.40.106", True),
            ("9.41.0", True),
            ("10.0.0", True),
            ("9.40.104", False),
            ("9.40.25", False),
            ("7.91.56", False),
            ("9.40", False),
            ("9.40.105.1", True),
            ("", False),
            (None, False),
            ("garbage", False),
            ("9.x.105", False),
            (9, False),
        ],
    )
    def test_gate(self, version: object, expected: bool) -> None:
        assert firmware_supports_ldi(version) is expected

    def test_oversized_digit_run_rejected(self) -> None:
        assert parse_firmware("9.40." + "1" * 5000) is None
        assert firmware_supports_ldi("9.40." + "1" * 5000) is False

    def test_parse_firmware_numeric_tuple(self) -> None:
        assert parse_firmware(" 9.40.105 ") == (9, 40, 105)
        assert parse_firmware("9..1") is None

    def test_active(self) -> None:
        assert state_from_response(200, {"username": "testuser"}) == {
            "state": "active",
            "username": "testuser",
        }

    def test_inactive(self) -> None:
        assert state_from_response(404, None) == {"state": "inactive"}

    def test_unsupported(self) -> None:
        assert state_from_response(449, None) == {"state": "unsupported"}

    @pytest.mark.parametrize(
        "body", [None, [], "x", {}, {"username": 5}, {"other": "x"}]
    )
    def test_garbage_200_keeps_last(self, body: object) -> None:
        assert state_from_response(200, body) is None

    @pytest.mark.parametrize("status", [0, 401, 403, 429, 500, 503])
    def test_other_status_keeps_last(self, status: int) -> None:
        assert state_from_response(status, None) is None


# ── slow-tier polling ───────────────────────────────────────────────────────
def _ldi_coord(**kw: object) -> SimpleNamespace:
    kw.setdefault("local_data_interface_cache", {})
    return _make_slow_coord(**kw)


def _session(status: int, body: object = None, *, raises: bool = False) -> MagicMock:
    calls: list[str] = []

    def _get(url: str, **_: object) -> MagicMock:
        calls.append(str(url))
        if str(url).endswith(f"/{LDI_ENDPOINT}"):
            if raises:
                raise OSError("boom")
            return _make_resp(status, body)
        return _make_resp(404)

    s = MagicMock()
    s.get = MagicMock(side_effect=_get)
    s.calls = calls
    return s


async def _poll(
    coord: SimpleNamespace, session: MagicMock, *, gen2: bool = True, raw=None
) -> None:
    from custom_components.bosch_shc_camera.slow_tier import _poll_slow_tier_endpoints

    data = {CAM_A: {}}
    await _poll_slow_tier_endpoints(
        coord,
        CAM_A,
        raw if raw is not None else {},
        _slow_ctx(is_gen2=gen2, hw="HOME_Eyes_Outdoor" if gen2 else "CAMERA_EYES"),
        data,
        session,
        {},
        MagicMock(),
    )


def _polled(session: MagicMock) -> bool:
    return any(c.endswith(f"/{LDI_ENDPOINT}") for c in session.calls)


class TestSlowTierPoll:
    @pytest.mark.asyncio
    async def test_active_200(self) -> None:
        coord = _ldi_coord(firmware_cache={CAM_A: {"current": "9.40.105"}})
        s = _session(200, {"username": "testuser"})
        await _poll(coord, s)
        assert coord.local_data_interface_cache[CAM_A] == {
            "state": "active",
            "username": "testuser",
        }

    @pytest.mark.asyncio
    async def test_inactive_404(self) -> None:
        coord = _ldi_coord(firmware_cache={CAM_A: {"current": "9.40.105"}})
        await _poll(coord, _session(404, {"error": "sh:entity.notfound"}))
        assert coord.local_data_interface_cache[CAM_A] == {"state": "inactive"}

    @pytest.mark.asyncio
    async def test_unsupported_449(self) -> None:
        coord = _ldi_coord(firmware_cache={CAM_A: {"current": "9.40.105"}})
        await _poll(coord, _session(449, {"error": "sh:firmware.not.supported"}))
        assert coord.local_data_interface_cache[CAM_A] == {"state": "unsupported"}

    @pytest.mark.asyncio
    async def test_garbage_body_keeps_last(self) -> None:
        prior = {"state": "active", "username": "testuser"}
        coord = _ldi_coord(
            firmware_cache={CAM_A: {"current": "9.40.105"}},
            local_data_interface_cache={CAM_A: prior},
        )
        await _poll(coord, _session(200, ["not", "a", "dict"]))
        assert coord.local_data_interface_cache[CAM_A] == prior

    @pytest.mark.asyncio
    async def test_network_error_keeps_last(self) -> None:
        prior = {"state": "active", "username": "testuser"}
        coord = _ldi_coord(
            firmware_cache={CAM_A: {"current": "9.40.105"}},
            local_data_interface_cache={CAM_A: prior},
        )
        await _poll(coord, _session(0, raises=True))
        assert coord.local_data_interface_cache[CAM_A] == prior

    @pytest.mark.asyncio
    async def test_fw_below_gate_not_polled(self) -> None:
        coord = _ldi_coord(firmware_cache={CAM_A: {"current": "9.40.104"}})
        s = _session(200, {"username": "testuser"})
        await _poll(coord, s)
        assert not _polled(s)
        assert CAM_A not in coord.local_data_interface_cache

    @pytest.mark.asyncio
    async def test_gate_lost_purges_stale_cache(self) -> None:
        coord = _ldi_coord(
            firmware_cache={CAM_A: {"current": "9.40.104"}},
            local_data_interface_cache={CAM_A: {"state": "active", "username": "u"}},
        )
        await _poll(coord, _session(200, {"username": "u"}))
        assert CAM_A not in coord.local_data_interface_cache

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fw", [None, "", "garbage"])
    async def test_fw_unknown_not_polled(self, fw: object) -> None:
        coord = _ldi_coord(firmware_cache={CAM_A: {"current": fw}})
        s = _session(200, {"username": "testuser"})
        await _poll(coord, s, raw={"firmwareVersion": fw})
        assert not _polled(s)

    @pytest.mark.asyncio
    async def test_gen1_never_polled(self) -> None:
        coord = _ldi_coord(firmware_cache={CAM_A: {"current": "9.40.105"}})
        s = _session(200, {"username": "testuser"})
        await _poll(coord, s, gen2=False)
        assert not _polled(s)

    @pytest.mark.asyncio
    async def test_fw_falls_back_to_camera_info(self) -> None:
        coord = _ldi_coord()
        s = _session(200, {"username": "testuser"})
        await _poll(coord, s, raw={"firmwareVersion": "9.40.105"})
        assert coord.local_data_interface_cache[CAM_A]["state"] == "active"


# ── entity ──────────────────────────────────────────────────────────────────
def _entity_coord(cache: dict) -> SimpleNamespace:
    return SimpleNamespace(
        data={CAM_A: {"info": {"title": "Terrasse", "featureSupport": {}}}},
        local_data_interface_cache=cache,
        last_update_success=True,
        async_add_listener=MagicMock(return_value=MagicMock()),
    )


def _entry(coord: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="01E",
        data={},
        options={},
        runtime_data=coord,
        async_on_unload=MagicMock(),
    )


async def _setup(coord: SimpleNamespace) -> tuple[list, MagicMock]:
    captured: list = []
    entry = _entry(coord)
    await async_setup_entry(
        hass=None,
        config_entry=entry,
        async_add_entities=lambda e, update_before_add=False: captured.extend(e),
    )
    return captured, coord.async_add_listener.call_args_list[0][0][0]


def _ldi(captured: list) -> list:
    return [e for e in captured if isinstance(e, BoschLocalDataInterfaceBinarySensor)]


class TestEntity:
    @pytest.mark.asyncio
    async def test_created_when_active(self) -> None:
        coord = _entity_coord({CAM_A: {"state": "active", "username": "testuser"}})
        captured, _ = await _setup(coord)
        (ent,) = _ldi(captured)
        ent.coordinator = coord
        assert ent.is_on is True
        assert ent.available is True
        assert ent.extra_state_attributes == {"username": "testuser"}
        assert ent.entity_description.entity_category.value == "diagnostic"
        assert ent.entity_description.device_class is None
        assert ent.translation_key == "local_data_interface"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "cache",
        [{}, {CAM_A: {"state": "inactive"}}, {CAM_A: {"state": "unsupported"}}],
    )
    async def test_not_created_unless_active(self, cache: dict) -> None:
        captured, _ = await _setup(_entity_coord(cache))
        assert _ldi(captured) == []

    @pytest.mark.asyncio
    async def test_created_later_when_becomes_active_once(self) -> None:
        coord = _entity_coord({CAM_A: {"state": "inactive"}})
        captured, listener = await _setup(coord)
        assert _ldi(captured) == []
        coord.local_data_interface_cache[CAM_A] = {
            "state": "active",
            "username": "testuser",
        }
        listener()
        assert len(_ldi(captured)) == 1
        listener()
        assert len(_ldi(captured)) == 1

    @pytest.mark.asyncio
    async def test_recreated_when_camera_returns_after_removal(self) -> None:
        coord = _entity_coord({CAM_A: {"state": "active", "username": "testuser"}})
        captured, listener = await _setup(coord)
        assert len(_ldi(captured)) == 1
        saved = coord.data
        coord.data = {}
        listener()
        coord.data = saved
        listener()
        assert len(_ldi(captured)) == 2

    @pytest.mark.asyncio
    async def test_unavailable_and_no_username_after_inactive(self) -> None:
        coord = _entity_coord({CAM_A: {"state": "active", "username": "testuser"}})
        captured, _ = await _setup(coord)
        (ent,) = _ldi(captured)
        ent.coordinator = coord
        coord.local_data_interface_cache[CAM_A] = {"state": "inactive"}
        assert ent.available is False
        assert ent.is_on is None
        assert ent.extra_state_attributes == {}

    @pytest.mark.asyncio
    async def test_unavailable_when_unsupported(self) -> None:
        coord = _entity_coord({CAM_A: {"state": "active", "username": "testuser"}})
        captured, _ = await _setup(coord)
        (ent,) = _ldi(captured)
        ent.coordinator = coord
        coord.local_data_interface_cache[CAM_A] = {"state": "unsupported"}
        assert ent.available is False

    def test_username_not_recorded(self) -> None:
        assert BoschLocalDataInterfaceBinarySensor._unrecorded_attributes == {
            "username"
        }

    @pytest.mark.asyncio
    async def test_empty_data_no_crash(self) -> None:
        coord = _entity_coord({})
        coord.data = {}
        captured, listener = await _setup(coord)
        coord.data = None
        listener()
        assert captured == []


# ── Repairs hint ────────────────────────────────────────────────────────────
def _repairs_coord(fw: object, state: str | None) -> SimpleNamespace:
    cache = {} if state is None else {CAM_A: {"state": state}}
    return SimpleNamespace(
        hass=SimpleNamespace(),
        data={CAM_A: {"info": {"title": "Terrasse"}}},
        firmware_cache={CAM_A: {"current": fw}},
        local_data_interface_cache=cache,
        _ldi_hint_alerted=set(),
    )


class TestRepairsHint:
    @patch(f"{MODULE}.ir")
    def test_created_on_404_when_gated(
        self, mock_ir: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        coord = _repairs_coord("9.40.105", "inactive")
        with caplog.at_level(logging.INFO, logger=f"{MODULE}.repairs"):
            refresh_local_data_interface_issues(coord)
            refresh_local_data_interface_issues(coord)
        kw = mock_ir.async_create_issue.call_args
        assert kw.args[2] == f"local_data_interface_available_{CAM_A}"
        assert kw.kwargs["is_fixable"] is False
        assert kw.kwargs["translation_key"] == "local_data_interface_available"
        assert kw.kwargs["translation_placeholders"] == {"camera": "Terrasse"}
        assert (
            sum(
                "Local data interface can be enabled" in r.message
                for r in caplog.records
            )
            == 1
        )

    @pytest.mark.parametrize(
        ("fw", "state"),
        [("9.40.105", "unsupported"), ("9.40.105", None), ("9.40.105", "active")],
    )
    @patch(f"{MODULE}.ir")
    def test_not_created(self, mock_ir: MagicMock, fw: str, state: str | None) -> None:
        refresh_local_data_interface_issues(_repairs_coord(fw, state))
        mock_ir.async_create_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_gen1_fw_never_created_and_cleared(self, mock_ir: MagicMock) -> None:
        coord = _repairs_coord("7.91.56", "inactive")
        refresh_local_data_interface_issues(coord)
        mock_ir.async_create_issue.assert_not_called()
        mock_ir.async_delete_issue.assert_called_once()

    @patch(f"{MODULE}.ir")
    def test_cleared_on_active(self, mock_ir: MagicMock) -> None:
        coord = _repairs_coord("9.40.105", "inactive")
        refresh_local_data_interface_issues(coord)
        coord.local_data_interface_cache[CAM_A] = {"state": "active"}
        refresh_local_data_interface_issues(coord)
        mock_ir.async_delete_issue.assert_called_once_with(
            coord.hass, "bosch_shc_camera", f"local_data_interface_available_{CAM_A}"
        )
        assert CAM_A not in coord._ldi_hint_alerted

    @patch(f"{MODULE}.ir")
    def test_cleared_when_gate_lost(self, mock_ir: MagicMock) -> None:
        coord = _repairs_coord("9.40.105", "inactive")
        refresh_local_data_interface_issues(coord)
        coord.firmware_cache[CAM_A]["current"] = "9.40.104"
        refresh_local_data_interface_issues(coord)
        mock_ir.async_delete_issue.assert_called_once()

    @patch(f"{MODULE}.ir")
    def test_unsupported_leaves_existing_issue(self, mock_ir: MagicMock) -> None:
        coord = _repairs_coord("9.40.105", "inactive")
        refresh_local_data_interface_issues(coord)
        coord.local_data_interface_cache[CAM_A] = {"state": "unsupported"}
        refresh_local_data_interface_issues(coord)
        mock_ir.async_delete_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_no_data_no_crash(self, mock_ir: MagicMock) -> None:
        coord = _repairs_coord("9.40.105", "inactive")
        coord.data = None
        refresh_local_data_interface_issues(coord)
        mock_ir.async_create_issue.assert_not_called()

    def test_coordinator_delegator(self) -> None:
        from custom_components.bosch_shc_camera.coordinator import (
            BoschCameraCoordinator,
        )

        with patch(f"{MODULE}.repairs.refresh_local_data_interface_issues") as m:
            BoschCameraCoordinator._refresh_local_data_interface_issues(MagicMock())
        m.assert_called_once()


# ── translations ────────────────────────────────────────────────────────────
class TestTranslations:
    @pytest.mark.parametrize(
        "path",
        [COMP / "strings.json", *sorted((COMP / "translations").glob("*.json"))],
        ids=lambda p: p.name,
    )
    def test_keys_present(self, path: Path) -> None:
        d = json.loads(path.read_text(encoding="utf-8"))
        assert d["entity"]["binary_sensor"]["local_data_interface"]["name"]
        issue = d["issues"]["local_data_interface_available"]
        assert issue["title"]
        assert "{camera}" in issue["description"]
