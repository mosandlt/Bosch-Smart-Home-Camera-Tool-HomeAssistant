"""Leaf-level HTTP-fetch helper(s) for the live/event snapshot cascade.

This module intentionally holds only the smallest, genuinely pure piece
of the snapshot-fetch cascade in `coordinator.py`
(`async_fetch_live_snapshot`/`_async_fetch_live_snapshot_impl`/
`async_fetch_fresh_event_snapshot`/`async_fetch_live_snapshot_local` —
that cascade is the most business-critical path in this integration,
every camera entity's image comes through it).

Style-audit round (2026-08-05) evaluated extracting all four methods but
found the tiered fallback orchestration itself (proxy-URL caching,
RCP 0x099e fast-path probing + its own failure-memoization cache,
404-retry, privacy-state-drift detection + refresh scheduling,
concurrent-fetch coalescing via `_fresh_snap_cache`/`_snapshot_fetch_locks`/
`_fresh_snap_locks`, `local_creds_cache` population) is inseparable from
coordinator state read *and* write at nearly every step — exactly the
class of logic CLAUDE.md's LOADING_MODEL/quality-audit plan says must
stay on the coordinator. Only ONE self-contained "make this one HTTP
call, interpret the response, return bytes or None" leaf was found with
zero coordinator-cache reads or writes: the final Digest-authenticated
`snap.jpg` GET at the end of `async_fetch_live_snapshot_local`, after
the PUT /connection LOCAL credentials have already been obtained and
cached by the caller. That leaf is `fetch_digest_snapshot` below.

The PUT /connection LOCAL step immediately before it was deliberately
NOT extracted even though it looks similarly self-contained: dozens of
existing regression tests (`tests/test_init.py`, `TestFetchDigestClosure`
+ `TestFetchLiveSnapshotLocalValueError`) patch
`custom_components.bosch_shc_camera.coordinator.async_bosch_cloud_session_cm`
— a coordinator-submodule-qualified patch target. Moving that call out
of `coordinator.py` would silently stop intercepting it in every one of
those tests (the lesson `AGENTS_FOR_CONTEXT`/prior-round retros call out
explicitly: grep the whole suite for `patch(".*coordinator\\.<symbol>"`
before moving a symbol) — not worth the risk for a leaf this small.

`BoschCameraCoordinator` keeps a thin delegating call site for this
function (same call semantics, not a same-name method — `fetch_digest_snapshot`
was never a coordinator method itself, just inline code) so
`async_fetch_live_snapshot_local`'s public behavior is unchanged for
every existing caller (`camera.py`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)


async def fetch_digest_snapshot(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    snap_url: str,
    user: str,
    password: str,
) -> bytes | None:
    """Fetch ``snap.jpg`` from a camera's LAN endpoint via HTTP Digest auth.

    Pure leaf HTTP fetch — no coordinator cache read or write. The caller
    (`async_fetch_live_snapshot_local`) has already opened the PUT
    /connection LOCAL session, validated the resulting host, and cached
    the credentials in `local_creds_cache` before calling this.

    Uses HA's shared client session (``verify_ssl=False`` — LOCAL camera
    endpoints use per-device self-signed certs, documented exception in
    `cloud_ssl.py`) and `auth_utils.async_digest_request` for
    non-blocking Digest auth.
    """
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    # Local import (not top-level): keeps unittest.mock.patch(
    # "custom_components.bosch_shc_camera.async_digest_request", ...)
    # working the same way it did when this lived inline on the
    # coordinator — matches the rcp_client.py/live_connection.py pattern.
    from . import (
        async_digest_request as async_digest_request,
    )

    session = async_get_clientsession(coordinator.hass, verify_ssl=False)
    try:
        async with asyncio.timeout(12):
            async with await async_digest_request(
                session,
                "GET",
                snap_url,
                user,
                password,
                timeout=10.0,
                ssl=False,
            ) as resp:
                if resp.status == 200 and "image" in resp.headers.get(
                    "Content-Type", ""
                ):
                    content: bytes = await resp.read()
                    _LOGGER.debug(
                        "fetch_live_snapshot_local: %s → %d bytes via Digest",
                        cam_id,
                        len(content),
                    )
                    return content
                _LOGGER.debug(
                    "fetch_live_snapshot_local: Digest snap.jpg → HTTP %d for %s",
                    resp.status,
                    cam_id,
                )
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        # ValueError: malformed/missing WWW-Authenticate (cam Digest state
        # may be half-rotated during FCM flap). Forum 998974/15 (Andrew75).
        _LOGGER.debug(
            "fetch_live_snapshot_local: aiohttp error for %s: %s", cam_id, err
        )
    return None
