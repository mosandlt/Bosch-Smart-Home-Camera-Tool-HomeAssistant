"""Regression tests for GitHub issues #15, #16, #17 (open as of 2026-05-29).

CLAUDE.md `TEST_EVERY_BUG`: every reported bug/feature gets a pinned test
with the fix. The Lovelace card is plain JS (no JS test runner in this
repo), so — exactly like ``TestGH4_CardFrontend`` in
``test_github_issues.py`` — these tests assert against the canonical card
source ``src/bosch-camera-card.js`` and the bundled mirror under
``custom_components/.../www/``.

| #  | Title                                                        | Author      |
|----|--------------------------------------------------------------|-------------|
| 15 | Option to hide unnecessary stuff in custom:bosch-camera-card | RkcCorian   |
| 16 | Exit fullscreen by clicking the fullscreen button again      | RkcCorian   |
| 17 | New card always stuck on bosch_garten / GUI can't change cam | RkcCorian   |
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_CARD_SRC = _REPO / "src" / "bosch-camera-card.js"
_CARD_BUNDLE = (
    _REPO / "custom_components" / "bosch_shc_camera" / "www" / "bosch-camera-card.js"
)


@pytest.fixture(scope="module")
def card_src() -> str:
    assert _CARD_SRC.exists(), f"card source missing: {_CARD_SRC}"
    return _CARD_SRC.read_text(encoding="utf-8")


class TestGH17_CameraDefaultStuck:
    """Root cause: ``getStubConfig`` hard-coded ``camera.bosch_garten`` (an
    entity that only exists in the author's install) and the editor's camera
    dropdown only surfaced entities whose id/brand contained "bosch", leaving
    a single, unselectable option for everyone else.
    """

    def test_stub_config_not_hardcoded_to_bosch_garten(self, card_src: str) -> None:
        """getStubConfig must not pin a specific author-only entity."""
        idx = card_src.find("static getStubConfig")
        assert idx != -1, "getStubConfig missing"
        body = card_src[idx : idx + 600]
        assert 'camera_entity: "camera.bosch_garten"' not in body, (
            "getStubConfig must not hardcode camera.bosch_garten (GH#17)"
        )

    def test_stub_config_resolves_from_hass_states(self, card_src: str) -> None:
        """The stub must derive the camera from the live install."""
        idx = card_src.find("static getStubConfig")
        body = card_src[idx : idx + 600]
        assert "getStubConfig(hass)" in card_src, (
            "getStubConfig must accept hass to discover a real camera"
        )
        assert "hass.states" in body and "camera." in body, (
            "getStubConfig must pick a camera.* entity from hass.states (GH#17)"
        )

    def test_camera_picker_falls_back_to_all_cameras(self, card_src: str) -> None:
        """When no bosch-tagged camera is found, list every camera.* entity so
        the dropdown is never empty/single-option."""
        # Anchor on the METHOD declaration, not an earlier call site (the editor's
        # hass-change guard also calls _bosch_cameras()).
        idx = card_src.find("_bosch_cameras() {")
        assert idx != -1, "_bosch_cameras method missing"
        body = card_src[idx : idx + 800]
        assert "out.length === 0" in body, (
            "_bosch_cameras must have an empty-list fallback (GH#17)"
        )

    def test_editor_keeps_configured_entity_in_options(self, card_src: str) -> None:
        """A configured entity missing from discovery must still be added to
        the option list so it stays selected (not silently dropped)."""
        assert "!cams.includes(cfg.camera_entity)" in card_src, (
            "editor must keep the configured camera_entity as an option (GH#17)"
        )


class TestGH16_FullscreenToggle:
    """The native (desktop/Android) path called requestFullscreen
    unconditionally. It must check whether we are already fullscreen and
    exit instead. The iOS CSS-fullscreen path already toggled via fs-active.
    """

    def test_native_path_checks_fullscreen_element(self, card_src: str) -> None:
        idx = card_src.find("_requestFullscreen() {")
        assert idx != -1, "_requestFullscreen missing"
        body = card_src[idx : idx + 2000]
        assert "_isNativeFullscreen()" in body, (
            "_requestFullscreen must detect active native fullscreen (GH#16)"
        )

    def test_fullscreen_detection_is_shadow_dom_aware(self, card_src: str) -> None:
        """document.fullscreenElement retargets to the shadow HOST, not the
        inner #img-wrapper, so the detector must consult
        shadowRoot.fullscreenElement / `=== this` instead (GH#16 root cause)."""
        assert "_isNativeFullscreen()" in card_src, "_isNativeFullscreen helper missing"
        assert (
            "sr.fullscreenElement" in card_src
            or "shadowRoot.fullscreenElement" in card_src
        ), "must read shadowRoot.fullscreenElement (retargeting fix)"
        assert "docFs === this" in card_src, (
            "must treat host (=== this) as fullscreen (retargeting fix)"
        )

    def test_native_path_calls_exit_fullscreen(self, card_src: str) -> None:
        idx = card_src.find("_requestFullscreen() {")
        body = card_src[idx : idx + 2000]
        for api in (
            "document.exitFullscreen",
            "document.webkitExitFullscreen",
        ):
            assert api in body, f"{api} must be called to exit fullscreen (GH#16)"

    def test_css_fullscreen_still_toggles(self, card_src: str) -> None:
        """The pre-existing iOS toggle must remain."""
        idx = card_src.find("_requestFullscreen() {")
        body = card_src[idx : idx + 2000]
        assert 'classList.contains("fs-active")' in body
        assert "_exitCssFullscreen()" in body


class TestGH15_HideOptions:
    """New ``show_title`` / ``show_last_event`` config flags let users strip
    the card down to a clean video tile (matching the overview-card look)."""

    @pytest.mark.parametrize("key", ["show_title", "show_last_event"])
    def test_config_keys_present(self, card_src: str, key: str) -> None:
        assert f"config.{key} !== false" in card_src, (
            f"setConfig must read {key} with a true default (GH#15)"
        )

    @pytest.mark.parametrize(
        "selector",
        [":host(.no-title) .ap-top", ":host(.no-last-event) .ap-last-event"],
    )
    def test_hide_css_present(self, card_src: str, selector: str) -> None:
        assert selector in card_src, f"missing hide rule '{selector}' (GH#15)"

    @pytest.mark.parametrize(
        "toggle",
        [
            'classList.toggle("no-title", !this._config.show_title)',
            'classList.toggle("no-last-event", !this._config.show_last_event)',
        ],
    )
    def test_host_class_toggles(self, card_src: str, toggle: str) -> None:
        assert toggle in card_src, f"missing host-class toggle: {toggle} (GH#15)"

    @pytest.mark.parametrize("name", ["show_title", "show_last_event"])
    def test_editor_exposes_checkbox(self, card_src: str, name: str) -> None:
        assert f'input[name="{name}"]' in card_src, (
            f"visual editor must wire the {name} checkbox (GH#15)"
        )


class TestCardFeatureParity:
    """Both cards must expose the same design/hide sub-features. The overview
    card renders child single-cards, so the rendered features already match;
    these pin the editor + propagation parity (user request 2026-05-29)."""

    @pytest.mark.parametrize("key", ["show_title", "show_last_event"])
    def test_overview_propagates_hide_options(self, card_src: str, key: str) -> None:
        """The overview card must read the hide flags and fold them into
        card_defaults so every tile inherits them."""
        assert f"config.{key} !== false" in card_src, (
            f"overview setConfig must read {key} (parity)"
        )
        assert f"{key}: this._config.{key}" in card_src, (
            f"overview must propagate {key} into card_defaults (parity)"
        )

    @pytest.mark.parametrize(
        "name",
        ["apple_style", "compact", "minimal", "show_title", "show_last_event"],
    )
    def test_overview_editor_exposes_design_options(
        self, card_src: str, name: str
    ) -> None:
        """The overview editor must expose the same design/hide checkboxes the
        single-card editor has."""
        idx = card_src.find("class BoschCameraOverviewCardEditor")
        assert idx != -1, "overview editor missing"
        body = card_src[idx : idx + 9000]
        # The editor builds inputs via chk("key", …) + binds via onChk("key", …);
        # the name="…" attribute is a runtime template, so assert the calls.
        assert f'chk("{name}"' in body, (
            f"overview editor must render the {name} checkbox (parity)"
        )
        assert f'onChk("{name}"' in body, (
            f"overview editor must bind the {name} checkbox (parity)"
        )


class TestControlStackVisibility:
    """The control stack (switches + accordions) visibility logic, reworked
    2026-05-29 after live testing: 'minimal meaningful' + offline cleanup."""

    def test_overflow_open_default_expression(self, card_src: str) -> None:
        """Non-minimal, non-compact apple-style cards start expanded (⋮ open)."""
        assert (
            "this._config.apple_style && !this._config.minimal && !this._config.compact"
            in card_src
        ), "overflow-open default must be apple_style && !minimal && !compact"

    def test_more_button_state_synced(self, card_src: str) -> None:
        """The ⋮ button must reflect the open state from first paint."""
        assert "_syncMoreButton()" in card_src
        assert 'more.classList.toggle("on", open)' in card_src

    def test_white_gap_collapsed_rows_zero_padding_border(self, card_src: str) -> None:
        """Collapsed apple-style rows must zero padding+border so no white strip
        renders below the video (max-height:0 does not clip them)."""
        idx = card_src.find(":host(.apple-style) .switch-rows,")
        body = card_src[idx : idx + 600]
        assert "padding-bottom: 0" in body and "border-top-width: 0" in body, (
            "collapsed rows must zero padding/border (white-gap fix)"
        )

    def test_offline_hides_controls_when_not_minimal(self, card_src: str) -> None:
        """Offline + expanded must hide the control stack except automations."""
        assert ":host(.apple-style.cam-offline:not(.minimal)) .switch-rows" in card_src
        assert ".accordion:not(#acc-automations)" in card_src, (
            "offline expanded cards keep only the Automations accordion"
        )

    def test_offline_hides_redundant_title_pill(self, card_src: str) -> None:
        """The garbled offline label was the title pill overlapping the offline
        pill — the title pill is hidden when offline."""
        assert ":host(.apple-style.cam-offline) .ap-top { display: none" in card_src

    def test_overview_tiles_default_minimal(self, card_src: str) -> None:
        """Overview grid tiles default to minimal:true (glanceable)."""
        assert "config.minimal !== false" in card_src, (
            "overview minimal must default to true"
        )


class TestDebugLineRemoved:
    """The on-card debug line (`Card vX | fresh … | WxH`) was removed in
    v13.3.3 (user request 2026-05-30 — no longer needed)."""

    def test_no_debug_line_element(self, card_src: str) -> None:
        assert 'id="debug-line"' not in card_src, (
            "the debug-line element must be gone (removed v13.3.3)"
        )

    def test_no_debug_line_textcontent_update(self, card_src: str) -> None:
        assert 'getElementById("debug-line")' not in card_src, (
            "no code should reference the removed debug-line element"
        )


class TestThemeModeSwitcherReflectsConfig:
    """issue #15 (2026-05-30): with `theme: ios` set in YAML (and no in-card
    localStorage override) the in-card switcher wrongly showed "Auto" selected.
    The switcher chips must mirror _resolveTheme/_resolveMode precedence
    (localStorage → config → auto), i.e. fall back to the configured value."""

    def test_theme_switcher_falls_back_to_config(self, card_src: str) -> None:
        assert "const cfgTheme = this._config?.theme;" in card_src, (
            "theme switcher must consider the configured theme, not only localStorage"
        )
        assert 'cfgTheme === "ios"' in card_src

    def test_mode_switcher_falls_back_to_config(self, card_src: str) -> None:
        assert "const cfgMode = this._config?.mode;" in card_src, (
            "mode switcher must consider the configured mode, not only localStorage"
        )
        assert 'cfgMode === "day"' in card_src


class TestOsChecks:
    """Cross-OS robustness (2026-05-30, issue #15 reporter on Edge/Win11; the
    card is developed/tested on macOS only). The card stamps an os-<name> host
    class for OS-targeted CSS and uses a cross-platform system-font fallback."""

    @pytest.mark.parametrize("os_name", ["windows", "macos", "ios", "android", "linux"])
    def test_os_host_classes_present(self, card_src: str, os_name: str) -> None:
        assert '"os-" + c' in card_src, "OS host class must be applied"
        assert f'"{os_name}"' in card_src, f"OS detection must cover {os_name}"

    def test_os_detection_uses_ua_not_deprecated_platform(self, card_src: str) -> None:
        assert "_applyOsClass" in card_src
        idx = card_src.find("_applyOsClass() {")
        body = card_src[idx : idx + 800]
        assert "navigator.userAgent" in body
        assert "navigator.platform" not in body, (
            "navigator.platform is deprecated — use the UA string"
        )

    def test_font_stack_has_cross_platform_fallback(self, card_src: str) -> None:
        # system-ui + Segoe UI cover Windows/Linux where -apple-system is ignored.
        assert "system-ui" in card_src and '"Segoe UI"' in card_src, (
            "font stack must fall back to system-ui / Segoe UI for non-Apple OSes"
        )


class TestCardBundleMirror:
    def test_bundle_exists(self) -> None:
        assert _CARD_BUNDLE.exists(), (
            "bundled card mirror missing — run scripts/build-card.mjs"
        )

    @pytest.mark.parametrize(
        "needle",
        ["getStubConfig", "show_title", "show_last_event", "exitFullscreen"],
    )
    def test_bundle_contains_fix_tokens(self, needle: str) -> None:
        """The minified bundle must carry the same identifiers (build ran)."""
        text = _CARD_BUNDLE.read_text(encoding="utf-8")
        assert needle in text, (
            f"'{needle}' not in bundled card — rebuild the card before commit"
        )
