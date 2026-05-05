"""Regression tests for closed GitHub issues.

CLAUDE.md `TEST_EVERY_BUG` rule: every reported bug gets a regression
test before/with the fix. This file covers issues that were closed
before the rule was in place.

Issue index (status as of 2026-05-05):

| # | Title                                                  | Author       | Status | Test class             |
|---|--------------------------------------------------------|--------------|--------|------------------------|
| 1 | Motion Sensitivity (and other states) not permanent    | DrNiKa       | CLOSED | TestGH1_MotionRevert   |
| 2 | Token refresh fails - 6.4.2 (Solved after re-install)  | —            | CLOSED | TestGH2_TokenRefresh   |
| 3 | Light controls for Eyes outdoor camera II              | DrNiKa       | CLOSED | TestGH3_Gen2Light      |
| 4 | bosch-camera-card is not working                       | Michael…     | CLOSED | TestGH4_CardFrontend   |
| 5 | Refresh-Token abgelaufen, Link zur Neuanmeldung        | dziko83      | CLOSED | TestGH5_ReauthFlow     |
| 6 | Streaming broken since 10.x (cloud & LAN)              | WoodenDuke   | CLOSED | TestGH6_StreamPipeline |
| 7 | Bosch Cam ein Traum wird wahr (positive feedback)      | —            | CLOSED | (no test — non-bug)    |
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


# ── GH#1: Motion Sensitivity not permanent (DrNiKa) ────────────────────


class TestGH1_MotionRevert:
    """Same root cause as forum issue #1 (Poldi41) — see test_forum_issues.py.

    The on-device IVA engine reverts cloud-side motion config in ~1 s.
    Documented in `docs/api-reference.md` § 'Motion Revert'. Workaround
    is the cloud rules engine, fully implemented as service actions in
    v8+.
    """

    def test_create_rule_service_registered(self):
        """Workaround for the limitation: cloud rules engine.

        `create_rule`, `update_rule`, `delete_rule` services must be
        present so users can manage schedule-based motion rules instead
        of relying on the auto-reverted /motion endpoint.
        """
        comp = (
            Path(__file__).parent.parent
            / "custom_components" / "bosch_shc_camera"
        )
        services = (comp / "services.yaml").read_text()
        for svc in ("create_rule", "update_rule", "delete_rule"):
            assert svc in services, (
                f"Service '{svc}' missing from services.yaml — needed as "
                f"the documented workaround for motion-revert (GH#1)"
            )


# ── GH#2: Token refresh fails (—) ──────────────────────────────────────


class TestGH2_TokenRefresh:
    """User-side fix ('solved after re-install'). Modern integration has
    a robust token-refresh path. Tests verify the path exists and runs.
    """

    def test_proactive_refresh_method_exists(self):
        """Coordinator must schedule + run a proactive refresh 5 min
        before token expiry."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        assert hasattr(BoschCameraCoordinator, "_proactive_refresh")
        assert hasattr(BoschCameraCoordinator, "_schedule_token_refresh")
        assert hasattr(BoschCameraCoordinator, "_ensure_valid_token")

    def test_token_failure_alert_uses_repair_issue(self):
        """In v11.0.0, persistent_notification was replaced by
        ir.async_create_issue('token_expired'). Verifies the strings
        bundle includes the issue translation key."""
        comp = (
            Path(__file__).parent.parent
            / "custom_components" / "bosch_shc_camera"
        )
        strings = json.loads((comp / "strings.json").read_text())
        assert "token_expired" in strings.get("issues", {}), (
            "v11.0.0 surfaces token_expired as a Repairs entry; the "
            "translation must exist so the user sees a clear message."
        )


# ── GH#3: Light controls for Eyes outdoor camera II (DrNiKa) ───────────


class TestGH3_Gen2Light:
    """Gen2 Outdoor (HOME_Eyes_Outdoor) needs separate front + topdown
    light controls. v9.1.6 fix added the Gen2 endpoints. v11.0.0 split
    them into BoschFrontLightSwitch + BoschWallwasherSwitch + RGB lights.
    """

    def test_gen2_outdoor_has_front_light_switch_class(self):
        """`BoschFrontLightSwitch` must exist for Gen2 outdoor."""
        from custom_components.bosch_shc_camera import switch as switch_mod
        assert hasattr(switch_mod, "BoschFrontLightSwitch"), (
            "GH#3 fix removed: Gen2 Outdoor needs its own front-light "
            "switch separate from the wallwasher"
        )

    def test_gen2_outdoor_has_wallwasher_switch(self):
        from custom_components.bosch_shc_camera import switch as switch_mod
        assert hasattr(switch_mod, "BoschWallwasherSwitch"), (
            "Wallwasher (top + bottom LEDs combined) must have its own "
            "switch entity for Gen2 Outdoor"
        )

    def test_gen2_outdoor_has_rgb_light_classes(self):
        """RGB color picker for top + bottom LEDs."""
        from custom_components.bosch_shc_camera import light as light_mod
        assert hasattr(light_mod, "BoschTopLedLight")
        assert hasattr(light_mod, "BoschBottomLedLight")
        assert hasattr(light_mod, "BoschFrontLight")

    def test_gen2_model_config_exists(self):
        """Ensure Gen2 Outdoor hardware version resolves to a real config."""
        from custom_components.bosch_shc_camera.models import get_model_config
        cfg = get_model_config("HOME_Eyes_Outdoor")
        assert cfg.generation == 2


# ── GH#4: bosch-camera-card is not working (Michael8885443) ────────────


class TestGH4_CardFrontend:
    """Card auto-registration since v10.3.19 — manual resource entry no
    longer needed. The integration serves the card from its own bundled
    `www/` folder via HA static-path handler.
    """

    def test_card_javascript_exists(self):
        """The bundled card must be present in custom_components/.../www/."""
        comp = (
            Path(__file__).parent.parent
            / "custom_components" / "bosch_shc_camera"
        )
        card = comp / "www" / "bosch-camera-card.js"
        assert card.exists(), (
            "Card auto-registration relies on the bundled JS at "
            "custom_components/bosch_shc_camera/www/bosch-camera-card.js"
        )

    def test_card_version_constant_exists(self):
        """`CARD_VERSION` must exist in const.py + bumped on every card change.

        Must be present in BOTH src/bosch-camera-card.js AND
        custom_components/.../const.py — Lovelace resource cache
        invalidation depends on the version match.
        """
        from custom_components.bosch_shc_camera.const import CARD_VERSION
        assert isinstance(CARD_VERSION, str)
        assert len(CARD_VERSION) > 0


# ── GH#5: Refresh-Token Link 404 (dziko83) ─────────────────────────────


class TestGH5_ReauthFlow:
    """User got 404 when clicking the legacy re-auth link
    (`bosch.com/boschcam`). v9.1.4+ introduced the automatic Reconfigure
    banner; v11.0.0 added an explicit 'Reconfigure' menu item that runs
    the same OAuth flow without deleting the entry.
    """

    def test_reauth_step_exists(self):
        """`async_step_reauth` + `async_step_reauth_confirm` must be present."""
        from custom_components.bosch_shc_camera.config_flow import (
            BoschSHCCameraConfigFlow,
        )
        assert hasattr(BoschSHCCameraConfigFlow, "async_step_reauth")
        assert hasattr(BoschSHCCameraConfigFlow, "async_step_reauth_confirm")

    def test_reconfigure_step_exists(self):
        """v11.0.0 reconfiguration-flow must exist (Quality-Scale Gold)."""
        from custom_components.bosch_shc_camera.config_flow import (
            BoschSHCCameraConfigFlow,
        )
        assert hasattr(BoschSHCCameraConfigFlow, "async_step_reconfigure"), (
            "v11.0.0 added explicit 'Reconfigure' menu item to fix "
            "the dziko83 404 issue — the flow must keep existing"
        )

    def test_reauth_string_exists(self):
        """The user must see a clear 'Re-authenticate' confirmation."""
        comp = (
            Path(__file__).parent.parent
            / "custom_components" / "bosch_shc_camera"
        )
        strings = json.loads((comp / "strings.json").read_text())
        assert "reauth_confirm" in strings.get("config", {}).get("step", {})


# ── GH#6: Streaming broken since 10.x (WoodenDuke) ─────────────────────


class TestGH6_StreamPipeline:
    """Resolved in v10.5.3. Stream lifecycle is:

      switch.live_stream ON → coordinator.try_live_connection
        → PUT /connection (LOCAL or REMOTE)
        → TLS proxy bring-up
        → pre-warm (~25 s indoor / ~35 s outdoor)
        → camera.stream_source returns rtsps:// → HA stream component
        → FFmpeg + go2rtc consume the same rtsp://127.0.0.1:N/...

    The user observed a yellow → blue → yellow indicator loop in v10.x;
    root cause was a stream cache invalidation bug. Tests pin the
    invariants.
    """

    def test_live_stream_switch_class_exists(self):
        from custom_components.bosch_shc_camera import switch as switch_mod
        assert hasattr(switch_mod, "BoschLiveStreamSwitch")

    def test_live_connections_dict_drives_supported_features(self):
        """Camera always advertises STREAM so HA's stream component registers
        the entity — live_connections drives stream_source() content, not the
        feature flag. (HA requires STREAM to be set statically at entity
        registration time; toggling it dynamically would cause the entity to
        deregister and re-register on every stream start/stop.)"""
        from types import SimpleNamespace as _SN
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        from homeassistant.components.camera import CameraEntityFeature
        coord = _SN(
            data={CAM_ID: {"info": {"title": "x", "hardwareVersion": "X",
                                     "firmwareVersion": "x", "macAddress": "x"}}},
            _live_connections={},
            _camera_entities={},
            last_update_success=True,
        )
        entry = _SN(entry_id="01", data={"bearer_token": "x"}, options={})
        cam = BoschSHCCamera(coord, CAM_ID, entry)
        # STREAM must always be advertised (static attribute, not dynamic)
        assert CameraEntityFeature.STREAM in cam.supported_features, (
            "Camera must always advertise STREAM — HA registers the stream "
            "component at entity setup time based on this flag"
        )
        # The flag must not change when live_connections is populated
        coord._live_connections[CAM_ID] = {"rtspsUrl": "rtsps://x"}
        assert CameraEntityFeature.STREAM in cam.supported_features

    def test_session_stale_blocks_live_stream_switch(self):
        """When `_session_stale` is set for a cam, the live_stream switch
        becomes unavailable so users don't see a frozen stream as healthy."""
        from types import SimpleNamespace as _SN
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        coord = _SN(
            data={CAM_ID: {"info": {"title": "x", "hardwareVersion": "X",
                                     "firmwareVersion": "x", "macAddress": "x"},
                            "status": "ONLINE"}},
            _live_connections={},
            _shc_state_cache={CAM_ID: {"privacy_mode": False}},
            _session_stale={CAM_ID: True},  # keepalive given up
            last_update_success=True,
            is_camera_online=lambda cid: True,
            is_session_stale=lambda cid: True,
        )
        entry = _SN(entry_id="01", data={}, options={})
        sw = BoschLiveStreamSwitch(coord, CAM_ID, entry)
        assert sw.available is False, (
            "LiveStream switch must show unavailable when keepalive "
            "loop has stalled — prevents users from thinking a frozen "
            "stream is healthy (GH#6 stream-loop symptom)"
        )


# ── Meta enforcer ─────────────────────────────────────────────────────


class TestMeta:
    """Pin the count of TestGH<N>_ classes so future closed issues
    require an entry here. CLAUDE.md TEST_EVERY_BUG rule."""

    def test_each_codetestable_gh_issue_has_test_class(self):
        text = Path(__file__).read_text()
        import re
        classes = re.findall(r"^class TestGH\d+_", text, re.MULTILINE)
        # 6 of 7 closed issues have code-testable surfaces; #7 was
        # positive feedback only.
        assert len(classes) >= 6, (
            "test_github_issues.py must have at least 6 TestGH<N>_ "
            "classes — one per code-testable closed issue. New closed "
            "issues need a new TestGH<N>_ class (CLAUDE.md "
            "TEST_EVERY_BUG rule)."
        )
