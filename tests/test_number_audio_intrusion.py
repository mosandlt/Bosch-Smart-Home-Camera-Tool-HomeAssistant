"""Tests for number.py — audio level entities and intrusion detection number entities.

Covers:
  - BoschSpeakerLevelNumber: reads from _audio_cache (not local default), availability,
    async_set_native_value full-body preservation, PUT failure path
  - BoschMicrophoneLevelNumber: cache read, availability, PUT body, privacy guard
  - BoschIntrusionSensitivityNumber: range 0-7, default, garbage clamp, write-lock, PUT body
  - BoschIntrusionDistanceNumber: range 1-10, default, garbage clamp, write-lock, PUT body

PIN_EVERY_MODE applied: one test per discrete boundary (min/max) + default + garbage input.
No HA runtime needed — SimpleNamespace + AsyncMock pattern.

Source: Thomas (project owner) — intrusion range confirmed via api-findings.md §5/§6.2
capture 2026-04-28 (distance=8 observed, sensitivity max=7 documented).
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

CAM_ID_GEN2_OUTDOOR = "11111111-1111-1111-1111-111111111111"
CAM_ID_GEN2_INDOOR  = "22222222-2222-2222-2222-222222222222"
CAM_ID_GEN1         = "44444444-0000-0000-0000-000000000001"

_INTRUSION_CFG = {
    "enabled": True,
    "detectionMode": "ALL_MOTIONS",
    "sensitivity": 3,
    "distance": 8,
}

_AUDIO_CFG = {
    "audioEnabled": True,
    "microphoneLevel": 60,
    "speakerLevel": 75,
}


# ── shared helpers ────────────────────────────────────────────────────────────


def _coord(
    cam_id: str = CAM_ID_GEN2_OUTDOOR,
    hw: str = "HOME_Eyes_Outdoor",
    audio_cache: dict | None = None,
    intrusion_cache: dict | None = None,
    intrusion_set_at: dict | None = None,
    shc_state_cache: dict | None = None,
    put_return: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        data={
            cam_id: {
                "info": {
                    "title": "Testkamera",
                    "hardwareVersion": hw,
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                },
            }
        },
        last_update_success=True,
        token="test-token",
        options={},
        _audio_cache=audio_cache if audio_cache is not None else {},
        _intrusion_config_cache=intrusion_cache if intrusion_cache is not None else {},
        _intrusion_config_set_at=intrusion_set_at if intrusion_set_at is not None else {},
        _shc_state_cache=shc_state_cache if shc_state_cache is not None else {cam_id: {}},
        async_put_camera=AsyncMock(return_value=put_return),
        is_camera_online=lambda cid: True,
    )


def _entry() -> SimpleNamespace:
    return SimpleNamespace(data={"bearer_token": "tok"}, options={}, runtime_data=None)


def _make_entity(cls, coord, cam_id=CAM_ID_GEN2_OUTDOOR):
    """Bypass __init__ safely for entities that call CoordinatorEntity.__init__."""
    ent = cls.__new__(cls)
    ent.coordinator = coord
    ent._cam_id    = cam_id
    ent._entry     = _entry()
    info = coord.data[cam_id]["info"]
    ent._cam_title  = info["title"]
    ent._model      = info["hardwareVersion"]
    ent._model_name = ent._model
    ent._fw         = info["firmwareVersion"]
    ent._mac        = info.get("macAddress", "")
    ent.async_write_ha_state = MagicMock()
    return ent


# ══════════════════════════════════════════════════════════════════════════════
# BoschSpeakerLevelNumber
# ══════════════════════════════════════════════════════════════════════════════


class TestBoschSpeakerLevelNumber:
    """Verify that speaker level reads from _audio_cache, not a static default."""

    def _make(self, audio_cache=None, cam_id=CAM_ID_GEN2_OUTDOOR, put_return=True):
        from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber
        coord = _coord(cam_id=cam_id, audio_cache=audio_cache, put_return=put_return)
        ent = _make_entity(BoschSpeakerLevelNumber, coord, cam_id)
        return ent, coord

    # --- native_value ---

    def test_native_value_reads_audio_cache(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: dict(_AUDIO_CFG)})
        assert ent.native_value == 75.0

    def test_native_value_none_when_cache_empty(self):
        ent, _ = self._make(audio_cache={})
        assert ent.native_value is None

    def test_native_value_none_when_speaker_level_missing(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: {"audioEnabled": True}})
        assert ent.native_value is None

    def test_native_value_min_boundary(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: {"speakerLevel": 0}})
        assert ent.native_value == 0.0

    def test_native_value_max_boundary(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: {"speakerLevel": 100}})
        assert ent.native_value == 100.0

    # --- available ---

    def test_available_true_when_cache_populated(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: dict(_AUDIO_CFG)})
        assert ent.available is True

    def test_available_false_when_cache_empty(self):
        ent, _ = self._make(audio_cache={})
        assert ent.available is False

    # --- async_set_native_value ---

    @pytest.mark.asyncio
    async def test_set_value_sends_full_body(self):
        """PUT must send complete audio body — not just speakerLevel field."""
        audio = dict(_AUDIO_CFG)
        ent, coord = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: audio})
        await ent.async_set_native_value(50.0)

        coord.async_put_camera.assert_called_once()
        _, endpoint, body = coord.async_put_camera.call_args[0]
        assert endpoint == "audio"
        assert body["speakerLevel"] == 50
        # audioEnabled and microphoneLevel must be preserved
        assert "audioEnabled" in body
        assert "microphoneLevel" in body

    @pytest.mark.asyncio
    async def test_set_value_updates_cache_on_success(self):
        audio = dict(_AUDIO_CFG)
        ent, coord = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: audio})
        await ent.async_set_native_value(30.0)
        assert coord._audio_cache[CAM_ID_GEN2_OUTDOOR]["speakerLevel"] == 30

    @pytest.mark.asyncio
    async def test_set_value_no_cache_update_on_failure(self):
        audio = {"speakerLevel": 75, "audioEnabled": True, "microphoneLevel": 60}
        ent, coord = self._make(
            audio_cache={CAM_ID_GEN2_OUTDOOR: dict(audio)},
            put_return=False,
        )
        await ent.async_set_native_value(20.0)
        # Cache must stay at original value
        assert coord._audio_cache[CAM_ID_GEN2_OUTDOOR]["speakerLevel"] == 75

    @pytest.mark.asyncio
    async def test_set_value_rounds_float(self):
        audio = dict(_AUDIO_CFG)
        ent, coord = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: audio})
        await ent.async_set_native_value(67.6)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["speakerLevel"] == 68

    @pytest.mark.asyncio
    async def test_set_min_value(self):
        audio = dict(_AUDIO_CFG)
        ent, coord = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: audio})
        await ent.async_set_native_value(0.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["speakerLevel"] == 0

    @pytest.mark.asyncio
    async def test_set_max_value(self):
        audio = dict(_AUDIO_CFG)
        ent, coord = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: audio})
        await ent.async_set_native_value(100.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["speakerLevel"] == 100

    # --- metadata ---

    def test_translation_key(self):
        from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber
        coord = _coord(audio_cache={CAM_ID_GEN2_OUTDOOR: dict(_AUDIO_CFG)})
        ent = BoschSpeakerLevelNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent._attr_translation_key == "speaker_level"

    def test_unique_id(self):
        from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber
        coord = _coord(audio_cache={})
        ent = BoschSpeakerLevelNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent._attr_unique_id == f"bosch_shc_camera_{CAM_ID_GEN2_OUTDOOR}_speaker_level"

    def test_disabled_by_default(self):
        from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber
        coord = _coord(audio_cache={})
        ent = BoschSpeakerLevelNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent.entity_registry_enabled_default is False


# ══════════════════════════════════════════════════════════════════════════════
# BoschMicrophoneLevelNumber
# ══════════════════════════════════════════════════════════════════════════════


class TestBoschMicrophoneLevelNumber:
    """Smoke + regression tests for mic level — privacy guard and body shape."""

    def _make(self, hw="HOME_Eyes_Outdoor", audio_cache=None, cam_id=CAM_ID_GEN2_OUTDOOR, put_return=True):
        from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber
        coord = _coord(cam_id=cam_id, hw=hw, audio_cache=audio_cache, put_return=put_return)
        ent = _make_entity(BoschMicrophoneLevelNumber, coord, cam_id)
        return ent, coord

    def test_native_value_reads_microphone_level(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: dict(_AUDIO_CFG)})
        assert ent.native_value == 60.0

    def test_native_value_none_when_cache_empty(self):
        ent, _ = self._make(audio_cache={})
        assert ent.native_value is None

    def test_native_value_min(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: {"microphoneLevel": 0}})
        assert ent.native_value == 0.0

    def test_native_value_max(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: {"microphoneLevel": 100}})
        assert ent.native_value == 100.0

    def test_available_false_when_no_cache(self):
        ent, _ = self._make(audio_cache={})
        assert ent.available is False

    @pytest.mark.asyncio
    async def test_set_value_sends_full_audio_body(self):
        audio = dict(_AUDIO_CFG)
        ent, coord = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: audio})
        await ent.async_set_native_value(80.0)
        coord.async_put_camera.assert_called_once()
        _, endpoint, body = coord.async_put_camera.call_args[0]
        assert endpoint == "audio"
        assert body["microphoneLevel"] == 80
        assert "speakerLevel" in body

    @pytest.mark.asyncio
    async def test_privacy_guard_indoor_blocks_write(self):
        """Indoor II with privacy ON must not call async_put_camera."""
        from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber
        coord = _coord(
            cam_id=CAM_ID_GEN2_INDOOR,
            hw="HOME_Eyes_Indoor",
            audio_cache={CAM_ID_GEN2_INDOOR: dict(_AUDIO_CFG)},
            shc_state_cache={CAM_ID_GEN2_INDOOR: {"privacy_mode": True}},
        )
        ent = _make_entity(BoschMicrophoneLevelNumber, coord, CAM_ID_GEN2_INDOOR)
        # Patch switch helpers to simulate privacy ON
        import unittest.mock as mock
        with mock.patch(
            "custom_components.bosch_shc_camera.switch._is_gen2_indoor", return_value=True
        ), mock.patch(
            "custom_components.bosch_shc_camera.switch._warn_if_privacy_on",
            new_callable=AsyncMock, return_value=True,
        ):
            await ent.async_set_native_value(40.0)
        coord.async_put_camera.assert_not_called()

    def test_translation_key(self):
        from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber
        coord = _coord(audio_cache={})
        ent = BoschMicrophoneLevelNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent._attr_translation_key == "microphone_level"


# ══════════════════════════════════════════════════════════════════════════════
# BoschIntrusionSensitivityNumber
# ══════════════════════════════════════════════════════════════════════════════


class TestBoschIntrusionSensitivityNumber:
    """PIN_EVERY_MODE: min(0) / max(7) / default(3) / garbage-clamp / PUT body / write-lock."""

    def _make(self, cam_id=CAM_ID_GEN2_OUTDOOR, intrusion_cache=None, put_return=True):
        from custom_components.bosch_shc_camera.number import BoschIntrusionSensitivityNumber
        ic = {cam_id: dict(_INTRUSION_CFG)} if intrusion_cache is None else intrusion_cache
        coord = _coord(cam_id=cam_id, intrusion_cache=ic, put_return=put_return)
        ent = _make_entity(BoschIntrusionSensitivityNumber, coord, cam_id)
        return ent, coord

    # --- native_value ---

    def test_native_value_default(self):
        ent, _ = self._make()
        assert ent.native_value == 3.0

    def test_native_value_min(self):
        ent, _ = self._make(intrusion_cache={CAM_ID_GEN2_OUTDOOR: {"sensitivity": 0, "distance": 5}})
        assert ent.native_value == 0.0

    def test_native_value_max(self):
        ent, _ = self._make(intrusion_cache={CAM_ID_GEN2_OUTDOOR: {"sensitivity": 7, "distance": 5}})
        assert ent.native_value == 7.0

    def test_native_value_none_when_cache_empty(self):
        ent, _ = self._make(intrusion_cache={})
        assert ent.native_value is None

    # --- available ---

    def test_available_true_when_cache_populated(self):
        ent, _ = self._make()
        assert ent.available is True

    def test_available_false_when_cache_empty(self):
        ent, _ = self._make(intrusion_cache={})
        assert ent.available is False

    # --- async_set_native_value: PIN_EVERY_MODE ---

    @pytest.mark.asyncio
    async def test_set_min_sensitivity(self):
        ent, coord = self._make()
        await ent.async_set_native_value(0.0)
        _, endpoint, body = coord.async_put_camera.call_args[0]
        assert endpoint == "intrusionDetectionConfig"
        assert body["sensitivity"] == 0

    @pytest.mark.asyncio
    async def test_set_max_sensitivity(self):
        ent, coord = self._make()
        await ent.async_set_native_value(7.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["sensitivity"] == 7

    @pytest.mark.asyncio
    async def test_set_default_sensitivity(self):
        ent, coord = self._make()
        await ent.async_set_native_value(5.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["sensitivity"] == 5

    @pytest.mark.asyncio
    async def test_set_garbage_clamps_to_max(self):
        """Values above 7 must be clamped to 7."""
        ent, coord = self._make()
        await ent.async_set_native_value(99.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["sensitivity"] == 7

    @pytest.mark.asyncio
    async def test_set_garbage_clamps_to_min(self):
        """Negative values must be clamped to 0."""
        ent, coord = self._make()
        await ent.async_set_native_value(-5.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["sensitivity"] == 0

    @pytest.mark.asyncio
    async def test_put_body_preserves_other_fields(self):
        """Full intrusion body must be sent — detectionMode + distance must not be dropped."""
        ent, coord = self._make()
        await ent.async_set_native_value(4.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert "detectionMode" in body
        assert "distance" in body
        assert "enabled" in body

    @pytest.mark.asyncio
    async def test_cache_updated_on_success(self):
        ent, coord = self._make()
        await ent.async_set_native_value(6.0)
        assert coord._intrusion_config_cache[CAM_ID_GEN2_OUTDOOR]["sensitivity"] == 6

    @pytest.mark.asyncio
    async def test_write_lock_set_on_success(self):
        """_intrusion_config_set_at must be set after successful PUT."""
        ent, coord = self._make()
        before = time.monotonic()
        await ent.async_set_native_value(4.0)
        after = time.monotonic()
        assert CAM_ID_GEN2_OUTDOOR in coord._intrusion_config_set_at
        ts = coord._intrusion_config_set_at[CAM_ID_GEN2_OUTDOOR]
        assert before <= ts <= after

    @pytest.mark.asyncio
    async def test_no_cache_update_on_failure(self):
        ent, coord = self._make(put_return=False)
        original_sensitivity = coord._intrusion_config_cache[CAM_ID_GEN2_OUTDOOR]["sensitivity"]
        await ent.async_set_native_value(7.0)
        assert coord._intrusion_config_cache[CAM_ID_GEN2_OUTDOOR]["sensitivity"] == original_sensitivity

    @pytest.mark.asyncio
    async def test_no_write_lock_on_failure(self):
        ent, coord = self._make(put_return=False)
        await ent.async_set_native_value(7.0)
        assert CAM_ID_GEN2_OUTDOOR not in coord._intrusion_config_set_at

    @pytest.mark.asyncio
    async def test_empty_cache_is_noop(self):
        ent, coord = self._make(intrusion_cache={})
        await ent.async_set_native_value(5.0)
        coord.async_put_camera.assert_not_called()

    # --- Gen1 skip (no intrusion cache → noop) ---

    @pytest.mark.asyncio
    async def test_gen1_not_wired_no_cache(self):
        """Gen1 has no intrusionDetectionConfig endpoint — cache stays empty, entity is unavailable."""
        from custom_components.bosch_shc_camera.number import BoschIntrusionSensitivityNumber
        coord = _coord(cam_id=CAM_ID_GEN1, hw="CAMERA_360", intrusion_cache={})
        ent = _make_entity(BoschIntrusionSensitivityNumber, coord, CAM_ID_GEN1)
        assert ent.available is False
        assert ent.native_value is None

    # --- metadata ---

    def test_translation_key(self):
        from custom_components.bosch_shc_camera.number import BoschIntrusionSensitivityNumber
        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionSensitivityNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent._attr_translation_key == "intrusion_sensitivity"

    def test_unique_id(self):
        from custom_components.bosch_shc_camera.number import BoschIntrusionSensitivityNumber
        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionSensitivityNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent._attr_unique_id == f"bosch_shc_camera_{CAM_ID_GEN2_OUTDOOR}_intrusion_sensitivity"

    def test_range_is_0_to_7(self):
        from custom_components.bosch_shc_camera.number import BoschIntrusionSensitivityNumber
        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionSensitivityNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent.native_min_value == 0
        assert ent.native_max_value == 7

    def test_enabled_by_default(self):
        from custom_components.bosch_shc_camera.number import BoschIntrusionSensitivityNumber
        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionSensitivityNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent.entity_registry_enabled_default is True

    # --- Gen2 Indoor II also gets the entity ---

    def test_available_gen2_indoor(self):
        from custom_components.bosch_shc_camera.number import BoschIntrusionSensitivityNumber
        coord = _coord(
            cam_id=CAM_ID_GEN2_INDOOR,
            hw="HOME_Eyes_Indoor",
            intrusion_cache={CAM_ID_GEN2_INDOOR: dict(_INTRUSION_CFG)},
        )
        ent = _make_entity(BoschIntrusionSensitivityNumber, coord, CAM_ID_GEN2_INDOOR)
        assert ent.available is True
        assert ent.native_value == 3.0


# ══════════════════════════════════════════════════════════════════════════════
# BoschIntrusionDistanceNumber
# ══════════════════════════════════════════════════════════════════════════════


class TestBoschIntrusionDistanceNumber:
    """PIN_EVERY_MODE: min(1) / max(10) / default(8 per capture) / garbage-clamp / write-lock."""

    def _make(self, cam_id=CAM_ID_GEN2_OUTDOOR, intrusion_cache=None, put_return=True):
        from custom_components.bosch_shc_camera.number import BoschIntrusionDistanceNumber
        ic = {cam_id: dict(_INTRUSION_CFG)} if intrusion_cache is None else intrusion_cache
        coord = _coord(cam_id=cam_id, intrusion_cache=ic, put_return=put_return)
        ent = _make_entity(BoschIntrusionDistanceNumber, coord, cam_id)
        return ent, coord

    # --- native_value ---

    def test_native_value_default(self):
        ent, _ = self._make()
        assert ent.native_value == 8.0  # from _INTRUSION_CFG distance=8 (capture 2026-04-28)

    def test_native_value_min(self):
        ent, _ = self._make(intrusion_cache={CAM_ID_GEN2_OUTDOOR: {"distance": 1, "sensitivity": 3}})
        assert ent.native_value == 1.0

    def test_native_value_max(self):
        ent, _ = self._make(intrusion_cache={CAM_ID_GEN2_OUTDOOR: {"distance": 10, "sensitivity": 3}})
        assert ent.native_value == 10.0

    def test_native_value_none_when_cache_empty(self):
        ent, _ = self._make(intrusion_cache={})
        assert ent.native_value is None

    # --- available ---

    def test_available_true_when_cache_populated(self):
        ent, _ = self._make()
        assert ent.available is True

    def test_available_false_when_cache_empty(self):
        ent, _ = self._make(intrusion_cache={})
        assert ent.available is False

    # --- async_set_native_value: PIN_EVERY_MODE ---

    @pytest.mark.asyncio
    async def test_set_min_distance(self):
        ent, coord = self._make()
        await ent.async_set_native_value(1.0)
        _, endpoint, body = coord.async_put_camera.call_args[0]
        assert endpoint == "intrusionDetectionConfig"
        assert body["distance"] == 1

    @pytest.mark.asyncio
    async def test_set_max_distance(self):
        ent, coord = self._make()
        await ent.async_set_native_value(8.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["distance"] == 8  # max 8 — API rejects > 8 (HTTP 400, FW 9.40.102)

    @pytest.mark.asyncio
    async def test_set_default_distance(self):
        ent, coord = self._make()
        await ent.async_set_native_value(4.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["distance"] == 4

    @pytest.mark.asyncio
    async def test_set_garbage_clamps_above_max(self):
        ent, coord = self._make()
        await ent.async_set_native_value(999.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["distance"] == 8  # clamp to 8 — API rejects > 8 (HTTP 400)

    @pytest.mark.asyncio
    async def test_set_garbage_clamps_below_min(self):
        ent, coord = self._make()
        await ent.async_set_native_value(-3.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["distance"] == 1

    @pytest.mark.asyncio
    async def test_put_body_preserves_other_fields(self):
        """detectionMode and sensitivity must survive the PUT body."""
        ent, coord = self._make()
        await ent.async_set_native_value(5.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert "sensitivity" in body
        assert "detectionMode" in body
        assert "enabled" in body

    @pytest.mark.asyncio
    async def test_cache_updated_on_success(self):
        ent, coord = self._make()
        await ent.async_set_native_value(7.0)
        assert coord._intrusion_config_cache[CAM_ID_GEN2_OUTDOOR]["distance"] == 7

    @pytest.mark.asyncio
    async def test_write_lock_set_on_success(self):
        ent, coord = self._make()
        before = time.monotonic()
        await ent.async_set_native_value(5.0)
        after = time.monotonic()
        assert CAM_ID_GEN2_OUTDOOR in coord._intrusion_config_set_at
        ts = coord._intrusion_config_set_at[CAM_ID_GEN2_OUTDOOR]
        assert before <= ts <= after

    @pytest.mark.asyncio
    async def test_no_cache_update_on_failure(self):
        ent, coord = self._make(put_return=False)
        original_distance = coord._intrusion_config_cache[CAM_ID_GEN2_OUTDOOR]["distance"]
        await ent.async_set_native_value(2.0)
        assert coord._intrusion_config_cache[CAM_ID_GEN2_OUTDOOR]["distance"] == original_distance

    @pytest.mark.asyncio
    async def test_no_write_lock_on_failure(self):
        ent, coord = self._make(put_return=False)
        await ent.async_set_native_value(2.0)
        assert CAM_ID_GEN2_OUTDOOR not in coord._intrusion_config_set_at

    @pytest.mark.asyncio
    async def test_empty_cache_is_noop(self):
        ent, coord = self._make(intrusion_cache={})
        await ent.async_set_native_value(5.0)
        coord.async_put_camera.assert_not_called()

    # --- metadata ---

    def test_translation_key(self):
        from custom_components.bosch_shc_camera.number import BoschIntrusionDistanceNumber
        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionDistanceNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent._attr_translation_key == "intrusion_distance"

    def test_unique_id(self):
        from custom_components.bosch_shc_camera.number import BoschIntrusionDistanceNumber
        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionDistanceNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent._attr_unique_id == f"bosch_shc_camera_{CAM_ID_GEN2_OUTDOOR}_intrusion_distance"

    def test_range_is_1_to_8(self):
        from custom_components.bosch_shc_camera.number import BoschIntrusionDistanceNumber
        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionDistanceNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent.native_min_value == 1
        assert ent.native_max_value == 8  # API rejects > 8 (HTTP 400, FW 9.40.102)

    def test_unit_is_meters(self):
        from custom_components.bosch_shc_camera.number import BoschIntrusionDistanceNumber
        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionDistanceNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        # Use private attr — public unit_of_measurement property requires entity platform context
        assert ent._attr_native_unit_of_measurement == "m"

    def test_enabled_by_default(self):
        from custom_components.bosch_shc_camera.number import BoschIntrusionDistanceNumber
        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionDistanceNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent.entity_registry_enabled_default is True

    # --- Gen2 Indoor II also gets the entity ---

    def test_available_gen2_indoor(self):
        from custom_components.bosch_shc_camera.number import BoschIntrusionDistanceNumber
        coord = _coord(
            cam_id=CAM_ID_GEN2_INDOOR,
            hw="HOME_Eyes_Indoor",
            intrusion_cache={CAM_ID_GEN2_INDOOR: dict(_INTRUSION_CFG)},
        )
        ent = _make_entity(BoschIntrusionDistanceNumber, coord, CAM_ID_GEN2_INDOOR)
        assert ent.available is True
        assert ent.native_value == 8.0

    # --- Gen1 not wired ---

    def test_gen1_no_cache_unavailable(self):
        from custom_components.bosch_shc_camera.number import BoschIntrusionDistanceNumber
        coord = _coord(cam_id=CAM_ID_GEN1, hw="CAMERA_EYES", intrusion_cache={})
        ent = _make_entity(BoschIntrusionDistanceNumber, coord, CAM_ID_GEN1)
        assert ent.available is False
        assert ent.native_value is None


# ══════════════════════════════════════════════════════════════════════════════
# async_setup_entry wiring smoke test
# ══════════════════════════════════════════════════════════════════════════════


class TestAsyncSetupEntryWiring:
    """Verify that setup_entry adds the correct entities for Gen1 and Gen2 cameras."""

    @pytest.mark.asyncio
    async def test_gen2_outdoor_gets_intrusion_entities(self):
        """Gen2 Outdoor II must produce BoschIntrusionSensitivityNumber + BoschIntrusionDistanceNumber."""
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionSensitivityNumber,
            BoschIntrusionDistanceNumber,
            async_setup_entry,
        )

        coord = _coord(
            cam_id=CAM_ID_GEN2_OUTDOOR,
            hw="HOME_Eyes_Outdoor",
            audio_cache={CAM_ID_GEN2_OUTDOOR: dict(_AUDIO_CFG)},
            intrusion_cache={CAM_ID_GEN2_OUTDOOR: dict(_INTRUSION_CFG)},
        )
        entry = SimpleNamespace(runtime_data=coord, data={}, options={}, entry_id="01ENTRY")
        added: list = []
        await async_setup_entry(None, entry, lambda ents, **_: added.extend(ents))  # type: ignore[arg-type]

        types = [type(e) for e in added]
        assert BoschIntrusionSensitivityNumber in types
        assert BoschIntrusionDistanceNumber in types

    @pytest.mark.asyncio
    async def test_gen2_indoor_gets_intrusion_entities(self):
        """Gen2 Indoor II must also produce the intrusion number entities."""
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionSensitivityNumber,
            BoschIntrusionDistanceNumber,
            async_setup_entry,
        )

        coord = _coord(
            cam_id=CAM_ID_GEN2_INDOOR,
            hw="HOME_Eyes_Indoor",
            audio_cache={CAM_ID_GEN2_INDOOR: dict(_AUDIO_CFG)},
            intrusion_cache={CAM_ID_GEN2_INDOOR: dict(_INTRUSION_CFG)},
        )
        entry = SimpleNamespace(runtime_data=coord, data={}, options={}, entry_id="01ENTRY")
        added: list = []
        await async_setup_entry(None, entry, lambda ents, **_: added.extend(ents))  # type: ignore[arg-type]

        types = [type(e) for e in added]
        assert BoschIntrusionSensitivityNumber in types
        assert BoschIntrusionDistanceNumber in types

    @pytest.mark.asyncio
    async def test_gen1_does_not_get_intrusion_entities(self):
        """Gen1 must NOT get intrusion number entities (no endpoint on Gen1)."""
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionSensitivityNumber,
            BoschIntrusionDistanceNumber,
            async_setup_entry,
        )

        coord = _coord(
            cam_id=CAM_ID_GEN1,
            hw="CAMERA_360",
            audio_cache={},
            intrusion_cache={},
        )
        entry = SimpleNamespace(runtime_data=coord, data={}, options={}, entry_id="01ENTRY")
        added: list = []
        await async_setup_entry(None, entry, lambda ents, **_: added.extend(ents))  # type: ignore[arg-type]

        types = [type(e) for e in added]
        assert BoschIntrusionSensitivityNumber not in types
        assert BoschIntrusionDistanceNumber not in types
