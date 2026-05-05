"""Tests for camera model registry + lookup helpers.

models.py is a pure-Python dataclass + dict module — no I/O, no async,
no HA dependency. Highest test ROI in the codebase.
"""

from __future__ import annotations

import pytest

from custom_components.bosch_shc_camera.models import (
    CameraModelConfig,
    MODELS,
    DEFAULT_MODEL,
    get_model_config,
    get_display_name,
)


KNOWN_HW_VERSIONS = [
    "INDOOR", "OUTDOOR",
    "CAMERA_360", "CAMERA_EYES",
    "HOME_Eyes_Outdoor", "HOME_Eyes_Indoor",
    "CAMERA_OUTDOOR_GEN2", "CAMERA_INDOOR_GEN2",
]


def test_default_model_is_camera_model_config() -> None:
    """DEFAULT_MODEL must be a real CameraModelConfig instance."""
    assert isinstance(DEFAULT_MODEL, CameraModelConfig)
    assert DEFAULT_MODEL.display_name


@pytest.mark.parametrize("hw", KNOWN_HW_VERSIONS)
def test_get_model_config_known_hw(hw: str) -> None:
    """Every documented hardwareVersion must resolve to a real config."""
    cfg = get_model_config(hw)
    assert isinstance(cfg, CameraModelConfig)
    # Sanity bounds — these timings must stay reasonable; absurd values
    # would block stream startup or hammer the camera.
    assert 0 < cfg.pre_warm_delay <= 10
    assert 0 < cfg.pre_warm_retries <= 20
    assert 0 < cfg.min_total_wait <= 120
    assert 0 < cfg.heartbeat_interval <= 3600
    assert cfg.max_session_duration > 0
    assert cfg.generation in (1, 2)


def test_get_model_config_unknown_hw_returns_default() -> None:
    """Unknown hardwareVersion strings fall back to DEFAULT_MODEL."""
    for unknown in ("MADE_UP_HW", "", "future_camera_v3", "AAAA"):
        assert get_model_config(unknown) is DEFAULT_MODEL


def test_gen2_outdoor_heartbeat_is_long() -> None:
    """Gen2 Outdoor FW 9.40.25 needs heartbeat_interval=3600 (no PUT-heartbeats).

    Lowering this would re-trigger the rotating-Digest-cred bug from v9.x.
    Regression guard: if anyone bumps this to a small value, the test fails
    and forces them to read the comment in models.py + confirm a fix.
    """
    cfg = get_model_config("HOME_Eyes_Outdoor")
    assert cfg.heartbeat_interval >= 3600, (
        f"Gen2 Outdoor heartbeat_interval={cfg.heartbeat_interval} — "
        f"PUT /connection rotates Digest creds, killing the active stream. "
        f"Must stay >= 3600."
    )


def test_gen1_indoor_360_pre_warm_is_short() -> None:
    """Gen1 Indoor 360 has fast SoC — pre-warm should be quick to reduce latency."""
    cfg = get_model_config("INDOOR")
    assert cfg.pre_warm_delay <= 2
    assert cfg.min_total_wait <= 30


def test_legacy_hw_aliases_resolve_to_same_config() -> None:
    """Legacy + canonical hardwareVersion strings must point to the same config."""
    assert get_model_config("INDOOR") is get_model_config("CAMERA_360")
    assert get_model_config("OUTDOOR") is get_model_config("CAMERA_EYES")
    assert get_model_config("HOME_Eyes_Outdoor") is get_model_config("CAMERA_OUTDOOR_GEN2")
    assert get_model_config("HOME_Eyes_Indoor") is get_model_config("CAMERA_INDOOR_GEN2")


def test_camera_model_config_is_frozen() -> None:
    """CameraModelConfig is @dataclass(frozen=True) — mutation must raise."""
    cfg = get_model_config("INDOOR")
    with pytest.raises((AttributeError, Exception)):
        cfg.heartbeat_interval = 1  # type: ignore[misc]


@pytest.mark.parametrize("hw,expected_substr", [
    ("INDOOR", "Innenkamera"),
    ("OUTDOOR", "Außenkamera"),
    ("HOME_Eyes_Outdoor", "Außenkamera"),
    ("HOME_Eyes_Indoor", "Innenkamera"),
])
def test_get_display_name_known(hw: str, expected_substr: str) -> None:
    """Known hardwareVersion gives the official Bosch name."""
    name = get_display_name(hw)
    assert expected_substr in name


@pytest.mark.parametrize("hw,expected_kind", [
    ("future_indoor_v9", "Innenkamera"),
    ("MY_CUSTOM_outdoor_HW", "Außenkamera"),
    ("HOME_Eyes_indoor_v3", "Innenkamera"),
    ("CAMERA_360_v2", "Innenkamera"),
])
def test_get_display_name_unknown_indoor_outdoor_inference(
    hw: str, expected_kind: str
) -> None:
    """Unknown hardwareVersion → infer indoor/outdoor from substring."""
    name = get_display_name(hw)
    assert expected_kind in name
    assert hw in name  # raw value preserved in parens


def test_get_display_name_truly_unknown_returns_raw() -> None:
    """Hardware that contains neither indoor nor outdoor markers returns the raw string."""
    assert get_display_name("WEIRD_HW_X1") == "WEIRD_HW_X1"


def test_models_registry_has_no_aliases_to_default() -> None:
    """Every registered key must resolve to a non-default config.

    If MODELS["FOO"] = DEFAULT_MODEL, the registration is pointless —
    DEFAULT_MODEL is the fallback anyway. Catches accidental dead entries.
    """
    for hw, cfg in MODELS.items():
        assert cfg is not DEFAULT_MODEL, (
            f"MODELS[{hw!r}] points to DEFAULT_MODEL — remove the entry "
            f"or replace with a model-specific config."
        )
