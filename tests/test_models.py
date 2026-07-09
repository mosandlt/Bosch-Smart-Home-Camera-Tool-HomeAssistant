"""Tests for camera model registry + lookup helpers.

models.py is a pure-Python dataclass + dict module — no I/O, no async,
no HA dependency. Highest test ROI in the codebase.
"""

from __future__ import annotations

import pytest

from custom_components.bosch_shc_camera.models import (
    DEFAULT_MODEL,
    MODELS,
    CameraModelConfig,
    get_display_name,
    get_model_config,
)

KNOWN_HW_VERSIONS = [
    "INDOOR",
    "OUTDOOR",
    "CAMERA_360",
    "CAMERA_EYES",
    "HOME_Eyes_Outdoor",
    "HOME_Eyes_Indoor",
    "CAMERA_OUTDOOR_GEN2",
    "CAMERA_INDOOR_GEN2",
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
    assert get_model_config("HOME_Eyes_Outdoor") is get_model_config(
        "CAMERA_OUTDOOR_GEN2"
    )
    assert get_model_config("HOME_Eyes_Indoor") is get_model_config(
        "CAMERA_INDOOR_GEN2"
    )


def test_camera_model_config_is_frozen() -> None:
    """CameraModelConfig is @dataclass(frozen=True) — mutation must raise."""
    cfg = get_model_config("INDOOR")
    with pytest.raises((AttributeError, Exception)):
        cfg.heartbeat_interval = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "hw,expected_substr",
    [
        ("INDOOR", "Innenkamera"),
        ("OUTDOOR", "Außenkamera"),
        ("HOME_Eyes_Outdoor", "Außenkamera"),
        ("HOME_Eyes_Indoor", "Innenkamera"),
    ],
)
def test_get_display_name_known(hw: str, expected_substr: str) -> None:
    """Known hardwareVersion gives the official Bosch name."""
    name = get_display_name(hw)
    assert expected_substr in name


@pytest.mark.parametrize(
    "hw,expected_kind",
    [
        ("future_indoor_v9", "Innenkamera"),
        ("MY_CUSTOM_outdoor_HW", "Außenkamera"),
        ("HOME_Eyes_indoor_v3", "Innenkamera"),
        ("CAMERA_360_v2", "Innenkamera"),
    ],
)
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


class TestStreamFallbackTiming:
    """Per-model thresholds governing when AUTO mode falls back from LOCAL
    to REMOTE. These are empirically tuned — lowering them reintroduces the
    false-fallback churn that was reported in pre-v10.5 versions.
    """

    def test_indoor_max_stream_errors_low_enough_to_fallback_quickly(self) -> None:
        """Indoor cameras on stable WLAN — should fallback at modest
        consecutive-error count."""
        cfg = get_model_config("INDOOR")
        assert 3 <= cfg.max_stream_errors <= 8, (
            f"Indoor max_stream_errors={cfg.max_stream_errors} — too low "
            "would cause spurious cloud fallbacks; too high delays recovery"
        )

    def test_outdoor_max_stream_errors_higher_for_wifi_jitter(self) -> None:
        """Outdoor cameras see real WLAN jitter — must tolerate more
        consecutive errors before falling back."""
        cfg = get_model_config("OUTDOOR")
        assert cfg.max_stream_errors >= 3, (
            f"Outdoor max_stream_errors={cfg.max_stream_errors} — needs "
            "higher tolerance than indoor due to outdoor WLAN flakiness"
        )

    def test_min_wifi_for_local_above_zero(self) -> None:
        """`min_wifi_for_local` gates LOCAL stream attempts; below this
        signal % we go straight to REMOTE."""
        for hw in ("INDOOR", "OUTDOOR", "HOME_Eyes_Outdoor", "HOME_Eyes_Indoor"):
            cfg = get_model_config(hw)
            assert 20 <= cfg.min_wifi_for_local <= 60, (
                f"{hw} min_wifi_for_local={cfg.min_wifi_for_local}% — must "
                "leave headroom for legit weak signals + reject hopeless ones"
            )

    def test_pre_warm_min_wait_per_generation(self) -> None:
        """Pre-warm `min_total_wait` must cover encoder warm-up:
        - Gen1 indoor: 360 SoC is fast → ≤ 30 s
        - Gen2 outdoor: heavier encoder → up to 60 s
        """
        gen1_indoor = get_model_config("INDOOR")
        gen2_outdoor = get_model_config("HOME_Eyes_Outdoor")
        assert gen1_indoor.min_total_wait <= 30
        # Gen2 outdoor needs more time
        assert gen2_outdoor.min_total_wait >= gen1_indoor.min_total_wait

    def test_renewal_interval_at_most_session_duration(self) -> None:
        """`renewal_interval` must be ≤ `max_session_duration` —
        otherwise the renewal happens AFTER the session times out and
        the stream drops.

        Equal values are OK for Gen2 Outdoor (HOME_Eyes_Outdoor) where
        `renewal_interval=heartbeat_interval=max_session_duration=3600`
        is intentional: PUT /connection rotates Digest creds, so we
        skip PUT-based renewal entirely and rely on FFmpeg's
        GET_PARAMETER to keep the session alive in-flight.
        """
        for hw in MODELS:
            cfg = get_model_config(hw)
            assert cfg.renewal_interval <= cfg.max_session_duration, (
                f"{hw}: renewal_interval={cfg.renewal_interval} > "
                f"max_session_duration={cfg.max_session_duration} — would "
                "cause stream drops"
            )

    def test_heartbeat_interval_sane(self) -> None:
        """`heartbeat_interval` ≤ `max_session_duration`. For Gen2 Outdoor
        the value is intentionally high (3600) to avoid Digest-cred
        rotation."""
        for hw in MODELS:
            cfg = get_model_config(hw)
            assert cfg.heartbeat_interval <= cfg.max_session_duration


def test_no_gen2_360_in_registry() -> None:
    """Bosch never released a Gen2 360° camera. The lineup is:
      Gen1: Eyes Außenkamera, 360 Innenkamera
      Gen2: Eyes Außenkamera II (outdoor), Eyes Innenkamera II (regular indoor — NOT a 360°)

    If MODELS ever gains an entry where "360" appears in the hw_id or display_name
    AND generation >= 2, that's almost certainly a copy-paste error from a public
    post / docs PR — not a real hardware release. Catch it before it ships.
    """
    for hw_id, cfg in MODELS.items():
        mentions_360 = "360" in hw_id.lower() or "360" in cfg.display_name.lower()
        if mentions_360:
            assert cfg.generation == 1, (
                f"MODELS[{hw_id!r}] (display_name={cfg.display_name!r}) "
                f"claims generation={cfg.generation} — but no Gen2 360° camera "
                f"exists in Bosch's lineup. If Bosch actually ships a Gen2 360°, "
                f"remove this guard in the same PR that adds the hardware."
            )


# ─────────────────────────────────────────────────────────────────────────────
# Section: GH#3 — Gen2 Outdoor model config (relocated from
# tests/test_github_issues.py)
# ─────────────────────────────────────────────────────────────────────────────


def test_gh3_gen2_outdoor_model_config_exists():
    """Gen2 Outdoor hardware version must resolve to a generation-2 config."""
    from custom_components.bosch_shc_camera.models import get_model_config

    cfg = get_model_config("HOME_Eyes_Outdoor")
    assert cfg.generation == 2
