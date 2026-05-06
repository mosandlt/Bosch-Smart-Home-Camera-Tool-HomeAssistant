"""Card-source regression tests.

These tests pin behaviors of the Lovelace card (`src/bosch-camera-card.js`)
that have caused observable bugs in the past. Since the card runs in the
browser and this repo has no JS test runner, we verify the contract by
string-searching the source — crude but it catches accidental deletion
of critical event listeners.
"""

from pathlib import Path

import pytest

CARD_SRC = (
    Path(__file__).parent.parent / "src" / "bosch-camera-card.js"
)
CONST_PY = (
    Path(__file__).parent.parent
    / "custom_components"
    / "bosch_shc_camera"
    / "const.py"
)


@pytest.fixture(scope="module")
def card_source() -> str:
    return CARD_SRC.read_text()


def test_pagehide_listener_wired(card_source: str) -> None:
    """Mobile reload fix: card must hook `pagehide` to call `_stopLiveVideo`.

    Reason: iOS Safari + HA Companion App (WKWebView) do not reliably fire
    `disconnectedCallback` on tab reload. Without an explicit teardown,
    the previous `RTCPeerConnection` lingers as a stale consumer in go2rtc
    until its internal timeout (~10–15 s) frees the slot, blocking the
    new mount's `camera/webrtc/offer`. User saw "stream appears magically
    after many seconds" on phone (desktop unaffected).
    Fix: wire `pagehide` → `_stopLiveVideo()` in `connectedCallback`,
    remove in `disconnectedCallback`. Verified 2026-05-05.
    """
    assert 'addEventListener("pagehide"' in card_source, (
        "pagehide listener not registered — mobile reload will leak the "
        "WebRTC consumer to go2rtc and stall the next stream start by "
        "10–15 s. Re-add `window.addEventListener('pagehide', ...)` in "
        "connectedCallback() that calls this._stopLiveVideo()."
    )
    assert 'removeEventListener("pagehide"' in card_source, (
        "pagehide listener not removed in disconnectedCallback — leaks "
        "the closure on element teardown."
    )


def test_pagehide_calls_stop_live_video(card_source: str) -> None:
    """Pin the handler body: must call `_stopLiveVideo()` so the
    RTCPeerConnection + WS subscription are torn down before unload."""
    # Find the pagehide handler assignment line and check it stops the video.
    # Anchored to `_pagehideHandler` so an unrelated `pagehide` listener
    # elsewhere wouldn't pass this test.
    assert "_pagehideHandler" in card_source
    # Locate the handler body — single-line arrow function is fine
    handler_idx = card_source.find("_pagehideHandler =")
    assert handler_idx > 0, "_pagehideHandler assignment missing"
    handler_window = card_source[handler_idx : handler_idx + 200]
    assert "_stopLiveVideo()" in handler_window, (
        "pagehide handler must call this._stopLiveVideo() to flush "
        "pc.close() + WS-unsubscribe before page unloads."
    )


def test_overview_sort_promotes_active_stream(card_source: str) -> None:
    """Overview-card must surface the camera whose live stream is ON
    at position 1 within its tier.

    Reason: while watching a single camera live, the user wants that
    tile to stay in the top-left of the grid even if other tier-0
    cams sort earlier alphabetically / by Bosch priority. Detected
    via `switch.<base>_live_stream` state (NOT the camera attribute,
    which lags one coordinator tick).
    """
    assert "_live_stream`]" in card_source, (
        "Overview discover() must inspect switch.<base>_live_stream to "
        "detect active streams. If the entity name changes, update both "
        "the loop and this test."
    )
    assert "streamingOn !== b.streamingOn" in card_source, (
        "Sort comparator must include streamingOn so an active live "
        "camera moves to position 1 within its tier. Removing this "
        "comparison would silently revert the user-requested behavior."
    )
    assert 'streamingOn ? "S"' in card_source or "streamingOn?'S'" in card_source, (
        "_lastSig must include the streaming flag — otherwise "
        "_update() short-circuits the DOM reorder when only the "
        "stream toggle flipped, and the active camera stays where it was."
    )


def test_overview_grid_single_column_on_mobile_landscape(card_source: str) -> None:
    """Overview grid must collapse to a single column on phone-class
    devices in landscape (where viewport > 640px but rows collapse).

    Reason: phones in landscape (iPhone Plus/Pro Max ≈ 932×430) blow
    past the 640px max-width rule, so the original `(max-width: 640px)`
    breakpoint silently leaves a 2-column grid. With viewport height
    ~430px each tile renders ~12 lines tall — unusable. Fix layered
    on top of the existing portrait rule, never replacing it.
    """
    # Original portrait rule must still be present.
    assert "max-width: 640px" in card_source, (
        "Portrait single-column rule disappeared — that breaks small "
        "phones in portrait mode."
    )
    # Touch-device rule must catch landscape phones up to ~1024px.
    assert "(pointer: coarse) and (max-width: 1024px)" in card_source, (
        "Touch-device single-column rule missing — phones in landscape "
        "(e.g. iPhone Pro Max 932×430) will render 2 cramped columns."
    )
    # Short-landscape rule covers any device whose height collapses below 500px.
    assert "(orientation: landscape) and (max-height: 500px)" in card_source, (
        "Short-landscape rule missing — guarantees 1-column on any "
        "device whose landscape height < 500px regardless of pointer type."
    )


def test_pull_fresh_states_includes_camera_entity(card_source: str) -> None:
    """CARD_STALE_APP fix (2026-04-27): on mount the card must pull
    the camera entity state via REST so the badge color (idle / connecting /
    streaming / warming_up) reflects the backend immediately. Without
    this, the WS-pushed camera state can lag 2-5 s after the
    HA-Companion-App resumes from background, leaving the badge stuck
    yellow despite backend=streaming. Pinned by source-grep so a
    refactor of `_pullFreshSwitchStates` can't silently drop the
    camera entity from the pull list.
    """
    pull_idx = card_source.find("_pullFreshSwitchStates() {")
    assert pull_idx > 0, "_pullFreshSwitchStates method missing"
    # Cover the body of the method (≈600 chars window) to find the ids list
    pull_body = card_source[pull_idx : pull_idx + 1200]
    assert "this._entities.camera" in pull_body, (
        "_pullFreshSwitchStates ids list must include camera — drop it "
        "and the badge stays stale on Companion-App-resume."
    )
    # First-hass code path must trigger the pull.
    first_hass_idx = card_source.find("if (firstHass)")
    assert first_hass_idx > 0
    first_hass_body = card_source[first_hass_idx : first_hass_idx + 800]
    assert "_pullFreshSwitchStates" in first_hass_body, (
        "firstHass branch in `set hass()` must call _pullFreshSwitchStates "
        "so the initial mount has authoritative state — otherwise the "
        "stream badge can render with stale data on the first paint."
    )


def test_banner_uses_high_contrast_white(card_source: str) -> None:
    """The HLS-fallback info banner sits over the video and must stay
    readable. Earlier rev used dark blue text on a 10 %-blue tint
    (effectively unreadable on the black letterbox bars). White text
    on a dark semi-transparent backdrop is the iOS-style we settled
    on; pin it so a future style refactor can't silently regress."""
    css_idx = card_source.find(".ios-hls-banner {")
    assert css_idx > 0
    css_block = card_source[css_idx : css_idx + 800]
    assert "color: #fff" in css_block, (
        "Banner text must be white. Dark-blue-on-black gave 0 contrast "
        "on Cloudflare-tunnel mobile users (screenshot 2026-05-06)."
    )
    assert "position: absolute" in css_block, (
        "Banner must be absolute-positioned over the video, not in the "
        "natural flow — otherwise it lands on the fullscreen letterbox "
        "bars where the dark backdrop disappears entirely."
    )


def test_card_version_matches_const_py() -> None:
    """`CARD_VERSION` must be in lock-step between `const.py` and the
    card source so the auto-registered Lovelace resource URL changes
    on every release and browsers fetch the new file (HA serves
    www/ with max-age=31 days)."""
    src_text = CARD_SRC.read_text()
    const_text = CONST_PY.read_text()

    src_match = [
        l for l in src_text.splitlines() if l.startswith('const CARD_VERSION = "')
    ]
    const_match = [
        l for l in const_text.splitlines() if l.startswith('CARD_VERSION = "')
    ]
    assert len(src_match) == 1
    assert len(const_match) == 1
    src_ver = src_match[0].split('"')[1]
    const_ver = const_match[0].split('"')[1]
    assert src_ver == const_ver, (
        f"CARD_VERSION drift: src={src_ver}, const={const_ver}"
    )
