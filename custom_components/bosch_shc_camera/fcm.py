"""FCM push notifications and alert routing for Bosch Smart Home Camera.

Extracted from __init__.py to keep the coordinator lean.
All functions that previously used `self` now take a `coordinator` parameter.

Handles:
  - Firebase Cloud Messaging registration + listening
  - Bosch CBS device token registration
  - 3-step alert pipeline (text -> snapshot -> video clip)
  - Per-type notification routing (information/screenshot/video/system)
  - Event mark-as-read on Bosch cloud
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

import aiohttp
from urllib.parse import urlparse

from homeassistant.helpers.aiohttp_client import async_get_clientsession


# ── URL allowlist for image/video downloads (SSRF prevention) ────────────────
_SAFE_DOMAINS = frozenset({".boschsecurity.com", ".bosch.com"})


def _is_safe_bosch_url(url: str) -> bool:
    """Validate that a URL points to a known Bosch domain (HTTPS only)."""
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and any(parsed.hostname.endswith(d) for d in _SAFE_DOMAINS)
    )

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CLOUD_API = "https://residential.cbs.boschsecurity.com"


class _FCMNoiseFilter(logging.Filter):
    """Tame the firebase_messaging FCM client log noise during WAN outages.

    When the WAN drops (router reboot, ISP blip), `firebase_messaging`'s
    `_listen` loop crashes on `await reader.readexactly(1)` and re-enters
    itself recursively while retrying — every ERROR log line carries a
    ~3000-frame stack trace. With a 30 s reconnect cadence that produces
    ~200 log lines/s, 12 k+ lines/min, and an HA CPU spike from ~30 % to
    ~85 % until WAN comes back. Library has no way to suppress the trace
    (issue sdb9696/firebase-messaging#33 covers the abort-on-error angle
    but not the recursive trace).

    Filter strategy:
      1. Strip `exc_info` from the record so the formatter doesn't dump
         the recursive stack — the plain message is enough to know the
         FCM connection failed.
      2. De-duplicate: at most one pass-through per 60 s window so the
         log has a heartbeat marker without flooding.
    """

    def __init__(self):
        super().__init__()
        self._last_passed = 0.0  # monotonic ts of last record we let through

    def filter(self, record: logging.LogRecord) -> bool:
        # Only target the noisy "Unexpected exception during read" record;
        # other firebase_messaging logs (INFO start/stop, registration) pass
        # through untouched so we keep diagnostic visibility.
        msg = record.getMessage() if hasattr(record, "getMessage") else str(record.msg)
        if "Unexpected exception during read" not in msg:
            return True
        # Drop the multi-thousand-line traceback unconditionally — the
        # message itself is the diagnostic, the trace is library-internal
        # recursion that doesn't help triage.
        record.exc_info = None
        record.exc_text = None
        # Then de-dupe: 1 line per 60 s.
        now = time.monotonic()
        if (now - self._last_passed) < 60.0:
            return False
        self._last_passed = now
        return True


def _install_fcm_noise_filter() -> None:
    """Install the noise filter on the firebase_messaging logger once.

    Idempotent: re-running attaches no duplicate filters.
    """
    fcm_logger = logging.getLogger("firebase_messaging.fcmpushclient")
    for f in fcm_logger.filters:
        if isinstance(f, _FCMNoiseFilter):
            return
    fcm_logger.addFilter(_FCMNoiseFilter())

# Firebase Cloud Messaging — push notifications from Bosch CBS
FCM_SENDER_ID = "404630424405"
FCM_IOS_APP_ID = "1:404630424405:ios:715aae2570e39faad9bddc"


# ── Firebase config ──────────────────────────────────────────────────────────

async def fetch_firebase_config(hass: HomeAssistant) -> dict:
    """Return Firebase config for the Bosch Smart Camera app.

    These are public app-level identifiers embedded in every copy of the
    Bosch Smart Camera APK — they identify the app to Firebase, not the user.
    The API key is restricted by Firebase project rules (not by secrecy).
    """
    project_id = "bosch-smart-cameras"
    app_id = f"1:{FCM_SENDER_ID}:android:9e5b6b58e4c70075"
    import base64
    # Official OSS key from Bosch (Sebastian Raff, 2026-04-20) — Firebase/FCM permissions confirmed.
    _k = base64.b64decode("QUl6YVN5Q0toaGZ4ZlRzMUc3V3Z6VERBaU8wQWlzN0VIMjVEYk9z").decode()
    return {
        "project_id": project_id,
        "app_id": app_id,
        "api_key": _k,
    }


# ── FCM start / stop ────────────────────────────────────────────────────────

async def async_start_fcm_push(coordinator) -> None:
    """Start the FCM push listener for near-instant motion/audio event detection.

    Flow:
      1. Register with Google FCM (get a device token)
      2. Register the token with Bosch CBS (POST /v11/devices)
      3. Listen for silent push notifications from Bosch
      4. On push -> immediately fetch events -> fire HA events + update sensors

    FCM credentials are stored in the config entry data and reused across restarts.
    The push is a silent wake-up signal (no payload) — event data comes from /v11/events.
    """
    if coordinator._fcm_running:
        return
    if not coordinator.options.get("enable_fcm_push", False):
        _LOGGER.debug("FCM push disabled in options")
        return

    try:
        from firebase_messaging import FcmPushClient, FcmRegisterConfig
    except ImportError:
        _LOGGER.warning("firebase-messaging not installed — FCM push disabled")
        return

    # FcmPushClientConfig landed in firebase-messaging 0.4; guard defensively
    # so older installs still start (without the hardening).
    try:
        from firebase_messaging import FcmPushClientConfig
    except ImportError:  # pragma: no cover — 0.4+ ships this symbol
        FcmPushClientConfig = None

    # Determine push mode
    push_mode = coordinator.options.get("fcm_push_mode", "auto")

    # Build FCM config based on mode
    async def _build_fcm_cfg(mode: str) -> dict:
        """Return FCM config dict for the given mode (android or ios)."""
        if mode == "ios":
            import base64
            return {
                "project_id": "bosch-smart-cameras",
                "app_id": FCM_IOS_APP_ID,
                "api_key": base64.b64decode("QUl6YVN5QmxyN1o0ZmpaM0lmcnhsN1VRZFE4eGZRd3g5WFJBYnBJ").decode(),
            }
        else:
            # Android mode — use stored config or fetch from Firebase
            cfg = coordinator._entry.data.get("fcm_config") or {}
            if not cfg:
                cfg = await fetch_firebase_config(coordinator.hass)
                if cfg:
                    coordinator.hass.config_entries.async_update_entry(
                        coordinator._entry,
                        data={**coordinator._entry.data, "fcm_config": cfg},
                    )
            return cfg

    async def _try_fcm_with_mode(mode: str) -> bool:
        """Attempt FCM registration and start with the given mode. Returns True on success."""
        fcm_cfg = await _build_fcm_cfg(mode)
        if not fcm_cfg.get("api_key"):
            _LOGGER.warning("FCM: could not obtain Firebase config for mode '%s'", mode)
            return False

        fcm_config = FcmRegisterConfig(
            project_id=fcm_cfg["project_id"],
            app_id=fcm_cfg["app_id"],
            api_key=fcm_cfg["api_key"],
            messaging_sender_id=FCM_SENDER_ID,
        )

        # Load saved FCM credentials from config entry (survives HA restarts)
        saved_fcm_creds = coordinator._entry.data.get("fcm_credentials")

        def _on_creds_updated(creds):
            """Save FCM credentials to config entry for persistence.

            WHY threadsafe: this callback fires from the FCM client's own
            thread (Firebase SDK), not from the HA event loop. Calling
            `async_update_entry` directly from a foreign thread corrupts
            HA's internal state. `call_soon_threadsafe` hops back onto
            the loop before scheduling the async task.
            """
            def _persist():
                coordinator.hass.async_create_task(
                    _async_persist_fcm_creds(coordinator, creds)
                )
            coordinator.hass.loop.call_soon_threadsafe(_persist)

        def _on_push(notification: dict, persistent_id: str, obj=None) -> None:
            """Called when a push notification arrives from Bosch CBS."""
            _on_fcm_push(coordinator, notification, persistent_id, obj)

        # v10.3.22: harden against firebase-messaging#33. Default config aborts
        # the listener after 3 sequential CONNECTION errors (e.g. WAN blip) and
        # never reconnects — the client goes silent, our sensor keeps reporting
        # "fcm_push" while no pushes arrive. Passing None disables the abort;
        # library handles normal reconnect. Coordinator-tick watchdog below
        # (__init__.py) flips _fcm_healthy=False if no push in 1h, so the
        # dashboard sensor still shows the degraded state.
        fcm_kwargs = {
            "callback": _on_push,
            "fcm_config": fcm_config,
            "credentials": saved_fcm_creds,
            "credentials_updated_callback": _on_creds_updated,
        }
        if FcmPushClientConfig is not None:
            fcm_kwargs["config"] = FcmPushClientConfig(
                abort_on_sequential_error_count=None,
            )
        coordinator._fcm_client = FcmPushClient(**fcm_kwargs)

        try:
            coordinator._fcm_token = await coordinator._fcm_client.checkin_or_register()
            _LOGGER.debug("FCM registered (mode=%s) — token: %s...", mode, coordinator._fcm_token[:8])
        except Exception as err:
            _LOGGER.warning("FCM registration failed (mode=%s): %s", mode, err)
            coordinator._fcm_client = None
            return False

        # Register FCM token with Bosch CBS API
        await register_fcm_with_bosch(coordinator)

        # Start listening for pushes
        try:
            await coordinator._fcm_client.start()
            with coordinator._fcm_lock:
                coordinator._fcm_running = True
                coordinator._fcm_healthy = True
                coordinator._fcm_push_mode = mode
            _LOGGER.info("FCM push listener started (mode=%s) — near-instant event detection active", mode)
            return True
        except Exception as err:
            _LOGGER.warning("FCM push listener failed to start (mode=%s): %s", mode, err)
            with coordinator._fcm_lock:
                coordinator._fcm_client = None
            return False

    # Install once before any FCM client is created so the very first WAN
    # outage doesn't spam 12 k+ recursive-traceback lines at us.
    _install_fcm_noise_filter()

    if push_mode == "polling":
        _LOGGER.info("FCM push mode set to 'polling' — using standard API polling only")
        return
    elif push_mode == "auto":
        # Try iOS first, fall back to Android, then polling
        if not await _try_fcm_with_mode("ios"):
            _LOGGER.info("FCM auto mode: iOS failed, trying Android fallback")
            if not await _try_fcm_with_mode("android"):
                _LOGGER.warning("FCM auto mode: both iOS and Android failed — falling back to standard polling")
    elif push_mode in ("android", "ios"):
        await _try_fcm_with_mode(push_mode)
    else:
        _LOGGER.warning("FCM: unknown push mode '%s' — defaulting to ios", push_mode)
        await _try_fcm_with_mode("ios")


async def register_fcm_with_bosch(coordinator) -> bool:
    """Register our FCM token with Bosch CBS so it sends us push notifications.

    Endpoint: POST /v11/devices {"deviceType": "ANDROID"|"IOS", "deviceToken": token}
    Response: HTTP 204 on success.
    deviceType must match the FCM platform used for registration.
    """
    if not coordinator._fcm_token or not coordinator.token:
        return False

    # Determine device type from active push mode
    device_type = "IOS" if coordinator._fcm_push_mode == "ios" else "ANDROID"

    session = async_get_clientsession(coordinator.hass, verify_ssl=False)
    headers = {
        "Authorization": f"Bearer {coordinator.token}",
        "Content-Type":  "application/json",
    }
    body = {"deviceType": device_type, "deviceToken": coordinator._fcm_token}

    try:
        async with asyncio.timeout(10):
            async with session.post(
                f"{CLOUD_API}/v11/devices", headers=headers, json=body
            ) as resp:
                if resp.status in (200, 201, 204):
                    _LOGGER.info("FCM token registered with Bosch CBS (HTTP %d)", resp.status)
                    return True
                _LOGGER.warning(
                    "FCM token registration failed: HTTP %d", resp.status
                )
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.warning("FCM token registration error: %s", err)
    return False


async def async_stop_fcm_push(coordinator) -> None:
    """Stop the FCM push listener."""
    with coordinator._fcm_lock:
        client = coordinator._fcm_client
        running = coordinator._fcm_running
    if client and running:
        try:
            await client.stop()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug("FCM stop raised: %s", err)
        with coordinator._fcm_lock:
            coordinator._fcm_running = False
            coordinator._fcm_healthy = False
            coordinator._fcm_client = None
            coordinator._fcm_push_mode = "unknown"
        _LOGGER.info("FCM push listener stopped")


async def _async_persist_fcm_creds(coordinator, creds: dict) -> None:
    """Write FCM credentials into the config entry (must run in event loop)."""
    try:
        coordinator.hass.config_entries.async_update_entry(
            coordinator._entry,
            data={**coordinator._entry.data, "fcm_credentials": creds},
        )
        _LOGGER.debug("FCM credentials saved to config entry")
    except Exception as err:
        _LOGGER.debug("FCM creds persist failed: %s", err)


# ── FCM push callback ───────────────────────────────────────────────────────

def _on_fcm_push(coordinator, notification: dict, persistent_id: str, obj=None) -> None:
    """Called when a push notification arrives from Bosch CBS.

    The push is a silent wake-up signal with no event payload.
    We immediately trigger an event fetch + snapshot refresh for all cameras.
    """
    with coordinator._fcm_lock:
        # Drop pushes that arrive after async_stop_fcm_push cleared the client —
        # a trailing push would otherwise reschedule async_handle_fcm_push on a
        # loop that already considers FCM down.
        if not coordinator._fcm_running:
            return
        coordinator._fcm_last_push = time.monotonic()
        coordinator._fcm_healthy = True
    _LOGGER.info(
        "FCM push received (id=%s, from=%s) — fetching events",
        persistent_id, notification.get("from", "?"),
    )
    # Schedule immediate event fetch + snapshot refresh on the HA event loop
    coordinator.hass.loop.call_soon_threadsafe(
        coordinator.hass.async_create_task,
        async_handle_fcm_push(coordinator),
    )


async def async_handle_fcm_push(coordinator) -> None:
    """Handle an FCM push — fetch fresh events for all cameras and fire HA events."""
    token = coordinator.token
    if not token:
        return

    session = async_get_clientsession(coordinator.hass, verify_ssl=False)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    for cam_id in list(coordinator.data.keys()):
        try:
            url = f"{CLOUD_API}/v11/events?videoInputId={cam_id}&limit=5"
            async with asyncio.timeout(10):
                async with session.get(url, headers=headers) as r:
                    if r.status != 200:
                        continue
                    events = await r.json()

            if not events:
                continue

            newest_id = events[0].get("id", "")
            prev_id   = coordinator._last_event_ids.get(cam_id)

            # Per-event-ID dedup: concurrent FCM handlers (Bosch sometimes
            # sends two pushes ~10 s apart for the same event) otherwise both
            # pass the prev_id check and fire two alert chains.
            import time as _time
            _now = _time.monotonic()
            _sent = coordinator._alert_sent_ids
            if newest_id and _sent.get(newest_id, 0.0) > _now - 60.0:
                _LOGGER.debug(
                    "FCM push dedup: skipping duplicate alert for %s id=%s (already sent %.1fs ago)",
                    cam_id, newest_id[:8], _now - _sent[newest_id],
                )
                continue
            # Evict entries older than 120s on every call. Original
            # `if len(_sent) > 32` guard could starve eviction during
            # burst-event scenarios (4 cams × dense events all within
            # 120 s window → cache grows past 32 but eviction loop finds
            # nothing to evict, so it grows unbounded). Plain age-based
            # cleanup on every call has O(len) cost which is fine — len
            # stays small.
            if _sent:
                for _k in [k for k, v in _sent.items() if v < _now - 120.0]:
                    _sent.pop(_k, None)

            if prev_id is not None and newest_id and newest_id != prev_id:
                # Record alert dispatch ASAP so a concurrent handler sees it
                _sent[newest_id] = _now
                # Update last event ID FIRST to prevent polling from
                # detecting the same event and sending duplicate alerts
                coordinator._last_event_ids[cam_id] = newest_id

                newest_event = events[0]
                event_type   = newest_event.get("eventType", "")
                event_tags   = newest_event.get("eventTags", []) or []
                cam_name     = coordinator.data.get(cam_id, {}).get("info", {}).get("title", cam_id)

                # Gen2 cameras (Outdoor II w/ DualRadar, Indoor II) send
                # eventType=MOVEMENT with eventTags=["PERSON"] when a human is
                # detected — the tag is more specific than the type, so upgrade.
                # Confirmed 2026-04-11 via /v11/events on Terrasse: 15x tags=['PERSON'].
                if "PERSON" in event_tags and event_type == "MOVEMENT":
                    event_type = "PERSON"

                _LOGGER.info(
                    "FCM push -> new %s event for %s (id=%s, tags=%s)",
                    event_type, cam_name, newest_id[:8], event_tags,
                )

                # Update cached events (next coordinator tick rebuilds data[]).
                coordinator._cached_events[cam_id] = events
                # Mirror into coordinator.data so the windowed binary sensors
                # (motion/person/audio in binary_sensor.py) see the new event
                # immediately on the async_update_listeners() call below —
                # otherwise data[] is only refreshed on the next tick (up to
                # scan_interval seconds away), by which time the event may be
                # outside EVENT_ACTIVE_WINDOW and the sensor stays OFF.
                if cam_id in coordinator.data:
                    coordinator.data[cam_id]["events"] = events

                # Fire HA event bus
                event_payload = {
                    "camera_id":   cam_id,
                    "camera_name": cam_name,
                    "timestamp":   newest_event.get("timestamp", ""),
                    "image_url":   newest_event.get("imageUrl", ""),
                    "event_id":    newest_id,
                    "source":      "fcm_push",
                }
                if event_type == "MOVEMENT":
                    coordinator.hass.bus.async_fire("bosch_shc_camera_motion", event_payload)
                elif event_type == "AUDIO_ALARM":
                    coordinator.hass.bus.async_fire("bosch_shc_camera_audio_alarm", event_payload)
                elif event_type == "PERSON":
                    coordinator.hass.bus.async_fire("bosch_shc_camera_person", event_payload)

                # Check notification switches before sending alert.
                # Master switch (switch.bosch_{name}_notifications) must be ON,
                # AND the type-specific switch must be ON for this event type.
                _alert_blocked = False
                _base = cam_name.lower().replace(" ", "_").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
                _master_eid = f"switch.bosch_{_base}_notifications"
                _master_state = coordinator.hass.states.get(_master_eid)
                if _master_state and _master_state.state == "off":
                    _LOGGER.debug("Alert suppressed: %s is OFF", _master_eid)
                    _alert_blocked = True
                # Type-specific check
                # Map raw event types to the notification-switch slug used by
                # BoschNotificationTypeSwitch (switch.bosch_{base}_{slug}_notifications).
                # TROUBLE_CONNECT + TROUBLE_DISCONNECT both follow the `trouble` switch —
                # they're system events and can be silenced together without affecting
                # motion/person alerts.
                _type_map = {
                    "MOVEMENT":           "movement",
                    "PERSON":             "person",
                    "AUDIO_ALARM":        "audio",
                    "CAMERA_ALARM":       "camera_alarm",
                    "TROUBLE":            "trouble",
                    "TROUBLE_CONNECT":    "trouble",
                    "TROUBLE_DISCONNECT": "trouble",
                }
                _type_key = _type_map.get(event_type)
                if _type_key and not _alert_blocked:
                    _type_eid = f"switch.bosch_{_base}_{_type_key}_notifications"
                    _type_state = coordinator.hass.states.get(_type_eid)
                    if _type_state and _type_state.state == "off":
                        _LOGGER.debug("Alert suppressed: %s is OFF", _type_eid)
                        _alert_blocked = True

                if not _alert_blocked:
                    # Send alert notification (3-step: text + snapshot + video)
                    coordinator.hass.async_create_task(
                        async_send_alert(
                            coordinator,
                            cam_name, event_type,
                            newest_event.get("timestamp", ""),
                            newest_event.get("imageUrl", ""),
                            newest_event.get("videoClipUrl", ""),
                            newest_event.get("videoClipUploadStatus", ""),
                        )
                    )
                else:
                    _LOGGER.info("Alert skipped for %s (%s) — notifications disabled", cam_name, event_type)

                # Trigger snapshot refresh.
                # WHY tracked: fire-and-forget tasks get GC-collected on
                # HA shutdown mid-flight, leaving half-written temp files.
                # Keeping a strong reference + cleanup on done lets
                # async_unload_entry cancel+await them cleanly.
                cam_entity = coordinator._camera_entities.get(cam_id)
                if cam_entity:
                    task = coordinator.hass.async_create_task(
                        cam_entity._async_trigger_image_refresh(delay=2)
                    )
                    coordinator._bg_tasks.add(task)
                    task.add_done_callback(coordinator._bg_tasks.discard)

                # Notify all entity listeners
                coordinator.async_update_listeners()

                # Mark new event as read on the Bosch cloud (gated by user option)
                if coordinator.options.get("mark_events_read", False):
                    try:
                        await async_mark_events_read(coordinator, [newest_id])
                    except Exception:
                        pass

            elif newest_id:
                coordinator._last_event_ids[cam_id] = newest_id

        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("FCM push event fetch network error for %s: %s", cam_id, err)
        except Exception as err:
            _LOGGER.debug("FCM push event fetch error for %s: %s", cam_id, err)


# ── Alert routing helpers ────────────────────────────────────────────────────

def get_alert_services(coordinator, type_key: str) -> list[str]:
    """Return notify services for a given alert type key.

    "system" and "information" fall back to alert_notify_service when empty.
    "screenshot" and "video" do NOT fall back — empty means skip that step.
    type_key: "system" | "information" | "screenshot" | "video"
    """
    opts = coordinator.options
    raw = opts.get(f"alert_notify_{type_key}", "").strip()
    if not raw and type_key not in ("screenshot", "video"):
        raw = opts.get("alert_notify_service", "").strip()
    return [s.strip() for s in raw.split(",") if s.strip()]


def build_notify_data(
    svc: str, message: str, file_path: str | None = None, title: str | None = None,
) -> dict:
    """Build notify service call data with correct attachment format per service type.

    mobile_app (iOS + Android HA Companion): image served from /local/bosch_alerts/
    telegram_bot: uses photo field
    All others (Signal, email, ...): file path in data.attachments
    """
    data: dict = {"message": message}
    if title:
        data["title"] = title
    if not file_path:
        return data
    fname = os.path.basename(file_path)
    if "mobile_app" in svc:
        # HA Companion App — image URL served without auth from /config/www/
        # Files deleted within seconds when alert_delete_after_send=True
        notify_data: dict = {
            "image": f"/local/bosch_alerts/{fname}",
            "push": {"sound": "default"},  # iOS: play sound; Android ignores this key
        }
        data["data"] = notify_data
    elif "telegram" in svc.lower():
        data["data"] = {"photo": file_path, "caption": message}
    else:
        # Signal, email, generic — local file path attachment
        data["data"] = {"attachments": [file_path]}
    return data


def _write_file(path: str, data: bytes) -> None:
    """Write binary data to a file (runs in executor)."""
    with open(path, "wb") as f:
        f.write(data)


# ── 3-step alert pipeline ───────────────────────────────────────────────────

async def async_send_alert(
    coordinator,
    cam_name: str, event_type: str, timestamp: str,
    image_url: str, clip_url: str = "", clip_status: str = "",
) -> None:
    """Send a 3-step alert: instant text, snapshot image, video clip.

    Step 1: Immediate text notification (no delay)
    Step 2: Download snapshot from Bosch cloud (after 5s), send with image
    Step 3: Download video clip (after 15s total), send as attachment
    """
    from .smb import sync_smb_upload, sync_local_save

    opts = coordinator.options

    # Per-type service routing: information/screenshot/video each fall back to alert_notify_service.
    # TROUBLE events use "system" — check that before bailing on missing information services.
    _is_trouble = event_type in ("TROUBLE_CONNECT", "TROUBLE_DISCONNECT")
    info_svcs = get_alert_services(coordinator, "information")
    if not info_svcs and not _is_trouble:
        return  # Nothing to send if no information services configured

    save_snapshots = opts.get("alert_save_snapshots", False)
    delete_after   = opts.get("alert_delete_after_send", True)
    ts_short       = timestamp[11:19] if len(timestamp) >= 19 else timestamp

    # Event type → German label + emoji icon.
    # Derived from full mitmproxy capture analysis (116K+ events across 12 captures,
    # 2026-04-11): 5 unique (eventType, eventTags) combinations observed.
    # Key finding: PERSON events are eventType=MOVEMENT + eventTags=["PERSON"] (Gen2
    # DualRadar) — the caller is expected to have already upgraded event_type from
    # "MOVEMENT" to "PERSON" when tag is present (see __init__.py + fcm.py push path).
    type_label = {
        "MOVEMENT":           "Bewegung",
        "PERSON":             "Person erkannt",
        "AUDIO_ALARM":        "Audio-Alarm",
        "TROUBLE_CONNECT":    "Verbindung hergestellt",
        "TROUBLE_DISCONNECT": "Verbindung getrennt",
        "CAMERA_ALARM":       "Kamera-Alarm",
    }.get(event_type, event_type)
    type_icon = {
        "MOVEMENT":           "\U0001f4f7",   # 📷
        "PERSON":             "\U0001f9d1",   # 🧑
        "AUDIO_ALARM":        "\U0001f50a",   # 🔊
        "TROUBLE_CONNECT":    "\U0001f7e2",   # 🟢
        "TROUBLE_DISCONNECT": "\U0001f534",   # 🔴
        "CAMERA_ALARM":       "\U0001f6a8",   # 🚨
    }.get(event_type, "\u26a0\ufe0f")       # ⚠️ fallback

    # www/bosch_alerts/ is served as /local/bosch_alerts/ — needed for mobile_app notifications
    alert_dir = os.path.join(coordinator.hass.config.config_dir, "www", "bosch_alerts")
    await coordinator.hass.async_add_executor_job(os.makedirs, alert_dir, 0o755, True)
    ts_safe = timestamp[:19].replace(":", "-").replace("T", "_")
    session = async_get_clientsession(coordinator.hass, verify_ssl=False)
    headers = {"Authorization": f"Bearer {coordinator.token}", "Accept": "*/*"}
    files_to_cleanup: list[str] = []

    async def _notify_type(type_key: str, message: str, file_path: str | None = None) -> None:
        """Send to services configured for this alert type (information/screenshot/video)."""
        for svc in get_alert_services(coordinator, type_key):
            try:
                domain, service = svc.split(".", 1)
                call_data = build_notify_data(svc, message, file_path)
                await coordinator.hass.services.async_call(domain, service, call_data)
            except Exception as err:
                _LOGGER.warning("Alert send failed for %s (%s): %s", svc, type_key, err)

    # -- Step 1: Instant text alert ----------------------------------------
    # TROUBLE_CONNECT/DISCONNECT are connectivity events — route to "system",
    # not "information", and skip snapshot/clip steps (no media for these).
    _step1_key = "system" if _is_trouble else "information"
    try:
        await _notify_type(_step1_key, f"{type_icon} {cam_name}: {type_label} ({ts_short})")
        _LOGGER.debug("Alert step 1 (text) sent via %s", _step1_key)
    except Exception as err:
        _LOGGER.warning("Alert step 1 failed: %s", err)
        return

    if _is_trouble:
        return  # No snapshot/clip for connectivity events

    # -- Step 2: Snapshot image (after 3s, retries up to ~25s) ------------
    # The FCM push sometimes arrives before Bosch's event API has the imageUrl
    # populated. Single re-fetch at 5s missed slow-cloud events (observed
    # 2026-04-26: text alert sent, snapshot silently skipped, JPG only
    # appeared 90s later via the SMB upload path). Retry at +3 / +10 / +25 s
    # cumulative — covers steady-state cloud and warm-up cases without
    # delaying the common path noticeably.
    if not image_url:
        events_url = f"{CLOUD_API}/v11/events?videoInputId=&limit=5"
        for cid, cdata in coordinator.data.items():
            if cdata.get("info", {}).get("title", "") == cam_name:
                events_url = f"{CLOUD_API}/v11/events?videoInputId={cid}&limit=5"
                break
        for attempt, delay in enumerate((3, 7, 15), start=1):
            await asyncio.sleep(delay)
            try:
                async with asyncio.timeout(10):
                    async with session.get(events_url, headers=headers) as r:
                        if r.status == 200:
                            fresh_events = await r.json()
                            if fresh_events:
                                image_url = fresh_events[0].get("imageUrl", "")
                                clip_url = fresh_events[0].get("videoClipUrl", "") or clip_url
                                clip_status = fresh_events[0].get("videoClipUploadStatus", "") or clip_status
            except Exception as err:
                _LOGGER.debug("Alert: re-fetch attempt %d failed: %s", attempt, err)
                continue
            if image_url:
                _LOGGER.debug("Alert: re-fetched image_url on attempt %d", attempt)
                break
        if not image_url:
            _LOGGER.debug("Alert: image_url still empty after 3 retries — skipping step 2")

    if image_url:
        if not _is_safe_bosch_url(image_url):
            _LOGGER.warning("Alert: unsafe imageUrl rejected: %s", image_url[:60])
            image_url = ""
        else:
            await asyncio.sleep(5)
        snap_path = os.path.join(alert_dir, f"{cam_name}_{ts_safe}_{event_type}.jpg")
        try:
            async with asyncio.timeout(15):
                async with session.get(image_url, headers=headers) as resp:
                    if resp.status == 200 and "image" in resp.headers.get("Content-Type", ""):
                        data = await resp.read()
                        if data:
                            await coordinator.hass.async_add_executor_job(_write_file, snap_path, data)
                            await _notify_type(
                                "screenshot",
                                f"\U0001f4f8 {cam_name} Snapshot ({ts_short})",
                                snap_path,
                            )
                            _LOGGER.debug("Alert step 2 (screenshot) sent: %s", snap_path)
                            if not save_snapshots:
                                files_to_cleanup.append(snap_path)
        except Exception as err:
            _LOGGER.warning("Alert step 2 failed: %s", err)

    # -- Step 3: Video clip — poll until ready, then download + send -------
    # Bosch uploads clips asynchronously. The event initially has
    # clip_status=Pending (or no clipUrl at all). We poll the events API
    # every 10s for up to 90s until videoClipUploadStatus=Done.
    cam_id = None
    for cid, cdata in coordinator.data.items():
        if cdata.get("info", {}).get("title", "") == cam_name:
            cam_id = cid
            break

    if cam_id:
        clip_path = os.path.join(alert_dir, f"{cam_name}_{ts_safe}_{event_type}.mp4")
        auth_headers = {"Authorization": f"Bearer {coordinator.token}", "Accept": "application/json"}
        found_clip_url = clip_url if (clip_url and clip_status == "Done") else ""

        # Try direct clip.mp4 download first (faster than polling)
        if not found_clip_url:
            event_id = coordinator._last_event_ids.get(cam_id, "")
            if event_id:
                try:
                    async with asyncio.timeout(10):
                        async with session.get(
                            f"{CLOUD_API}/v11/events/{event_id}/clip.mp4",
                            headers={"Authorization": f"Bearer {coordinator.token}", "Accept": "*/*"},
                        ) as r:
                            if r.status == 200 and "video" in r.headers.get("Content-Type", ""):
                                found_clip_url = f"{CLOUD_API}/v11/events/{event_id}/clip.mp4"
                                _LOGGER.debug("Alert: direct clip.mp4 available for %s", cam_name)
                except Exception:
                    pass

        if not found_clip_url and clip_status == "Unavailable":
            _LOGGER.debug(
                "Alert: clip status Unavailable from start — skipping poll for %s", cam_name,
            )
        elif not found_clip_url:
            # Poll for clip readiness (10s intervals, up to 90s)
            clip_unavailable = False
            for attempt in range(9):
                await asyncio.sleep(10)
                try:
                    async with asyncio.timeout(10):
                        async with session.get(
                            f"{CLOUD_API}/v11/events?videoInputId={cam_id}&limit=3",
                            headers=auth_headers,
                        ) as r:
                            if r.status != 200:
                                continue
                            fresh = await r.json()
                            for ev in fresh:
                                if ev.get("timestamp", "")[:19] == timestamp[:19]:
                                    status = ev.get("videoClipUploadStatus", "")
                                    url = ev.get("videoClipUrl", "")
                                    if status == "Done" and url:
                                        found_clip_url = url
                                    elif status == "Unavailable":
                                        clip_unavailable = True
                                        _LOGGER.debug(
                                            "Alert: clip Unavailable after %ds — stop polling for %s",
                                            (attempt + 1) * 10, cam_name,
                                        )
                                    break
                    if found_clip_url:
                        _LOGGER.debug(
                            "Alert: clip ready after %ds for %s",
                            (attempt + 1) * 10, cam_name,
                        )
                        break
                    if clip_unavailable:
                        break
                except Exception:
                    continue

        if found_clip_url and _is_safe_bosch_url(found_clip_url):
            try:
                dl_headers = {"Authorization": f"Bearer {coordinator.token}", "Accept": "*/*"}
                async with asyncio.timeout(60):
                    async with session.get(found_clip_url, headers=dl_headers) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            if data and len(data) > 1000:
                                await coordinator.hass.async_add_executor_job(
                                    _write_file, clip_path, data
                                )
                                size_kb = len(data) // 1024
                                await _notify_type(
                                    "video",
                                    f"\U0001f3ac {cam_name} Video ({ts_short}, {size_kb} KB)",
                                    clip_path,
                                )
                                _LOGGER.info(
                                    "Alert step 3 (video) sent: %s (%d KB)", clip_path, size_kb
                                )
                                if not save_snapshots:
                                    files_to_cleanup.append(clip_path)
            except Exception as err:
                _LOGGER.warning("Alert step 3 (video) failed: %s", err)
        else:
            _LOGGER.debug("Alert: video clip not ready after 90s for %s", cam_name)

    # -- Mark event as read ------------------------------------------------
    if cam_id and coordinator.options.get("mark_events_read", False):
        event_id = coordinator._last_event_ids.get(cam_id, "")
        if event_id:
            try:
                await async_mark_events_read(coordinator, [event_id])
            except Exception:
                pass

    # -- SMB upload (immediate, alongside alert) ---------------------------
    if opts.get("enable_smb_upload") and opts.get("smb_server") and cam_id:
        try:
            # Build a minimal data dict for sync_smb_upload with just this event
            ev_id = coordinator._last_event_ids.get(cam_id, "unknown")
            ev_data = {
                "timestamp": timestamp,
                "eventType": event_type,
                "id": ev_id,
                "imageUrl": image_url,
                "videoClipUrl": found_clip_url if found_clip_url else "",
                "videoClipUploadStatus": "Done" if found_clip_url else "",
            }
            smb_data = {
                cam_id: {
                    "info": {"title": cam_name},
                    "events": [ev_data],
                }
            }
            _LOGGER.info(
                "Alert: SMB upload starting for %s (event=%s, img=%s, clip=%s)",
                cam_name, ev_id[:8] if ev_id else "?",
                bool(image_url), bool(found_clip_url),
            )
            await asyncio.wait_for(
                    coordinator.hass.async_add_executor_job(
                        sync_smb_upload, coordinator, smb_data, coordinator.token
                    ),
                    timeout=30.0,
                )
            _LOGGER.info("Alert: SMB upload completed for %s", cam_name)
        except asyncio.TimeoutError:
            _LOGGER.warning("Alert: SMB upload timed out after 30s for %s", cam_name)
        except Exception as err:
            _LOGGER.warning("Alert: SMB upload failed for %s: %s", cam_name, err)

    # -- Local save (FCM-triggered, alongside SMB) -------------------------
    if opts.get("download_path") and cam_id:
        try:
            ev_id = coordinator._last_event_ids.get(cam_id, "unknown")
            ev_data = {
                "timestamp": timestamp,
                "eventType": event_type,
                "id": ev_id,
                "imageUrl": image_url,
                "videoClipUrl": found_clip_url if found_clip_url else "",
                "videoClipUploadStatus": "Done" if found_clip_url else "",
            }
            await asyncio.wait_for(
                coordinator.hass.async_add_executor_job(
                    sync_local_save, coordinator, ev_data, coordinator.token, cam_name
                ),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            _LOGGER.warning("Alert: local save timed out after 30s for %s", cam_name)
        except Exception as err:
            _LOGGER.warning("Alert: local save failed for %s: %s", cam_name, err)

    # -- Cleanup local files -----------------------------------------------
    if delete_after and files_to_cleanup:
        await asyncio.sleep(5)  # give Signal time to read the files
        for fpath in files_to_cleanup:
            try:
                await coordinator.hass.async_add_executor_job(os.remove, fpath)
            except OSError:
                pass


# ── Mark events as read ──────────────────────────────────────────────────────

async def async_mark_events_read(coordinator, event_ids: list[str]) -> bool:
    """Mark events as read/seen on the Bosch cloud via PUT /v11/events.

    The /v11/events/bulk endpoint only supports `{ids, action: "DELETE"}` —
    there is no bulk mark-as-read. Best-effort — never raises.
    """
    if not event_ids:
        return True

    token = coordinator.token
    if not token:
        return False

    session = async_get_clientsession(coordinator.hass, verify_ssl=False)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    success = False
    for eid in event_ids:
        try:
            async with asyncio.timeout(5):
                async with session.put(
                    f"{CLOUD_API}/v11/events",
                    headers=headers, json={"id": eid, "isRead": True},
                ) as resp:
                    if resp.status in (200, 201, 204):
                        success = True
        except Exception:
            pass

    if success:
        _LOGGER.debug("Marked %d events as read", len(event_ids))
    return success
