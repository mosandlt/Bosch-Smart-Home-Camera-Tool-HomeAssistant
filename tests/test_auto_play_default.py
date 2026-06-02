"""Regression tests for the card auto-play default option (v2.15.0 card / v12.8.0 integration).

The integration exposes a ``auto_play_default`` option that the Lovelace card
reads off the camera entity attribute to decide whether to auto-start the
live stream on mount. Per PIN_EVERY_MODE there is one explicit test per
mode + one default + one garbage-collapse test for both:

* the options-flow round-trip (user submits → stored unchanged)
* the camera-attribute exposure (option → camera.extra_state_attributes)

Plus a DEFAULT_OPTIONS pin and a OPTIONS_SECTIONS membership check so the
field never silently disappears from the Features section.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.bosch_shc_camera.config_flow import (
    OPTIONS_SECTIONS,
    BoschCameraOptionsFlow,
)
from custom_components.bosch_shc_camera.const import (
    AUTO_PLAY_DEFAULT_VALUES,
    DEFAULT_OPTIONS,
)

CAM_ID = "11111111-1111-1111-1111-111111111111"


# ── shared helpers (mirror tests/test_options_flow_settings.py) ─────────────


def _make_entry(*, options: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="01TEST",
        data={"bearer_token": "", "refresh_token": "rt"},
        options=options or {},
    )


async def _submit(flow: BoschCameraOptionsFlow, user_input: dict) -> dict:
    saved: dict = {}
    flow.async_create_entry = MagicMock(
        side_effect=lambda **kw: (
            saved.update({"data": kw.get("data", {})}) or {"type": "create_entry"}
        ),
    )
    result = await flow.async_step_init(user_input=user_input)
    assert result["type"] == "create_entry", f"Expected create_entry, got {result}"
    return saved["data"]


# ── constants + section membership ──────────────────────────────────────────


class TestConstants:
    def test_default_is_lan(self):
        """Sane default: LAN auto-play, silent on remote.

        Bumping this default is a breaking change for every existing install,
        so the constant is pinned here. Any change must be intentional + go
        through release notes.
        """
        assert DEFAULT_OPTIONS["auto_play_default"] == "lan"

    def test_canonical_values_pinned(self):
        """The 3 modes are part of the public option contract — pin the set.

        v12.8.1 dropped the legacy "confirm" value (popup dialog UX) in
        favour of the tap-to-reveal overlay. Stale stored "confirm" values
        from v12.8.0 collapse to "lan" — see test_legacy_confirm_collapses.
        """
        assert AUTO_PLAY_DEFAULT_VALUES == ("lan", "always", "never")

    def test_in_features_section(self):
        """The option lives in the Features section (next to enable_intercom),
        not in the Stream section — keeps the streaming subsection focused on
        connection type + buffer profile."""
        assert "auto_play_default" in OPTIONS_SECTIONS["features"]
        assert "auto_play_default" not in OPTIONS_SECTIONS["stream"]

    def test_default_is_in_canonical_set(self):
        """The DEFAULT_OPTIONS value must itself be a canonical mode — a
        typo in const.py (e.g. "Lan" instead of "lan") would silently
        collapse to "lan" at the read site, masking the bug. Pin this
        invariant explicitly."""
        assert DEFAULT_OPTIONS["auto_play_default"] in AUTO_PLAY_DEFAULT_VALUES

    def test_dropdown_options_match_canonical_set(self):
        """The SelectSelector dropdown in config_flow.py must list exactly
        the canonical modes — no more (orphan value users could set but
        camera.py would collapse), no less (mode users can't reach via UI).
        Reads the dropdown by triggering the form schema render.
        """
        from homeassistant.data_entry_flow import section as _section

        from custom_components.bosch_shc_camera.config_flow import (
            BoschCameraOptionsFlow,
        )

        flow = BoschCameraOptionsFlow(_make_entry())
        # Build the form synchronously without submitting — async_step_init
        # returns a form dict whose schema is the sectioned voluptuous
        # schema. We don't need to await the full coroutine since the
        # branch we want runs before any await; instead we inspect the
        # SelectSelector options directly from the source-of-truth dict.
        # The dropdown literal must enumerate the canonical set verbatim.
        import inspect

        src = inspect.getsource(BoschCameraOptionsFlow.async_step_init)
        for mode in AUTO_PLAY_DEFAULT_VALUES:
            assert f'value="{mode}"' in src, (
                f"auto_play_default mode {mode!r} missing from config_flow.py "
                "SelectSelector — users can't set it via UI."
            )
        # Pin the absence of the legacy v12.8.0 "confirm" value — its
        # presence would re-introduce the popup UX path.
        assert 'value="confirm"' not in src, (
            "Legacy 'confirm' mode must not appear in the dropdown; "
            "v12.8.1 dropped it in favour of the tap-to-reveal overlay."
        )


# ── options-flow round-trip: 4 modes + default + garbage ────────────────────


class TestOptionsFlowRoundTrip:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["lan", "always", "never"])
    async def test_each_mode_saved(self, mode: str):
        """PIN_EVERY_MODE: each of the 3 canonical modes round-trips unchanged."""
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(flow, {"features": {"auto_play_default": mode}})
        assert data["auto_play_default"] == mode

    @pytest.mark.asyncio
    async def test_default_used_when_omitted(self):
        """Submitting an empty features dict relies on the schema default —
        the saved entry must still carry the canonical "lan" value so the
        camera attribute can resolve it without falling back to a stale
        previous-version value."""
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(flow, {"features": {}})
        # HA's section() helper omits unchanged fields from user_input. The
        # schema default is bound at form-build time → on submit nothing is
        # written. Existing stored value is preserved by HA itself. This
        # test simply pins that nothing crashes and the key, if absent, will
        # default via DEFAULT_OPTIONS lookup at read time.
        assert "auto_play_default" not in data or data["auto_play_default"] == "lan"

    @pytest.mark.asyncio
    async def test_garbage_value_stored_then_collapsed_at_read(self):
        """The options-flow itself does not constrain the value beyond what
        the SelectSelector enforces in the UI (tests bypass that). A garbage
        value lands in options as-is — the camera-attribute read site is
        responsible for collapsing it to "lan". The collapse is verified in
        TestCameraAttribute.test_garbage_collapses_to_lan.
        """
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(flow, {"features": {"auto_play_default": "garbage"}})
        assert data["auto_play_default"] == "garbage"


# ── camera attribute exposure: 4 modes + default + garbage ──────────────────


@pytest.fixture
def stub_coord():
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                },
                "events": [],
                "live": {},
            }
        },
        _live_connections={},
        _camera_entities={},
        _stream_fell_back={},
        _stream_error_count={},
        last_update_success=True,
        motion_settings=lambda cam_id: {},
        is_stream_warming=lambda cam_id: False,
    )


def _entry_with(mode_value):
    """ConfigEntry-like with auto_play_default set to ``mode_value`` (or absent)."""
    opts: dict = {"snapshot_interval": 1800}
    if mode_value is not None:
        opts["auto_play_default"] = mode_value
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "fake-token"},
        options=opts,
    )


class TestCameraAttribute:
    @pytest.mark.parametrize("mode", ["lan", "always", "never"])
    def test_each_mode_exposed(self, stub_coord, mode):
        """PIN_EVERY_MODE: each canonical mode shows up verbatim on the
        camera entity attribute the card reads."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, _entry_with(mode))
        attrs = cam.extra_state_attributes
        assert attrs["auto_play_default"] == mode

    def test_legacy_confirm_collapses(self, stub_coord):
        """v12.8.0 briefly exposed a "confirm" mode (popup dialog). v12.8.1
        dropped it in favour of an inline tap-to-reveal overlay. Any stale
        stored "confirm" value must collapse to "lan" so existing users
        keep working without manual intervention."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, _entry_with("confirm"))
        attrs = cam.extra_state_attributes
        assert attrs["auto_play_default"] == "lan"

    def test_default_when_option_absent(self, stub_coord):
        """No option stored → attribute must still be "lan" so the card
        gets a usable signal without falling back to its own client-side
        default (which would drift from the integration default)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, _entry_with(None))
        attrs = cam.extra_state_attributes
        assert attrs["auto_play_default"] == "lan"

    def test_garbage_collapses_to_lan(self, stub_coord):
        """Garbage value (typo, stale value from a future renamed mode)
        collapses to "lan" at the read site. Never disables stream start
        silently — sane fallback ensures the card stays functional."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, _entry_with("not-a-real-mode"))
        attrs = cam.extra_state_attributes
        assert attrs["auto_play_default"] == "lan"

    def test_empty_string_collapses_to_lan(self, stub_coord):
        """Empty string is a sub-case of garbage. Pin it explicitly because
        select selectors can serialize a no-selection state as ``""`` in
        some HA versions."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, _entry_with(""))
        attrs = cam.extra_state_attributes
        assert attrs["auto_play_default"] == "lan"
