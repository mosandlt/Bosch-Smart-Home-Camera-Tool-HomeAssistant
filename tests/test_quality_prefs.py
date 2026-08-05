"""Regression tests for quality_prefs.py — video-quality + Mini-NVR-mode
per-camera preference getters/setters, extracted out of coordinator.py
(structural cleanup toward Platinum quality_scale, coordinator.py was
~4,585 lines).

Tests call the module functions directly with a lightweight stub
(SimpleNamespace) standing in for the coordinator, mirroring the existing
`_make_coord_coordinator_pure_helpers()` pattern in tests/test_init.py —
these functions only ever touch a handful of per-cam dicts plus
`.options`/`.data`, never `self.hass` or coordinator-only machinery.
"""

from types import SimpleNamespace

from custom_components.bosch_shc_camera import quality_prefs

CAM_A = "cam-a"
CAM_B = "cam-b"


def _make_coord(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "_quality_preference": {},
        "_proxy_url_cache": {},
        "_quality_effective_inst": {},
        "_nvr_mode_preference": {},
        "_nvr_event_clip_enabled": {},
        "options": {},
        "data": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestGetQuality:
    def test_default_is_auto(self) -> None:
        assert quality_prefs.get_quality(_make_coord(), CAM_A) == "auto"

    def test_runtime_override_wins(self) -> None:
        coord = _make_coord(_quality_preference={CAM_A: "low"})
        assert quality_prefs.get_quality(coord, CAM_A) == "low"

    def test_override_scoped_to_its_own_camera(self) -> None:
        coord = _make_coord(_quality_preference={CAM_A: "high"})
        assert quality_prefs.get_quality(coord, CAM_B) == "auto"


class TestSetQuality:
    def test_stores_preference(self) -> None:
        coord = _make_coord()
        quality_prefs.set_quality(coord, CAM_A, "high")
        assert coord._quality_preference[CAM_A] == "high"

    def test_invalidates_proxy_url_cache(self) -> None:
        """Switching quality must drop the cached proxy URL — otherwise
        the next stream-on reuses the old highQualityVideo flag."""
        coord = _make_coord(_proxy_url_cache={CAM_A: ("url", 123.0)})
        quality_prefs.set_quality(coord, CAM_A, "low")
        assert CAM_A not in coord._proxy_url_cache

    def test_does_not_touch_other_cameras_proxy_cache(self) -> None:
        coord = _make_coord(_proxy_url_cache={CAM_B: ("url-b", 1.0)})
        quality_prefs.set_quality(coord, CAM_A, "low")
        assert coord._proxy_url_cache[CAM_B] == ("url-b", 1.0)

    def test_missing_proxy_cache_entry_is_a_noop(self) -> None:
        coord = _make_coord()
        quality_prefs.set_quality(coord, CAM_A, "auto")  # must not raise KeyError
        assert coord._quality_preference[CAM_A] == "auto"


class TestGetQualityParams:
    def test_high_returns_hq_true_inst_1(self) -> None:
        coord = _make_coord(_quality_preference={CAM_A: "high"})
        assert quality_prefs.get_quality_params(coord, CAM_A) == (True, 1)

    def test_low_returns_hq_false_inst_4(self) -> None:
        coord = _make_coord(_quality_preference={CAM_A: "low"})
        assert quality_prefs.get_quality_params(coord, CAM_A) == (False, 4)

    def test_auto_returns_hq_false_inst_2(self) -> None:
        coord = _make_coord()
        assert quality_prefs.get_quality_params(coord, CAM_A) == (False, 2)

    def test_garbage_preference_falls_back_to_auto_params(self) -> None:
        coord = _make_coord(_quality_preference={CAM_A: "bogus"})
        assert quality_prefs.get_quality_params(coord, CAM_A) == (False, 2)


class TestGetQualityRemoteFallbackActive:
    def test_false_when_preference_is_not_low(self) -> None:
        coord = _make_coord(
            _quality_preference={CAM_A: "auto"},
            _quality_effective_inst={CAM_A: 2},
        )
        assert quality_prefs.get_quality_remote_fallback_active(coord, CAM_A) is False

    def test_false_when_low_but_effective_inst_is_4_unclamped(self) -> None:
        coord = _make_coord(
            _quality_preference={CAM_A: "low"},
            _quality_effective_inst={CAM_A: 4},
        )
        assert quality_prefs.get_quality_remote_fallback_active(coord, CAM_A) is False

    def test_true_when_low_but_remote_clamped_to_inst_2(self) -> None:
        coord = _make_coord(
            _quality_preference={CAM_A: "low"},
            _quality_effective_inst={CAM_A: 2},
        )
        assert quality_prefs.get_quality_remote_fallback_active(coord, CAM_A) is True

    def test_false_when_no_effective_inst_recorded_yet(self) -> None:
        coord = _make_coord(_quality_preference={CAM_A: "low"})
        assert quality_prefs.get_quality_remote_fallback_active(coord, CAM_A) is False


class TestGetNvrMode:
    def test_no_override_global_false_falls_back_to_continuous(self) -> None:
        coord = _make_coord(options={"nvr_event_only": False})
        assert quality_prefs.get_nvr_mode(coord, CAM_A) == "continuous"

    def test_no_override_global_true_falls_back_to_event_buffered(self) -> None:
        coord = _make_coord(options={"nvr_event_only": True})
        assert quality_prefs.get_nvr_mode(coord, CAM_A) == "event_buffered"

    def test_no_override_missing_global_option_defaults_continuous(self) -> None:
        coord = _make_coord(options={})
        assert quality_prefs.get_nvr_mode(coord, CAM_A) == "continuous"

    def test_per_camera_override_wins_over_global_false(self) -> None:
        coord = _make_coord(
            options={"nvr_event_only": False},
            _nvr_mode_preference={CAM_A: "event_buffered"},
        )
        assert quality_prefs.get_nvr_mode(coord, CAM_A) == "event_buffered"

    def test_per_camera_override_wins_over_global_true(self) -> None:
        coord = _make_coord(
            options={"nvr_event_only": True},
            _nvr_mode_preference={CAM_A: "continuous"},
        )
        assert quality_prefs.get_nvr_mode(coord, CAM_A) == "continuous"

    def test_override_scoped_to_its_own_camera(self) -> None:
        coord = _make_coord(
            options={"nvr_event_only": False},
            _nvr_mode_preference={CAM_A: "event_buffered"},
        )
        assert quality_prefs.get_nvr_mode(coord, CAM_B) == "continuous"

    def test_invalid_cached_override_value_is_ignored(self) -> None:
        """Defensive: a garbage cached value (shouldn't happen via the
        select entity) must not be treated as a valid override."""
        coord = _make_coord(
            options={"nvr_event_only": False},
            _nvr_mode_preference={CAM_A: "bogus_mode"},
        )
        assert quality_prefs.get_nvr_mode(coord, CAM_A) == "continuous"


class TestSetNvrMode:
    def test_stores_override(self) -> None:
        coord = _make_coord(options={"nvr_event_only": False})
        quality_prefs.set_nvr_mode(coord, CAM_A, "event_buffered")
        assert coord._nvr_mode_preference[CAM_A] == "event_buffered"
        assert quality_prefs.get_nvr_mode(coord, CAM_A) == "event_buffered"

    def test_does_not_affect_other_cameras(self) -> None:
        coord = _make_coord(options={"nvr_event_only": False})
        quality_prefs.set_nvr_mode(coord, CAM_A, "event_buffered")
        assert quality_prefs.get_nvr_mode(coord, CAM_B) == "continuous"


class TestGetNvrEventClipEnabled:
    def test_defaults_true_when_unset(self) -> None:
        assert quality_prefs.get_nvr_event_clip_enabled(_make_coord(), CAM_A) is True

    def test_reflects_explicit_false(self) -> None:
        coord = _make_coord(_nvr_event_clip_enabled={CAM_A: False})
        assert quality_prefs.get_nvr_event_clip_enabled(coord, CAM_A) is False

    def test_reflects_explicit_true(self) -> None:
        coord = _make_coord(_nvr_event_clip_enabled={CAM_A: False, CAM_B: True})
        assert quality_prefs.get_nvr_event_clip_enabled(coord, CAM_B) is True


class TestSetNvrEventClipEnabled:
    def test_set_false_then_get_reflects_it(self) -> None:
        coord = _make_coord()
        quality_prefs.set_nvr_event_clip_enabled(coord, CAM_A, False)
        assert coord._nvr_event_clip_enabled[CAM_A] is False
        assert quality_prefs.get_nvr_event_clip_enabled(coord, CAM_A) is False

    def test_does_not_affect_other_cameras(self) -> None:
        coord = _make_coord()
        quality_prefs.set_nvr_event_clip_enabled(coord, CAM_A, False)
        assert quality_prefs.get_nvr_event_clip_enabled(coord, CAM_B) is True


class TestMotionSettings:
    def test_empty_when_no_data(self) -> None:
        assert quality_prefs.motion_settings(_make_coord(), CAM_A) == {}

    def test_empty_when_camera_present_but_no_motion_key(self) -> None:
        coord = _make_coord(data={CAM_A: {}})
        assert quality_prefs.motion_settings(coord, CAM_A) == {}

    def test_returns_cam_motion_dict(self) -> None:
        coord = _make_coord(
            data={
                CAM_A: {"motion": {"enabled": True, "motionAlarmConfiguration": "HIGH"}}
            }
        )
        assert quality_prefs.motion_settings(coord, CAM_A) == {
            "enabled": True,
            "motionAlarmConfiguration": "HIGH",
        }

    def test_unaffected_by_other_cameras_data(self) -> None:
        coord = _make_coord(data={CAM_B: {"motion": {"enabled": True}}})
        assert quality_prefs.motion_settings(coord, CAM_A) == {}


class TestRecordingOptions:
    def test_empty_when_no_data(self) -> None:
        assert quality_prefs.recording_options(_make_coord(), CAM_A) == {}

    def test_empty_when_camera_present_but_no_recording_options_key(self) -> None:
        coord = _make_coord(data={CAM_A: {}})
        assert quality_prefs.recording_options(coord, CAM_A) == {}

    def test_returns_cam_recording_options_dict(self) -> None:
        coord = _make_coord(data={CAM_A: {"recordingOptions": {"recordSound": True}}})
        assert quality_prefs.recording_options(coord, CAM_A) == {"recordSound": True}

    def test_unaffected_by_other_cameras_data(self) -> None:
        coord = _make_coord(data={CAM_B: {"recordingOptions": {"recordSound": True}}})
        assert quality_prefs.recording_options(coord, CAM_A) == {}
