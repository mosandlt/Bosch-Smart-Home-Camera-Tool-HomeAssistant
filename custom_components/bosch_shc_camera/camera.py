"""Bosch Smart Home Camera — Camera Platform.

Each camera discovered via /v11/video_inputs becomes a HA camera entity.
Images are the latest motion-triggered event snapshots from the cloud API.

If a live proxy connection has been opened (via the "Open Live Stream" button
or the bosch_shc_camera.open_live_connection service), the entity exposes
a stream_source (rtsps:// URL on port 443) for full 30fps H.264 + AAC audio.

Stream URL format:
  rtsps://proxy-NN.live.cbs.boschsecurity.com:443/{hash}/rtsp_tunnel
    ?inst=2&enableaudio=1&fmtp=1&maxSessionDuration=3600

Note: HA's stream component must support rtsps:// (RTSP over TLS).
The stream requires -tls_verify 0 / insecure TLS (Bosch private CA).
If HA cannot open rtsps://, use ffplay from the Python CLI tool instead.

Stream session limit: Bosch enforces maxSessionDuration=3600 (60 minutes).
After 60 minutes the stream stops and must be restarted manually.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
import urllib3

import aiohttp

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, LIVE_SESSION_TTL, get_options, _is_safe_bosch_url, BoschCameraCoordinator  # type: ignore[attr-defined]
from .auth_utils import async_digest_request
from .const import TIMEOUT_SNAP, AUTO_PLAY_DEFAULT_VALUES
from .mjpeg_snapshot import fetch_mjpeg_snapshot
from .snapshot_store import load_snapshot, save_snapshot

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

IMAGE_REFRESH_INTERVAL  = 1800  # fallback: seconds between background proactive refreshes
CLOUD_SNAP_CACHE_TTL    = 30    # minimum seconds between cloud fetches (de-bounce)
DEFAULT_SNAPSHOT_INTERVAL = 1800 # default proactive background refresh interval (30 min)
IDLE_FRAME_INTERVAL     = 60    # seconds — how often HA's camera proxy calls async_camera_image


def _rotate_jpeg_180(jpeg_bytes: bytes) -> bytes:
    """Rotate a JPEG image by 180° using PIL. Sync — call via executor.

    Used by async_camera_image when the user enabled the Bild 180° drehen
    switch (ceiling-mounted indoor cameras). Typical 1280×720 JPEG: ~15-30 ms
    with libjpeg-turbo. Returns the original bytes if rotation fails.
    """
    try:
        from PIL import Image
        from io import BytesIO
        img = Image.open(BytesIO(jpeg_bytes))
        rotated = img.rotate(180)
        out = BytesIO()
        rotated.save(out, format="JPEG", quality=90)
        return out.getvalue()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("rotate_jpeg_180 failed (%s) — returning original", err)
        return jpeg_bytes


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up camera entities — one per discovered Bosch camera."""
    opts = get_options(config_entry)
    if not opts.get("enable_snapshots", True):
        _LOGGER.debug("Camera snapshots disabled in options — skipping camera platform")
        return

    coordinator = config_entry.runtime_data
    entities = [
        BoschCamera(coordinator, cam_id, config_entry)
        for cam_id in coordinator.data
    ]
    async_add_entities(entities, update_before_add=False)


class BoschCamera(CoordinatorEntity, Camera):  # type: ignore[misc]
    """Represents a single Bosch Smart Home camera in Home Assistant.

    • Shows the latest motion-triggered JPEG snapshot (refreshed every scan_interval)
    • Exposes stream_source (RTSP) once a live connection has been established
    • Device groups with sensor and button entities on the same HA device
    • Camera state is "streaming" when live proxy is active, "idle" otherwise
    • Image is refreshed on startup, on stream stop, and every 30 minutes
    """

    # 1×1 black JPEG — prevents HTTP 500 when no cached image available
    _PLACEHOLDER_JPEG = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xc4\x00\x14\x10\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xda\x00\x08\x01\x01\x00\x00?\x00T\xdf\xb2\x80\x01\xff\xd9'
    _attr_has_entity_name = True

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        # stream_options is set dynamically in stream_source() based on connection
        # type (LOCAL needs rtsp_transport=tcp; REMOTE uses FFmpeg default).

        self._cam_id = cam_id
        self._entry  = entry
        self._cached_image: bytes | None = self._PLACEHOLDER_JPEG
        self._force_image_refresh: bool = False  # bypasses HA image cache once
        self._last_image_fetch: float = -86400.0  # monotonic timestamp of last fetch (large-negative = never fetched)
        self._was_streaming: bool = False

        info = coordinator.data.get(cam_id, {}).get("info", {})
        title = info.get("title", cam_id)

        self._display_name   = f"Bosch {title}"
        self._attr_name      = None  # entity is the main feature of the device; HA uses device name as friendly_name
        self._attr_unique_id = f"bosch_shc_cam_{cam_id.lower()}"
        self._model = info.get("hardwareVersion", "CAMERA")
        self._hw_version = info.get("hardwareVersion", "")
        from .models import get_display_name
        self._model_name = get_display_name(self._hw_version)
        self._fw    = info.get("firmwareVersion", "")
        self._mac   = info.get("macAddress", "")

    # ── Startup ───────────────────────────────────────────────────────────────
    async def async_added_to_hass(self) -> None:
        """Called when entity is added to HA — kick off initial image fetch."""
        await super().async_added_to_hass()
        # Register with coordinator so button/service can trigger image refresh
        self.coordinator._camera_entities[self._cam_id] = self

        # Restore the last-persisted snapshot from disk so HA can serve a real
        # image immediately — before the first live fetch completes (~2–4 s).
        # This prevents the 1×1 black placeholder from flashing on a cold start.
        persisted = await load_snapshot(self.hass, self._cam_id)
        if persisted:
            self._cached_image = persisted
            # Back-date _last_image_fetch so the normal snapshot_interval still
            # triggers a live refresh on schedule.  Using float('-inf') would
            # trigger an immediate re-fetch; instead back-date by one full
            # snapshot_interval so the first refresh fires normally.
            from .const import DEFAULT_OPTIONS
            opts = get_options(self._entry)
            snap_interval = float(int(opts.get("snapshot_interval", DEFAULT_OPTIONS["snapshot_interval"])))
            self._last_image_fetch = time.monotonic() - snap_interval
            _LOGGER.debug(
                "%s: restored %d-byte snapshot from disk",
                self._display_name, len(persisted),
            )

        # Fetch a real image shortly after startup (let coordinator settle first).
        self.hass.async_create_task(self._async_trigger_image_refresh(delay=2))

    async def async_will_remove_from_hass(self) -> None:
        """Called when entity is removed — unregister from coordinator."""
        self.coordinator._camera_entities.pop(self._cam_id, None)
        await super().async_will_remove_from_hass()

    def _handle_coordinator_update(self) -> None:
        """Detect streaming → idle transitions and trigger background 30-min refresh."""
        is_now_streaming = self.is_streaming

        # Stream just stopped → grab a fresh event snapshot immediately
        if self._was_streaming and not is_now_streaming:
            self.hass.async_create_task(self._async_trigger_image_refresh(delay=2))

        # Proactive background refresh (even when nobody has the page open).
        # Interval: snapshot_interval option (default 1800 s / 30 min).
        elif not is_now_streaming:
            now = time.monotonic()
            opts = get_options(self._entry)
            proactive_interval = float(int(opts.get("snapshot_interval", IMAGE_REFRESH_INTERVAL)))
            if now - self._last_image_fetch >= proactive_interval:
                self.hass.async_create_task(self._async_trigger_image_refresh(delay=0))

        self._was_streaming = is_now_streaming
        super()._handle_coordinator_update()

    async def _async_trigger_image_refresh(self, delay: float = 0) -> None:
        """Fetch a fresh image and force HA's camera proxy to serve it.

        Primarily used on startup and after stream stop. For CAMERA_360 (whose
        REMOTE snap.jpg returns 401) this runs the LOCAL Digest-auth fallback so
        the camera cache stays warm even though async_camera_image's cloud fetch
        would return None for it.

        Sets _force_image_refresh=True so that frame_interval returns 0.1 s,
        causing HA's image cache to expire on the very next proxy request.
        After the fetch, frame_interval reverts to its normal value.
        """
        if delay:
            await asyncio.sleep(delay)

        # Skip refresh when privacy mode is ON — the camera blocks the view,
        # so any image we'd fetch would just be the stale last event snapshot.
        # The frontend card shows the "Privat-Modus aktiv" placeholder instead.
        shc = self.coordinator._shc_state_cache.get(self._cam_id, {})
        if shc.get("privacy_mode") is True:
            _LOGGER.debug("%s: skipping image refresh — privacy mode is ON", self._display_name)
            return

        self._force_image_refresh = True
        try:
            # Fast path: populate _cached_image from the latest event snapshot immediately
            # so the HA camera proxy can serve something while the live snap is fetching.
            # This ensures the card shows a real image within ~1s of startup/stream-stop,
            # instead of waiting 5-15s for the PUT /connection + snap.jpg round-trip.
            if not self._cached_image:
                quick = await self.async_camera_image()
                if quick:
                    self._cached_image = quick
                    self._last_image_fetch = time.monotonic()
                    _LOGGER.debug(
                        "%s: quick event-snapshot seed — %d bytes",
                        self._display_name, len(quick),
                    )
                    self.async_write_ha_state()

            # Slow path: fetch a fresh live snapshot via PUT /connection + snap.jpg
            # Skip when streaming — opening a new PUT /connection kills the active RTSP session
            image = None
            if not self.is_streaming:
                image = await self.coordinator.async_fetch_live_snapshot(self._cam_id)
                # Fallback for cameras whose REMOTE snap.jpg returns 401 (e.g. CAMERA_360):
                # try LOCAL connection with Digest auth for a direct LAN snapshot.
                if not image:
                    image = await self.coordinator.async_fetch_live_snapshot_local(self._cam_id)

            # Last resort: fetch fresh events from Bosch API and use the latest imageUrl.
            # Bypasses stale/expired coordinator-cached event URLs.
            # Skip when streaming — fetching events in streaming mode is unnecessary (the live
            # proxy snap.jpg already provides a current frame via async_camera_image path 1)
            # and would overwrite _cached_image with a stale event still, corrupting live frames.
            if not image and not self.is_streaming:
                image = await self.coordinator.async_fetch_fresh_event_snapshot(self._cam_id)

            if image:
                self._cached_image = image
                self._last_image_fetch = time.monotonic()
                _LOGGER.debug(
                    "%s: background refresh — %d bytes",
                    self._display_name, len(image),
                )
                self.async_write_ha_state()

                # Persist to disk (defence-in-depth: privacy gate at top of
                # this method already prevents reaching here when privacy is ON)
                shc_state = self.coordinator._shc_state_cache.get(self._cam_id, {})
                if not shc_state.get("privacy_mode"):
                    await save_snapshot(self.hass, self._cam_id, image)
                    img_entity = self.coordinator._image_entities.get(self._cam_id)
                    if img_entity is not None:
                        await img_entity.async_notify_refreshed()

        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: image refresh failed: %s", self._display_name, err)
        finally:
            self._force_image_refresh = False

    # ── Helpers ───────────────────────────────────────────────────────────────
    @property
    def _cam_data(self) -> dict[str, Any]:
        return self.coordinator.data.get(self._cam_id, {})  # type: ignore[no-any-return]

    # ── Streaming state ───────────────────────────────────────────────────────
    @property
    def is_streaming(self) -> bool:
        """True when a live proxy connection is active AND the RTSP URL is ready.

        Controls the HA camera state: True → "streaming", False → "idle".
        Returning True only when `rtspsUrl` is populated (not just when the
        coordinator's `_live_connections` entry exists) prevents a race that
        broke WebRTC on first stream-start: try_live_connection writes the
        cred-bearing result into `_live_connections` BEFORE pre-warm finishes
        (~25-35 s for Gen2), and only sets `rtspsUrl` after pre-warm completes.
        If `is_streaming` flipped to True at the first write, the camera card's
        `_waitForStreamReady` would observe `cam.state === "streaming"`,
        immediately fire `camera/webrtc/offer`, and HA's go2rtc provider would
        reject with `Camera has no stream source` (because `stream_source()`
        below also gates on `rtspsUrl`). The card then 5-s-timed-out and fell
        back to HLS — which is what made WebRTC look like "only works after a
        browser reload" (after a reload, pre-warm was already done and rtspsUrl
        was present from the first state read). Bug 2026-05-27 Innenbereich.
        """
        live = self.coordinator._live_connections.get(self._cam_id, {})
        if not live:
            return False
        return bool(live.get("rtspsUrl") or live.get("rtspUrl"))

    @property
    def is_recording(self) -> bool:
        return False

    @property
    def motion_detection_enabled(self) -> bool:
        """Whether motion detection is currently enabled on this camera.

        Reads from the same cloud API data as the Motion Detection switch.
        Enables the standard HA camera.enable/disable_motion_detection services.
        """
        settings = self.coordinator.motion_settings(self._cam_id)
        if not settings:
            return False
        return bool(settings.get("enabled", False))

    async def async_enable_motion_detection(self, **kwargs: Any) -> None:
        """Enable motion detection via standard HA camera service."""
        settings = self.coordinator.motion_settings(self._cam_id)
        sensitivity = settings.get("motionAlarmConfiguration", "HIGH") if settings else "HIGH"
        await self.coordinator.async_put_camera(
            self._cam_id, "motion",
            {"enabled": True, "motionAlarmConfiguration": sensitivity},
        )
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    async def async_disable_motion_detection(self, **kwargs: Any) -> None:
        """Disable motion detection via standard HA camera service."""
        settings = self.coordinator.motion_settings(self._cam_id)
        sensitivity = settings.get("motionAlarmConfiguration", "HIGH") if settings else "HIGH"
        await self.coordinator.async_put_camera(
            self._cam_id, "motion",
            {"enabled": False, "motionAlarmConfiguration": sensitivity},
        )
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    @property
    def frame_interval(self) -> float:
        """How often (seconds) HA requests a fresh image from this camera.

        When _force_image_refresh is set: 0.1 s — forces immediate cache expiry
        so HA's next proxy request fetches the new snapshot right away.
        When streaming: 1 s — must be shorter than the card's 2 s setInterval so
                        that every card poll triggers a fresh snap.jpg fetch. At 2 s,
                        browser setInterval jitter (±50 ms early) caused HA to return
                        cached frames → alternating 1 s / 3 s gaps instead of 2 s.
        When idle:      IDLE_FRAME_INTERVAL (60 s) — HA calls async_camera_image
                        every 60 s. The actual cloud fetch rate is governed by
                        CLOUD_SNAP_CACHE_TTL (30 s) inside async_camera_image:
                        stale cache → return cached immediately + bg refresh.
                        snapshot_interval (default 1800 s) controls the proactive
                        background refresh in _handle_coordinator_update, not this.
        """
        if self._force_image_refresh:
            return 0.1
        if self.is_streaming:
            return 1.0
        return float(IDLE_FRAME_INTERVAL)

    @property
    def _token(self) -> str:
        return self._entry.data.get("bearer_token", "")  # type: ignore[no-any-return]

    # ── HA metadata ───────────────────────────────────────────────────────────
    @property
    def brand(self) -> str:
        return "Bosch"

    @property
    def model(self) -> str:
        return self._model  # type: ignore[no-any-return]

    @property
    def available(self) -> bool:
        # Firmware install reboots the camera (3–7 min). Mark unavailable so
        # automations and the UI don't poll a dead endpoint or surface stale
        # snapshots as live state.
        is_updating = getattr(self.coordinator, "is_updating", None)
        if is_updating is not None and is_updating(self._cam_id):
            return False
        return bool(self.coordinator.last_update_success)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers  = {(DOMAIN, self._cam_id)},
            name         = self._display_name,
            manufacturer = "Bosch",
            model        = self._model_name,
            sw_version   = self._fw,
            connections  = {("mac", self._mac)} if self._mac else set(),
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cam_data = self._cam_data
        events   = cam_data.get("events", [])
        latest   = events[0] if events else {}
        live     = cam_data.get("live", {})
        rtsps_url = live.get("rtspsUrl", live.get("rtspUrl", ""))
        # Stream status for dashboard display
        fell_back = self.coordinator._stream_fell_back.get(self._cam_id, False)
        err_count = self.coordinator._stream_error_count.get(self._cam_id, 0)
        if self.coordinator.is_stream_warming(self._cam_id):
            stream_status = "warming_up"
        elif self.is_streaming:
            stream_status = "streaming (REMOTE fallback)" if fell_back else "streaming"
        elif self._cam_id in self.coordinator._live_connections:
            stream_status = "connecting"
        else:
            stream_status = "idle"

        info = cam_data.get("info", {})
        bosch_priority = info.get("priority")
        attrs = {
            "camera_id":       self._cam_id,
            "status":          cam_data.get("status", "UNKNOWN"),
            "stream_status":   stream_status,
            "streaming_state": "active" if self.is_streaming else "idle",
            "last_event":      latest.get("timestamp", "")[:19],
            "event_type":      latest.get("eventType", ""),
            "model_name":      self._model_name,
            "hardware_version": self._hw_version,
            "firmware":        self._fw,
            "mac":             self._mac,
            "live_rtsps":      rtsps_url,
            "live_proxy":      live.get("proxyUrl", ""),
            "stream_errors":   err_count,
            # Bosch-app camera order. Mirrors the float priority returned by
            # GET /v11/video_inputs (settable via PUT /v11/video_inputs/order).
            # The overview card reads this when `use_bosch_sort: true` so the
            # HA layout matches the Bosch app order.
            "bosch_priority":  bosch_priority,
        }
        if rtsps_url:
            attrs["stream_url"] = rtsps_url
        # bufferingTime from PUT /connection (LOCAL=500ms, REMOTE=1000ms)
        # — Bosch-server-side hint, NOT the player buffer. Display only.
        live_conn = self.coordinator._live_connections.get(self._cam_id, {})
        bt = live_conn.get("_bufferingTime")
        if bt is not None:
            attrs["buffering_time_ms"] = bt
            attrs["connection_type"] = live_conn.get("_connection_type", "REMOTE")
        # Player-side buffer profile — read by the Lovelace card to configure
        # hls.js. Mode → (liveSyncDurationCount, liveMaxLatencyDurationCount,
        # maxBufferLength, lowLatencyMode) is mapped client-side.
        attrs["live_buffer_mode"] = get_options(self._entry).get(
            "live_buffer_mode", "balanced"
        )
        # Card auto-play default — collapses any non-canonical value to "lan"
        # so a typo or stale option from a previous version never disables
        # stream start. Per-card YAML `auto_play` still overrides this.
        mode = get_options(self._entry).get("auto_play_default", "lan")
        attrs["auto_play_default"] = (
            mode if mode in AUTO_PLAY_DEFAULT_VALUES else "lan"
        )
        # Camera-side timestamp overlay (burned-in date/time, bottom-right of
        # the video frame). The card reads this to hide its own last-event
        # glass pill — otherwise the user sees two timestamps stacked, one
        # burned-in by the camera and one drawn by the card. Defensive
        # getattr covers test stubs that lack the cache.
        ts_cache = getattr(self.coordinator, "_timestamp_cache", None)
        if ts_cache is not None:
            ts_overlay = ts_cache.get(self._cam_id)
            if ts_overlay is not None:
                attrs["camera_timestamp_overlay"] = bool(ts_overlay)
        return attrs

    # ── Live stream ───────────────────────────────────────────────────────────
    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Close a WebRTC session — no-op for unknown / never-established sessions.

        HA's go2rtc provider stores sessions in a dict keyed by session_id and
        calls ``dict.pop(session_id)`` (without a default) in ``async_close_session``.
        When privacy mode is ON, ``async_handle_async_webrtc_offer`` calls
        ``_update_stream_source`` which raises HomeAssistantError (no stream source),
        so the session is *never inserted* into ``go2rtc._sessions``.  However the
        websocket handler already registered
        ``partial(camera.close_webrtc_session, session_id)`` as a subscription
        cleanup before the offer was even forwarded — so when the client
        disconnects, ``async_handle_close`` calls this method for a session_id that
        go2rtc never tracked, causing a KeyError that HA logs at ERROR level:

            "Error unsubscribing from subscription: functools.partial(
             <bound method Camera.close_webrtc_session …>, '<session_id>')"

        Fix: delegate to the base class (which calls provider.async_close_session)
        inside a try/except that silences KeyError.  All other exceptions (e.g.
        RuntimeError from a closed event-loop) still propagate so real bugs
        remain visible.

        Regression test: tests/test_close_webrtc_session.py
        """
        try:
            super().close_webrtc_session(session_id)
        except KeyError:
            # Session was never established (e.g. privacy mode was ON when the
            # WebRTC offer arrived and go2rtc bailed before inserting it into
            # its _sessions dict).  Silently discard — there is nothing to close.
            _LOGGER.debug(
                "%s: close_webrtc_session(%s) — session not found, already closed or "
                "never established (privacy mode?); ignoring",
                self._display_name, session_id,
            )

    async def async_create_stream(self) -> Any:
        """Auto-open live connection when play_stream / Cast is requested.

        HA calls this when camera.play_stream is invoked (e.g. Cast to Chromecast).
        Without the override stream_source() returns None with no active session
        → async_create_stream() returns None → HA logs
        "does not support play stream service" (observed 2026-05-09 19:02 CEST).

        When privacy mode is ON the live connection is intentionally blocked.
        We raise HomeAssistantError so HA surfaces a meaningful message instead of
        the generic "does not support play stream service".
        """
        if not self.coordinator._live_connections.get(self._cam_id):
            # Privacy mode gate: do not even attempt a live connection
            shc = self.coordinator._shc_state_cache.get(self._cam_id, {})
            if shc.get("privacy_mode") is True:
                raise HomeAssistantError(
                    f"{self._display_name}: stream unavailable — privacy mode is ON"
                )
            _LOGGER.debug("%s: play_stream — auto-opening live connection", self._display_name)
            result = await self.coordinator.try_live_connection(self._cam_id)
            if not result:
                _LOGGER.warning("%s: play_stream — live connection failed", self._display_name)
                return None
            self.coordinator.async_update_listeners()
        # Pre-warm race (observed 2026-05-17 05:16:14 UTC for bosch_innenbereich):
        # coordinator sets _live_connections[cam_id] BEFORE the LOCAL pre-warm
        # populates rtspsUrl. During that window stream_source() intentionally
        # returns None — but super().async_create_stream() reads stream_source()
        # and returns None too, which HA core surfaces as the misleading
        # "does not support play stream service". Wait for warming to clear
        # (rtspsUrl gets populated at the same point) so super() reads a valid URL.
        if self._cam_id in self.coordinator._stream_warming:
            cfg = self.coordinator.get_model_config(self._cam_id)
            deadline = time.monotonic() + cfg.min_total_wait + 5
            while self._cam_id in self.coordinator._stream_warming:
                if time.monotonic() > deadline:
                    _LOGGER.warning(
                        "%s: play_stream — pre-warm did not complete within %ds",
                        self._display_name, cfg.min_total_wait + 5,
                    )
                    return None
                await asyncio.sleep(0.5)
        return await super().async_create_stream()

    async def stream_source(self) -> str | None:
        """Return RTSP URL when a live connection has been opened.

        LOCAL streams use a local TLS proxy (rtsp://127.0.0.1:PORT/...) so
        FFmpeg can connect via plain TCP while the proxy handles TLS to the camera.
        REMOTE streams use rtsps:// directly (Bosch cloud proxy has valid certs).

        Returns None when no live session is active (switch is OFF).
        Always reads from _live_connections (real-time) instead of coordinator
        data cache to avoid stale URLs after session renewal or mode switch.
        """
        # Read from _live_connections (updated immediately) instead of
        # coordinator data cache (updated on next refresh cycle)
        live = self.coordinator._live_connections.get(self._cam_id, {})
        if not live:
            return None
        url: str | None = live.get("rtspsUrl") or live.get("rtspUrl") or None
        if not url:
            return None
        # LOCAL streams go through our TLS proxy (plain TCP → TLS). HA 2026.4 /
        # FFmpeg Lavf 62 rejects the UDP→TCP transport rewrite the proxy used to
        # do, so we force TCP interleaved on SETUP. REMOTE streams go directly to
        # the Bosch cloud proxy via rtsps:// and must use the FFmpeg default
        # (UDP) — forcing TCP on REMOTE breaks Gen1 Eyes Outdoor cloud streams.
        is_local = live.get("_connection_type") == "LOCAL"
        self.stream_options = {"rtsp_transport": "tcp"} if is_local else {}
        # Strip audio param if audio switch is OFF (default)
        if not self.coordinator._audio_enabled.get(self._cam_id, True):
            url = url.replace("&enableaudio=1", "").replace("enableaudio=1&", "")
        return url

    # ── RCP thumbnail fallback ────────────────────────────────────────────────
    def _yuv422_to_jpeg(self, data: bytes) -> bytes | None:
        """Convert a 320×180 YUV422 (YUYV) raw frame to JPEG bytes using numpy+Pillow."""
        try:
            import numpy as np
            from PIL import Image
            import io
            if len(data) != 320 * 180 * 2:
                return None
            # YUYV interleaved: Y0 U Y1 V per 4 bytes = 2 pixels
            raw = np.frombuffer(data, dtype=np.uint8).reshape(180, 320, 2)
            y = raw[:, :, 0].astype(np.float32)
            # U/V are at alternating positions in the second byte channel
            uv_plane = raw[:, :, 1].astype(np.float32)
            # U at even columns, V at odd columns
            u_half = uv_plane[:, 0::2]  # shape (180, 160)
            v_half = uv_plane[:, 1::2]  # shape (180, 160)
            u = np.repeat(u_half, 2, axis=1) - 128.0  # (180, 320)
            v = np.repeat(v_half, 2, axis=1) - 128.0  # (180, 320)
            r = np.clip(y + 1.402 * v, 0, 255).astype(np.uint8)
            g = np.clip(y - 0.344136 * u - 0.714136 * v, 0, 255).astype(np.uint8)
            b = np.clip(y + 1.772 * u, 0, 255).astype(np.uint8)
            rgb = np.stack([r, g, b], axis=2)
            img = Image.fromarray(rgb, mode='RGB')
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=85)
            return buf.getvalue()
        except Exception:
            return None

    async def _async_rcp_thumbnail(self) -> bytes | None:
        """Fetch a thumbnail via RCP — tries 320×180 JPEG (0x099e) first,
        falls back to 320×180 YUV422 raw frame (0x0c98) converted to JPEG.

        Resolution confirmed via RCP 0x0a88 READ (returns 0x00000140/0x000000B4 = 320×180).
        Uses the cached live proxy connection (if available) to reach the
        camera's RCP endpoint. Much faster than snap.jpg (~instant vs ~1.5 s)
        and used as a fallback when the proxy snap.jpg fetch fails.
        """
        live = self.coordinator._live_connections.get(self._cam_id, {})
        urls = live.get("urls", [])
        if not urls:
            return None

        # urls[0] = "proxy-NN.live.cbs.boschsecurity.com:42090/{hash}"
        parts = urls[0].split("/", 1)
        if len(parts) != 2:
            return None
        proxy_host = parts[0]
        proxy_hash = parts[1]

        session_id = await self.coordinator._get_cached_rcp_session(proxy_host, proxy_hash)
        if not session_id:
            return None

        rcp_base = f"https://{proxy_host}/{proxy_hash}/rcp.xml"

        # Try 320×180 JPEG via RCP 0x099e (resolution confirmed by 0x0a88 = 320×180)
        raw: bytes | None = await self.coordinator._rcp_read(rcp_base, "0x099e", session_id)
        if raw and raw[:2] == b"\xff\xd8":
            _LOGGER.debug(
                "%s: Using RCP thumbnail fallback (320×180) — %d bytes",
                self._display_name, len(raw),
            )
            return raw

        # Fallback: 320×180 YUV422 raw frame → convert to JPEG
        raw = await self.coordinator._rcp_read(rcp_base, "0x0c98", session_id)
        if raw and len(raw) == 115200:
            jpeg = self._yuv422_to_jpeg(raw)
            if jpeg:
                _LOGGER.debug(
                    "%s: Using RCP YUV422 fallback (320x180) — %d bytes → %d bytes JPEG",
                    self._display_name, len(raw), len(jpeg),
                )
                return jpeg
            _LOGGER.debug(
                "%s: RCP YUV422 conversion failed (0x0c98, %d bytes)",
                self._display_name, len(raw),
            )
        elif raw:
            _LOGGER.debug(
                "%s: RCP 0x0c98 unexpected size: %d bytes (expected 115200)",
                self._display_name, len(raw),
            )
        return None

    # ── Snapshot image ────────────────────────────────────────────────────────
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Public entrypoint — wraps the implementation so that any uncaught
        exception still returns a valid (placeholder) JPEG instead of letting
        HA's camera proxy serve a textual `500: Internal Server Error` body
        (26 bytes of plain text in place of an image).

        Observed 2026-04-27 on Gen1 cams during the pre-warm transition: while
        `_live_connections[cam_id]` had a partial entry but no proxyUrl yet,
        an unhandled exception path in `_async_camera_image_impl` propagated up
        and HA returned 500. Lovelace's `<img>` element rendered the literal
        text bytes as a brown error frame on every camera card sharing the
        same broken endpoint, making it look like cross-camera bleed.
        """
        try:
            result = await self._async_camera_image_impl(width, height)
            jpeg = result if result else self._PLACEHOLDER_JPEG
        except asyncio.CancelledError:
            raise  # let cancellation propagate cleanly
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "%s: async_camera_image failed (%s) — serving placeholder",
                self._display_name, err,
            )
            jpeg = self._cached_image or self._PLACEHOLDER_JPEG
        # Apply 180° rotation if the user enabled it via the Bild 180° drehen
        # switch (ceiling-mounted indoor cameras). Skip the placeholder JPEG.
        rotate = bool(getattr(self.coordinator, "_image_rotation_180", {}).get(self._cam_id))
        if rotate and jpeg is not self._PLACEHOLDER_JPEG and jpeg:
            jpeg = await self.hass.async_add_executor_job(_rotate_jpeg_180, jpeg)
        return jpeg

    async def _async_camera_image_impl(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """
        Return the best available JPEG snapshot, tried in order:

        0. MJPEG inst=3 LAN snapshot (Gen2 opt-in) — when use_mjpeg_snapshot is
           enabled: FFmpeg subprocess captures one frame from RTSP inst=3.
           Requires Gen2 hardware + cached LOCAL Digest credentials.
           ~150-300 ms on healthy LAN. Falls through on any error.
        1. Cloud proxy live snap  — if a live connection has been opened
           (proxy-NN.live.cbs.boschsecurity.com snap.jpg, no auth needed)
           Updated every coordinator tick while live switch is ON.
           1b. RCP thumbnail fallback — 320×180 JPEG via RCP 0x099e, used when
               snap.jpg fetch fails with any error (timeout, network, etc.)
        2. Cloud proxy on-demand  — PUT /connection REMOTE + RCP 0x099e / snap.jpg.
           If no cached image: fetches fresh synchronously (~3 s for snap.jpg,
           ~100 ms for RCP thumbnail when width <= 640).
           If cached image is older than CLOUD_SNAP_CACHE_TTL (30 s): fetches
           fresh synchronously so the user always sees a current image.
        3. Cached image           — fallback when cloud fetch fails (e.g. CAMERA_360
           whose REMOTE snap.jpg returns 401; refreshed via _async_trigger_image_refresh
           using LOCAL connection).
        4. Latest event snapshot  — last resort on very first startup before any
           cloud fetch has completed.

        The card calls trigger_snapshot on page load / tab switch / 60s timer,
        which sets _force_image_refresh=True (frame_interval → 0.1s) and fetches
        a fresh image via _async_trigger_image_refresh. This ensures HA's camera
        proxy serves the fresh image on the next request instead of its 60s cache.

        width/height: passed by HA when the card requests ?width=N. We use this to
        prefer the 320×180 RCP thumbnail on mobile/small displays (avoids 150 KB
        snap.jpg when the card only needs a 400 px thumbnail).
        """
        session = async_get_clientsession(self.hass, verify_ssl=False)
        token   = self._token
        headers_bearer = {"Authorization": f"Bearer {token}", "Accept": "*/*"}
        # True when card requests a mobile/thumbnail-sized image
        prefer_small = width is not None and width <= 640

        # ── 0. MJPEG inst=3 snapshot (Gen2 opt-in, LAN-only) ─────────────────
        # When use_mjpeg_snapshot is enabled: capture one JPEG frame directly
        # from the camera's RTSP inst=3 stream via FFmpeg subprocess.
        # Requires Gen2 hardware and valid LOCAL Digest credentials cached by
        # the coordinator from the most recent PUT /connection.
        # Estimated latency: ~150-300 ms on LAN (vs ~500-1500 ms cloud-proxy).
        # Falls through silently on any error — existing paths take over.
        opts = get_options(self._entry)
        if opts.get("use_mjpeg_snapshot", False):
            from .models import get_model_config
            model_cfg = get_model_config(self._hw_version)
            if model_cfg.generation >= 2:
                creds = self.coordinator._local_creds_cache.get(self._cam_id)
                # cbs creds rotate ~60 s after each PUT /connection without
                # heartbeat. Skip MJPEG when creds are stale — FFmpeg would
                # fail "Invalid data found when processing input" otherwise.
                # Threshold 45 s keeps a safety margin.
                creds_age = time.monotonic() - creds.get("ts", 0.0) if creds else float("inf")
                if creds and creds_age < 45.0:
                    _local_user_m = creds.get("user", "")
                    _local_pass_m = creds.get("password", "")
                    _local_host_m = creds.get("host", "")
                    _local_port_m = int(creds.get("port", 443))
                    if _local_user_m and _local_pass_m and _local_host_m:
                        mjpeg_data = await fetch_mjpeg_snapshot(
                            _local_host_m,
                            _local_port_m,
                            _local_user_m,
                            _local_pass_m,
                            timeout=float(opts.get("mjpeg_snapshot_timeout", TIMEOUT_SNAP)),
                        )
                        if mjpeg_data:
                            self._cached_image = mjpeg_data
                            self._last_image_fetch = time.monotonic()
                            return mjpeg_data

        # ── 1. Cloud proxy live snapshot (active live-stream session) ─────────
        live = self.coordinator._live_connections.get(self._cam_id, {})
        proxy_url = live.get("proxyUrl", "")
        if proxy_url:
            # LOCAL connection: snap.jpg requires HTTP Digest auth
            if live.get("_connection_type") == "LOCAL":
                local_user = live.get("_local_user", "")
                local_pass = live.get("_local_password", "")
                if local_user and local_pass:
                    data: bytes | None = None
                    try:
                        # Tightened from 12 s to 6 s: HA's CameraImageView wraps
                        # async_camera_image() with CAMERA_IMAGE_TIMEOUT (10 s);
                        # 12 s + 10 s aiohttp fallback below = >22 s, well over
                        # HA's outer timeout. HA cancels mid-flight → image=None
                        # → HomeAssistantError → 26-byte "500: Internal Server
                        # Error" body rendered as a brown placeholder on the
                        # camera card. 6 s is enough for a healthy LAN Digest
                        # round-trip; if it fails, return cached/placeholder
                        # immediately rather than racing HA's outer timeout.
                        async with asyncio.timeout(6):
                            async with await async_digest_request(
                                session, "GET", proxy_url, local_user, local_pass,
                                timeout=TIMEOUT_SNAP, ssl=False,
                            ) as resp:
                                if resp.status == 200 and "image" in resp.headers.get("Content-Type", ""):
                                    data = await resp.read()
                    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                        # ValueError: malformed/missing WWW-Authenticate header
                        # from auth_utils.async_digest_request — forum 998974/15.
                        _LOGGER.debug("LOCAL snap via proxy failed: %s", err)
                        data = None
                    if data:
                        self._cached_image = data
                        self._last_image_fetch = time.monotonic()
                        _LOGGER.debug(
                            "%s: LOCAL live snap %d bytes",
                            self._display_name, len(data),
                        )
                        return self._cached_image
                    # LOCAL conn: skip the aiohttp fallback below. The proxy_url
                    # for LOCAL is `https://<lan-ip>:443/snap.jpg` which requires
                    # the Digest auth we just tried — aiohttp without auth would
                    # 401 in another ~10 s burning HA's outer budget. Go straight
                    # to cached image / placeholder via the final return.
                    return self._cached_image or self._PLACEHOLDER_JPEG
            try:
                async with asyncio.timeout(10):
                    async with session.get(proxy_url) as resp:
                        ct = resp.headers.get("Content-Type", "")
                        if resp.status == 200 and "image" in ct:
                            data = await resp.read()
                            if data:
                                self._cached_image = data
                                self._last_image_fetch = time.monotonic()
                                _LOGGER.debug(
                                    "%s: live proxy snapshot %d bytes",
                                    self._display_name, len(self._cached_image),
                                )
                                return self._cached_image
                        elif resp.status == 404:
                            # 404 = proxy URL expired — re-request a fresh connection and retry
                            opened_at = self.coordinator._live_opened_at.get(self._cam_id, 0)
                            age = time.monotonic() - opened_at
                            _LOGGER.debug(
                                "%s: proxy snapshot 404 (age %.0fs) — proxy URL expired, refreshing connection",
                                self._display_name, age,
                            )
                            # Refresh the live connection so proxyUrl is current again
                            new_live = await self.coordinator.try_live_connection(self._cam_id)
                            if new_live:
                                new_proxy_url = new_live.get("proxyUrl", "")
                                if new_proxy_url:
                                    try:
                                        async with asyncio.timeout(10):
                                            async with session.get(new_proxy_url) as retry_resp:
                                                ct2 = retry_resp.headers.get("Content-Type", "")
                                                if retry_resp.status == 200 and "image" in ct2:
                                                    data = await retry_resp.read()
                                                    if data:
                                                        self._cached_image = data
                                                        self._last_image_fetch = time.monotonic()
                                                        return self._cached_image
                                    except (asyncio.TimeoutError, aiohttp.ClientError):
                                        pass
                        elif resp.status in (401, 403):
                            opened_at = self.coordinator._live_opened_at.get(self._cam_id, 0)
                            age = time.monotonic() - opened_at
                            if age >= LIVE_SESSION_TTL:
                                # Proxy hash expired — renew the session (same as 404 path).
                                # Do NOT clear _live_connections: clearing makes is_streaming=False
                                # which stops the card display ("disabled livestream").
                                _LOGGER.debug(
                                    "%s: proxy snapshot %d (age %.0fs) — session expired, renewing connection",
                                    self._display_name, resp.status, age,
                                )
                                new_live = await self.coordinator.try_live_connection(self._cam_id)
                                if new_live:
                                    new_proxy_url = new_live.get("proxyUrl", "")
                                    if new_proxy_url:
                                        try:
                                            async with asyncio.timeout(10):
                                                async with session.get(new_proxy_url) as retry_resp:
                                                    ct2 = retry_resp.headers.get("Content-Type", "")
                                                    if retry_resp.status == 200 and "image" in ct2:
                                                        data = await retry_resp.read()
                                                        if data:
                                                            self._cached_image = data
                                                            self._last_image_fetch = time.monotonic()
                                                            return self._cached_image
                                        except (asyncio.TimeoutError, aiohttp.ClientError):
                                            pass
                                else:
                                    # Renewal failed — clear so is_streaming goes to False cleanly
                                    _LOGGER.debug(
                                        "%s: session renewal failed — clearing", self._display_name
                                    )
                                    self.coordinator._live_connections.pop(self._cam_id, None)
                                    self.coordinator._live_opened_at.pop(self._cam_id, None)
                            else:
                                _LOGGER.debug(
                                    "%s: proxy snapshot %d (age %.0fs) — keeping session (camera requires auth for snap.jpg)",
                                    self._display_name, resp.status, age,
                                )
            except (asyncio.TimeoutError, aiohttp.ClientError):
                # Any network/timeout error on the live proxy snap.jpg — try RCP thumbnail
                rcp_thumb = await self._async_rcp_thumbnail()
                if rcp_thumb:
                    self._cached_image = rcp_thumb
                    self._last_image_fetch = time.monotonic()
                    return self._cached_image

        # ── 2. Cloud proxy on-demand snapshot (PUT /connection REMOTE → snap.jpg) ──
        # Primary snapshot method for idle cameras. Two modes:
        #
        # a) No cached image yet (first load / cache empty): fetch synchronously so
        #    HA has something to serve immediately. ~3s on cold cache.
        #
        # b) Cached image exists but is stale (> CLOUD_SNAP_CACHE_TTL): fetch fresh
        #    synchronously so the user always sees a current image. The card triggers
        #    this via trigger_snapshot service which sets _force_image_refresh, so
        #    HA's frame_interval cache is bypassed and the fresh image is served.
        #
        # Skip when streaming — opening a new PUT /connection kills the active RTSP session.
        if not self.is_streaming:
            now = time.monotonic()
            cache_stale = (now - self._last_image_fetch) >= CLOUD_SNAP_CACHE_TTL
            if not self._cached_image:
                # First load — must wait synchronously.
                # For mobile/thumbnail requests (width ≤ 640): try RCP 0x099e first
                # (320×180 JPEG, ~3 KB, ~100 ms with cached session) before the slow
                # full proxy path (PUT /connection + snap.jpg, ~3 s cold).
                if prefer_small:
                    rcp_img = await self._async_rcp_thumbnail()
                    if rcp_img:
                        self._cached_image = rcp_img
                        self._last_image_fetch = now
                        _LOGGER.debug(
                            "%s: RCP thumbnail (first load, prefer_small) — %d bytes",
                            self._display_name, len(rcp_img),
                        )
                        return rcp_img
                fresh: bytes | None = await self.coordinator.async_fetch_live_snapshot(self._cam_id)
                if not fresh:
                    # REMOTE snap.jpg returns 401 on CAMERA_360 — try LOCAL Digest fallback
                    fresh = await self.coordinator.async_fetch_live_snapshot_local(self._cam_id)
                if fresh:
                    self._cached_image = fresh
                    self._last_image_fetch = now
                    _LOGGER.debug(
                        "%s: cloud proxy snapshot %d bytes (first load)",
                        self._display_name, len(fresh),
                    )
                    return fresh
            elif cache_stale:
                cache_age = now - self._last_image_fetch
                # Always fetch fresh synchronously when cache is stale.
                # The old background-refresh approach returned the stale image
                # and refreshed async — but HA's frame_interval meant the fresh
                # image was never served until the NEXT cycle, so the user saw
                # the same stale frame repeatedly.
                _LOGGER.debug(
                    "%s: cache stale (%ds) — fetching fresh synchronously",
                    self._display_name, int(cache_age),
                )
                if prefer_small:
                    rcp_img = await self._async_rcp_thumbnail()
                    if rcp_img:
                        self._cached_image = rcp_img
                        self._last_image_fetch = now
                        return rcp_img
                fresh2: bytes | None = await self.coordinator.async_fetch_live_snapshot(self._cam_id)
                if not fresh2:
                    # REMOTE snap.jpg returns 401 on CAMERA_360 — try LOCAL Digest fallback
                    fresh2 = await self.coordinator.async_fetch_live_snapshot_local(self._cam_id)
                if fresh2:
                    self._cached_image = fresh2
                    self._last_image_fetch = now
                    return fresh2
                # Both REMOTE + LOCAL failed — advance timestamp so next tick retries instead of looping
                self._last_image_fetch = now
                _LOGGER.debug(
                    "%s: fresh fetch failed — returning cached (%ds old)",
                    self._display_name, int(cache_age),
                )
                return self._cached_image
            else:
                return self._cached_image

        # ── 2b. LOCAL snap.jpg with cached Digest creds (cloud-outage fallback) ──
        # When the Bosch cloud or auth server is unreachable, PUT /connection
        # REMOTE fails — but we may still have valid LOCAL creds from the
        # previous session (cached in coordinator._local_creds_cache). Try
        # fetching snap.jpg directly from the camera's LAN IP using those
        # creds before giving up. Digest creds are ephemeral (camera rotates
        # them on reboot) but usually stable for minutes to hours.
        creds = self.coordinator._local_creds_cache.get(self._cam_id)
        if creds and self.coordinator._auth_outage_count > 0:
            local_user = creds.get("user", "")
            local_pass = creds.get("password", "")
            host = creds.get("host", "")
            port = creds.get("port", 443)
            if local_user and local_pass and host:
                snap_url = f"https://{host}:{port}/snap.jpg?JpegSize=1206"
                outage_data: bytes | None = None
                try:
                    async with asyncio.timeout(12):
                        async with await async_digest_request(
                            session, "GET", snap_url, local_user, local_pass,
                            timeout=TIMEOUT_SNAP, ssl=False,
                        ) as resp:
                            if resp.status == 200 and "image" in resp.headers.get("Content-Type", ""):
                                outage_data = await resp.read()
                except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                    _LOGGER.debug("LOCAL outage snap failed: %s", err)
                    outage_data = None
                if outage_data:
                    self._cached_image = outage_data
                    self._last_image_fetch = time.monotonic()
                    _LOGGER.info(
                        "%s: outage fallback — LOCAL snap.jpg %d bytes via cached Digest creds",
                        self._display_name, len(outage_data),
                    )
                    return self._cached_image

        # ── 3. Cached image (fallback for cameras whose REMOTE snap.jpg needs auth) ──
        # For cameras like CAMERA_360 the cloud fetch above returns None;
        # _async_trigger_image_refresh keeps this cache warm via LOCAL connection.
        if self._cached_image:
            return self._cached_image

        # ── 4. Latest event snapshot (last resort — first startup before cloud fetch runs) ──
        events = self._cam_data.get("events", [])
        for ev in events:
            img_url = ev.get("imageUrl")
            if not img_url:
                continue
            if not _is_safe_bosch_url(img_url):
                _LOGGER.warning("Unsafe imageUrl rejected: %s", img_url[:60])
                continue
            try:
                async with asyncio.timeout(20):
                    async with session.get(img_url, headers=headers_bearer) as resp:
                        if resp.status == 200:
                            self._cached_image = await resp.read()
                            self._last_image_fetch = time.monotonic()
                            _LOGGER.debug(
                                "%s: event snapshot %d bytes @ %s",
                                self._display_name,
                                len(self._cached_image),
                                ev.get("timestamp", "")[:19],
                            )
                            return self._cached_image
                        elif resp.status == 401:
                            _LOGGER.warning(
                                "%s: token expired — update via integration options",
                                self._display_name,
                            )
                            return self._cached_image
                        else:
                            # e.g. 403/404/410 = expired URL — try next event
                            _LOGGER.debug(
                                "%s: event snapshot HTTP %d @ %s — trying next",
                                self._display_name,
                                resp.status,
                                ev.get("timestamp", "")[:19],
                            )
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.debug("%s: event snapshot error: %s", self._display_name, err)

        # Return last cached image if all methods failed
        return self._cached_image or self._PLACEHOLDER_JPEG
