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
