"""Regression tests for snapshot_fetchers.py — the one leaf-level pure
HTTP-fetch helper extracted out of coordinator.py's live/event snapshot
cascade (style-audit round, coordinator.py was 3,939 lines; see that
module's own docstring for why only this single piece was extracted —
everything else in the cascade reads/writes coordinator caches and
stays inline).

`fetch_digest_snapshot` takes no coordinator cache dependency at all
(only `coordinator.hass`, to obtain the shared client session) so these
tests pin its full behavior directly, mirroring the existing
`TestFetchDigestClosure`/`TestFetchLiveSnapshotLocalValueError` coverage
in tests/test_init.py for the coordinator's own
`async_fetch_live_snapshot_local` (which now delegates its terminal
Digest GET to this function) — those stay in place unchanged since they
exercise the coordinator's public method end-to-end (PUT /connection
LOCAL + this fetch), while this file pins the leaf in isolation.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.bosch_shc_camera import snapshot_fetchers

CAM_ID = "11111111-1111-1111-1111-111111111111"
SNAP_URL = "https://192.0.2.149:443/snap.jpg?JpegSize=1206"
MODULE = "custom_components.bosch_shc_camera"


def _coord() -> SimpleNamespace:
    return SimpleNamespace(hass=SimpleNamespace())


def _digest_cm(status: int, content_type: str = "image/jpeg", body: bytes = b""):
    """Build the async-context-manager mock `async_digest_request` returns."""
    resp = MagicMock()
    resp.status = status
    resp.headers = {"Content-Type": content_type}
    resp.read = AsyncMock(return_value=body)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


class TestFetchDigestSnapshot:
    @pytest.mark.asyncio
    async def test_200_image_returns_bytes(self) -> None:
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 20
        with (
            patch(
                "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                f"{MODULE}.async_digest_request",
                new=AsyncMock(return_value=_digest_cm(200, "image/jpeg", jpeg)),
            ),
        ):
            result = await snapshot_fetchers.fetch_digest_snapshot(
                _coord(), CAM_ID, SNAP_URL, "u", "p"
            )
        assert result == jpeg

    @pytest.mark.asyncio
    async def test_200_non_image_content_type_returns_none(self) -> None:
        with (
            patch(
                "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                f"{MODULE}.async_digest_request",
                new=AsyncMock(
                    return_value=_digest_cm(200, "text/html", b"<html></html>")
                ),
            ),
        ):
            result = await snapshot_fetchers.fetch_digest_snapshot(
                _coord(), CAM_ID, SNAP_URL, "u", "p"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_non_200_status_returns_none(self) -> None:
        with (
            patch(
                "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                f"{MODULE}.async_digest_request",
                new=AsyncMock(return_value=_digest_cm(401)),
            ),
        ):
            result = await snapshot_fetchers.fetch_digest_snapshot(
                _coord(), CAM_ID, SNAP_URL, "u", "p"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self) -> None:
        with (
            patch(
                "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                f"{MODULE}.async_digest_request",
                new=AsyncMock(side_effect=TimeoutError()),
            ),
        ):
            result = await snapshot_fetchers.fetch_digest_snapshot(
                _coord(), CAM_ID, SNAP_URL, "u", "p"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_client_error_returns_none(self) -> None:
        with (
            patch(
                "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                f"{MODULE}.async_digest_request",
                new=AsyncMock(side_effect=aiohttp.ClientConnectionError("boom")),
            ),
        ):
            result = await snapshot_fetchers.fetch_digest_snapshot(
                _coord(), CAM_ID, SNAP_URL, "u", "p"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_value_error_returns_none(self) -> None:
        """Malformed/missing WWW-Authenticate header (forum 998974/15, Andrew75)."""
        with (
            patch(
                "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                f"{MODULE}.async_digest_request",
                new=AsyncMock(
                    side_effect=ValueError(
                        "Server returned 401 without WWW-Authenticate header"
                    )
                ),
            ),
        ):
            result = await snapshot_fetchers.fetch_digest_snapshot(
                _coord(), CAM_ID, SNAP_URL, "u", "p"
            )
        assert result is None
