"""Regression tests for `switch._redact_rtsp_creds`.

The "Live stream active for … — <url>" INFO log used to write the full
`rtspsUrl`, which embeds the LOCAL proxy / Digest credentials in its userinfo
(``user:password@host``). HA logs are routinely pasted into forum bug reports,
so credentials must never reach the log. `_redact_rtsp_creds` replaces the
userinfo with ``***:***`` while keeping host/port/path/query for debugging.

Bug found live 2026-06-13 during the v13.5.15 watchdog (switch.py:385 logged
``rtsp://cbs-13494370:<pw>@127.0.0.1:33689/...`` at INFO). Pin every variant.
"""

from __future__ import annotations

import pytest

from custom_components.bosch_shc_camera.switch import _redact_rtsp_creds


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # LOCAL proxy URL with Digest userinfo — the actual leak.
        (
            "rtsp://cbs-13494370:N-pa%24sw0rd@127.0.0.1:33689/rtsp_tunnel?inst=1&enableaudio=1",
            "rtsp://***:***@127.0.0.1:33689/rtsp_tunnel?inst=1&enableaudio=1",
        ),
        # rtsps scheme (REMOTE) with creds.
        (
            "rtsps://user:secret@cloud.example.com:8554/path?token=abc",
            "rtsps://***:***@cloud.example.com:8554/path?token=abc",
        ),
        # Userinfo with no port.
        (
            "rtsp://u:p@10.0.0.5/stream",
            "rtsp://***:***@10.0.0.5/stream",
        ),
        # Username only (no colon) still gets masked.
        (
            "rtsp://justuser@host:554/s",
            "rtsp://***:***@host:554/s",
        ),
        # No credentials → returned unchanged.
        (
            "rtsp://127.0.0.1:33689/rtsp_tunnel?inst=1",
            "rtsp://127.0.0.1:33689/rtsp_tunnel?inst=1",
        ),
        # Empty string → empty string (matches result.get default).
        ("", ""),
    ],
)
def test_redact_rtsp_creds(url: str, expected: str) -> None:
    """No credentials survive; non-cred URLs and empties pass through."""
    redacted = _redact_rtsp_creds(url)
    assert redacted == expected
    # Defence in depth: the original userinfo must not appear anywhere.
    if "@" in url and ":" in url.split("@", 1)[0]:
        secret = url.split("://", 1)[1].split("@", 1)[0]
        assert secret not in redacted


class TestExtraStateAttributesRedaction:
    """extra_state_attributes must redact rtsps_url — HIGH-2 security fix."""

    def test_extra_state_attributes_redacts_creds(self) -> None:
        """rtspsUrl with LOCAL/Digest creds in _live_connections → attribute has ***:***."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        raw_url = "rtsp://cbs-AABBCCDD:N-pa%24sw0rd@127.0.0.1:33689/rtsp_tunnel?inst=1"
        coord = SimpleNamespace(
            _live_connections={
                "cam-1": {
                    "rtspsUrl": raw_url,
                    "_connection_type": "LOCAL",
                    "proxyUrl": "",
                }
            },
            _shc_state_cache={"cam-1": {}},
            options={},
        )
        switch = object.__new__(BoschLiveStreamSwitch)
        switch.coordinator = coord
        switch._cam_id = "cam-1"

        attrs = switch.extra_state_attributes
        assert "***:***" in attrs["rtsps_url"]
        assert "cbs-AABBCCDD" not in attrs["rtsps_url"]
        assert "N-pa%24sw0rd" not in attrs["rtsps_url"]

    def test_extra_state_attributes_empty_url_stays_empty(self) -> None:
        """Empty rtspsUrl (stream off) → empty string attribute (no crash)."""
        from types import SimpleNamespace

        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        coord = SimpleNamespace(
            _live_connections={
                "cam-1": {"rtspsUrl": "", "_connection_type": "REMOTE", "proxyUrl": ""}
            },
            _shc_state_cache={"cam-1": {}},
            options={},
        )
        switch = object.__new__(BoschLiveStreamSwitch)
        switch.coordinator = coord
        switch._cam_id = "cam-1"

        attrs = switch.extra_state_attributes
        assert attrs["rtsps_url"] == ""

    def test_extra_state_attributes_no_creds_passes_through(self) -> None:
        """REMOTE URL without userinfo → returned unchanged (no false redaction)."""
        from types import SimpleNamespace

        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        no_cred_url = "rtsps://cloud.boschsecurity.com:8554/stream/abc123"
        coord = SimpleNamespace(
            _live_connections={
                "cam-1": {
                    "rtspsUrl": no_cred_url,
                    "_connection_type": "REMOTE",
                    "proxyUrl": "",
                }
            },
            _shc_state_cache={"cam-1": {}},
            options={},
        )
        switch = object.__new__(BoschLiveStreamSwitch)
        switch.coordinator = coord
        switch._cam_id = "cam-1"

        attrs = switch.extra_state_attributes
        assert attrs["rtsps_url"] == no_cred_url
