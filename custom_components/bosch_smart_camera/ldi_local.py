"""Local-only video source over the camera's local data interface.

When a camera reports the local data interface as active and the user stored
its password, the camera is read over its LAN address (RTSP over TLS on port
9554, `rtsp_tunnel?line=1&inst=<N>&enableaudio=<0|1>`) and never touches the
Bosch cloud: no token check, no PUT /connection, no REMOTE fallback. `inst` 1
is the high and 2 the low stream (the camera's quality select), 3 a 1 Hz JPEG
preview used for snapshots; audio is always requested, as on the cloud path
(the audio switch only mutes the card). go2rtc is the single upstream reader;
the live view, the Mini-NVR recorder and the external-recorder endpoint all
read its local restream (see ldi_go2rtc.py), because the camera serves only a
few concurrent sessions. Without go2rtc there is no stream. Failure leaves
the camera without a stream until the next local attempt.

Reachability, the password check and the privacy state come from the local
REST interface (ldi_rest.py), polled at most once a minute per camera. A few
settings shown by entities are read there too (every two minutes); writes
stay on the cloud.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from . import ldi_go2rtc, ldi_rest
from .local_data_interface import STATE_ACTIVE, firmware_supports_ldi

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)

LDI_RTSP_PORT = 9554
LDI_USER = "localuser"
LDI_PASSWORDS_OPTION = "local_passwords"
LDI_STREAM_PATH = "/rtsp_tunnel"
LDI_INST_HIGH = 1
LDI_INST_LOW = 2
LDI_INST_PREVIEW = 3
VARIANT_LOW = "low"
VARIANT_SNAP = "snap"
LDI_REST_POLL_SEC = 60.0
_LDI_VERSION_POLL_SEC = 3600.0
_LDI_VERSION_RETRIES = 3
LDI_SETTINGS_POLL_SEC = 120.0
LDI_SETTINGS_MAX_AGE_SEC = 300.0
# The camera may lag behind a cloud write; its own value is ignored meanwhile.
LDI_WRITE_HOLD_SEC = 30.0
_LDI_MISSING_RECHECK_SEC = 3600.0

# Cloud endpoint written -> local settings that write changes.
_CLOUD_WRITE_SETTINGS: dict[str, tuple[str, ...]] = {
    "ledlights": (ldi_rest.SETTING_STATUS_LED,),
    "recording_options": (ldi_rest.SETTING_RECORD_SOUND,),
    "audioDetectionConfig": (
        ldi_rest.SETTING_GLASS_BREAK,
        ldi_rest.SETTING_FIRE_ALARM,
    ),
    "intrusionDetectionConfig": (
        ldi_rest.SETTING_INTRUSION_ENABLED,
        ldi_rest.SETTING_INTRUSION_SENSITIVITY,
        ldi_rest.SETTING_INTRUSION_DISTANCE,
    ),
    "alarm_settings": (
        ldi_rest.SETTING_ALARM_ENABLE,
        ldi_rest.SETTING_PRE_ALARM_ENABLE,
        ldi_rest.SETTING_ALARM_DELAY,
        ldi_rest.SETTING_ALARM_ACTIVATION_DELAY,
        ldi_rest.SETTING_PRE_ALARM_DELAY,
    ),
    "privacy_sound_override": (ldi_rest.SETTING_PRIVACY_SOUND,),
    "iconLedBrightness": (ldi_rest.SETTING_ICON_LED_BRIGHTNESS,),
}

_STALE_STREAM_STOP_TIMEOUT = 5
_PROBE_ATTEMPTS = 2
_PROBE_RETRY_WAIT = 2


RESULT_OK = "ok"
RESULT_PRIVACY = "privacy"
RESULT_AUTH = "auth"
RESULT_UNREACHABLE = "unreachable"
RESULT_NO_IP = "no_ip"
RESULT_NO_GO2RTC = "no_go2rtc"


def ldi_active(coordinator: BoschCameraCoordinator, cam_id: str) -> bool:
    """True when the camera reports its local data interface as active."""
    cache = getattr(coordinator, "local_data_interface_cache", None) or {}
    entry = cache.get(cam_id)
    return isinstance(entry, dict) and entry.get("state") == STATE_ACTIVE


def _ldi_password(coordinator: BoschCameraCoordinator, cam_id: str) -> str | None:
    """Stored password for a camera whose interface is active or not yet known.

    A missing status (fresh start, before the first slow-tier poll) still counts
    when the firmware qualifies, so the first stream open after a restart
    cannot slip onto the cloud. A known inactive/unsupported status does not.
    """
    cache = getattr(coordinator, "local_data_interface_cache", None) or {}
    entry = cache.get(cam_id)
    if isinstance(entry, dict) and entry.get("state"):
        if entry["state"] != STATE_ACTIVE:
            return None
    else:
        fw_cache = getattr(coordinator, "firmware_cache", None) or {}
        info = ((getattr(coordinator, "data", None) or {}).get(cam_id) or {}).get(
            "info"
        ) or {}
        fw = (fw_cache.get(cam_id) or {}).get("current") or info.get("firmwareVersion")
        if not firmware_supports_ldi(fw):
            return None
    passwords = coordinator.entry.options.get(LDI_PASSWORDS_OPTION)
    if not isinstance(passwords, dict):
        return None
    password = passwords.get(cam_id)
    if not isinstance(password, str) or not password.strip():
        return None
    return password


def ldi_wanted(coordinator: BoschCameraCoordinator, cam_id: str) -> bool:
    """True when the camera must be reached locally only.

    Independent of whether a LAN address is known yet: a wanted camera with
    no resolvable address stays without a stream instead of using the cloud.
    """
    return _ldi_password(coordinator, cam_id) is not None


SOURCE_LDI = "local_data_interface"
SOURCE_LDI_PROBLEM = "local_data_interface_problem"
SOURCE_CLOUD_UNCONFIGURED = "cloud_no_password"
SOURCE_CLOUD = "cloud"
VIDEO_SOURCE_OPTIONS: list[str] = [
    SOURCE_LDI,
    SOURCE_LDI_PROBLEM,
    SOURCE_CLOUD_UNCONFIGURED,
    SOURCE_CLOUD,
]


def ldi_video_source(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> tuple[str, str | None]:
    """Where the camera's video comes from, plus the open problem if any.

    `local_data_interface`: local only, no Bosch cloud. `..._problem`: wanted
    locally but the last open failed (reason: auth/unreachable/no_ip/
    no_go2rtc); there is no cloud fallback. `cloud_no_password`: the camera
    offers the interface but no password is stored, so the cloud is used.
    `cloud`: interface off or unsupported. A not-yet-polled interface counts
    as wanted when the firmware qualifies and a password is stored.
    """
    if ldi_wanted(coordinator, cam_id):
        status = (getattr(coordinator, "ldi_open_status", None) or {}).get(cam_id)
        reason = status.get("reason") if isinstance(status, dict) else None
        if isinstance(reason, str):
            return SOURCE_LDI_PROBLEM, reason
        return SOURCE_LDI, None
    if ldi_active(coordinator, cam_id):
        return SOURCE_CLOUD_UNCONFIGURED, None
    return SOURCE_CLOUD, None


def resolve_ldi_ip(coordinator: BoschCameraCoordinator, cam_id: str) -> str | None:
    """Safe LAN address from the RCP/credential caches or the cloud Wi-Fi status."""
    from .coordinator import _is_safe_local_camera_host

    candidates: list[str] = []
    ip = coordinator.get_cam_lan_ip(cam_id)
    if ip:
        candidates.append(ip)
    wifi = getattr(coordinator, "wifiinfo_cache", None) or {}
    wifi_ip = (wifi.get(cam_id) or {}).get("ipAddress")
    if isinstance(wifi_ip, str) and wifi_ip:
        candidates.append(wifi_ip)
    for candidate in candidates:
        if _is_safe_local_camera_host(f"{candidate}:{LDI_RTSP_PORT}"):
            return candidate
    return None


def ldi_source(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> tuple[str, str, str] | None:
    """Return (lan_ip, user, password) when the local-only source applies.

    None means the source cannot be used right now: interface not active, no
    usable password stored, or no safe LAN address known. Callers that must
    not fall back to the cloud check `ldi_wanted` separately.
    """
    password = _ldi_password(coordinator, cam_id)
    if password is None:
        return None
    ip = resolve_ldi_ip(coordinator, cam_id)
    if not ip:
        return None
    return ip, LDI_USER, password


def ldi_serviceable(coordinator: BoschCameraCoordinator, cam_id: str) -> bool:
    """True when the camera is served over its local data interface, has a
    usable LAN address and was last seen reachable on the LAN.

    Independent of the Bosch cloud, so entities that only need the local path
    use it to stay available while the cloud poll fails. Never raises: it is
    read on every state update.
    """
    try:
        if ldi_video_source(coordinator, cam_id)[0] != SOURCE_LDI:
            return False
        if ldi_source(coordinator, cam_id) is None:
            return False
        is_lan_reachable = getattr(coordinator, "is_lan_reachable", None)
        return is_lan_reachable is not None and is_lan_reachable(cam_id) is True
    except (AttributeError, TypeError, ValueError):
        return False


def record_ldi_result(
    coordinator: BoschCameraCoordinator, cam_id: str, result: str
) -> None:
    """Track the outcome of the last local-only open (drives the Repairs issue).

    Success and privacy mode (a legitimate closed state) clear the failure.
    """
    status: dict[str, dict[str, Any]] | None = getattr(
        coordinator, "ldi_open_status", None
    )
    if status is None:
        status = {}
        coordinator.ldi_open_status = status
    if result in (RESULT_OK, RESULT_PRIVACY):
        status.pop(cam_id, None)
        return
    prev = status.get(cam_id)
    since = prev["since"] if prev and prev.get("reason") == result else time.monotonic()
    status[cam_id] = {"reason": result, "since": since}


def ldi_inst_for_quality(quality: object) -> int:
    """Camera stream for the quality select: low -> 2, high/auto -> 1."""
    return LDI_INST_LOW if quality == "low" else LDI_INST_HIGH


def _quality(coordinator: BoschCameraCoordinator, cam_id: str) -> str:
    getter = getattr(coordinator, "get_quality", None)
    value = getter(cam_id) if callable(getter) else "auto"
    return value if isinstance(value, str) else "auto"


def ldi_source_url(
    ip: str,
    user: str,
    password: str,
    *,
    inst: int = LDI_INST_HIGH,
    audio: bool = True,
) -> str:
    """go2rtc source for the camera; userinfo is percent-encoded."""
    return (
        f"rtsps://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{ip}:{LDI_RTSP_PORT}{LDI_STREAM_PATH}"
        f"?line=1&inst={inst}&enableaudio={int(audio)}"
    )


def ldi_privacy_on(coordinator: BoschCameraCoordinator, cam_id: str) -> bool | None:
    """Privacy state: the camera's own answer first, else the cloud/SHC cache."""
    rest: dict[str, dict[str, Any]] = getattr(coordinator, "ldi_rest_state", None) or {}
    local = (rest.get(cam_id) or {}).get("privacy_on")
    if isinstance(local, bool):
        return local
    shc = (getattr(coordinator, "shc_state_cache", None) or {}).get(cam_id) or {}
    cloud = shc.get("privacy_mode")
    return cloud if isinstance(cloud, bool) else None


def ldi_local_firmware(coordinator: BoschCameraCoordinator, cam_id: str) -> str | None:
    """Firmware the camera itself reported over its local REST interface."""
    rest: dict[str, dict[str, Any]] = getattr(coordinator, "ldi_rest_state", None) or {}
    value = (rest.get(cam_id) or {}).get("firmware")
    return value if isinstance(value, str) else None


def _rest_entry(coordinator: BoschCameraCoordinator, cam_id: str) -> dict[str, Any]:
    """Per-camera REST record, created on first use."""
    store: dict[str, dict[str, Any]] | None = getattr(
        coordinator, "ldi_rest_state", None
    )
    if store is None:
        store = {}
        coordinator.ldi_rest_state = store
    return store.setdefault(
        cam_id,
        {
            "checked_at": float("-inf"),
            "version_at": float("-inf"),
            "privacy_on": None,
            "firmware": None,
        },
    )


def _apply_probe(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    probe: ldi_rest.LdiProbe,
) -> None:
    """Store a REST probe and mirror its reachability into the open status."""
    entry = _rest_entry(coordinator, cam_id)
    entry["result"] = probe.result
    if probe.result != ldi_rest.PROBE_OK:
        entry["probe_fail_seq"] = entry.get("probe_fail_seq", 0) + 1
    # Without an answer the privacy state is unknown again (the cloud flag
    # takes over), so a stale "on" cannot hide an unreachable camera.
    entry["privacy_on"] = (
        probe.privacy_on if probe.result == ldi_rest.PROBE_OK else None
    )
    if probe.result != ldi_rest.PROBE_OK:
        _clear_settings(coordinator, cam_id)
    if probe.result == ldi_rest.PROBE_OK and probe.firmware is not None:
        entry["firmware"] = probe.firmware
    status: dict[str, dict[str, Any]] = (
        getattr(coordinator, "ldi_open_status", None) or {}
    )
    reason = status.get(cam_id, {}).get("reason")
    if probe.result == ldi_rest.PROBE_AUTH:
        record_ldi_result(coordinator, cam_id, RESULT_AUTH)
    elif probe.result in (ldi_rest.PROBE_UNREACHABLE, ldi_rest.PROBE_ERROR):
        record_ldi_result(coordinator, cam_id, RESULT_UNREACHABLE)
    elif reason in (RESULT_AUTH, RESULT_UNREACHABLE):
        # The camera answers and accepts the password again.
        status.pop(cam_id, None)


async def refresh_ldi_rest(
    coordinator: BoschCameraCoordinator, cam_id: str, *, force: bool = False
) -> ldi_rest.LdiProbe | None:
    """Ask the camera's local REST interface; at most once a minute per camera.

    None when throttled or when the camera cannot be addressed. The firmware
    is read once, then hourly. The stamp is taken before the request so
    overlapping ticks cannot stack requests.
    """
    source = ldi_source(coordinator, cam_id)
    if source is None:
        return None
    entry = _rest_entry(coordinator, cam_id)
    now = time.monotonic()
    if not force and now - entry["checked_at"] < LDI_REST_POLL_SEC:
        return None
    tries: int = entry.get("version_tries", 0)
    # Unknown firmware is retried each poll a few times (transient miss); a
    # model without the endpoint then falls back to the hourly read instead
    # of a second request every minute.
    want_version = (
        entry["firmware"] is None and tries < _LDI_VERSION_RETRIES
    ) or now - entry["version_at"] >= _LDI_VERSION_POLL_SEC
    entry["checked_at"] = now
    if want_version:
        entry["version_at"] = now
        entry["version_tries"] = 0 if entry["firmware"] is not None else tries + 1
    ip, user, password = source
    probe = await ldi_rest.probe_camera(
        coordinator.hass, ip, user, password, want_version=want_version
    )
    _apply_probe(coordinator, cam_id, probe)
    return probe


def _clear_settings(coordinator: BoschCameraCoordinator, cam_id: str) -> None:
    """Forget the local settings, so entities fall back to the cloud cache."""
    store = getattr(coordinator, "ldi_rest_state", None)
    entry = store.get(cam_id) if isinstance(store, dict) else None
    if not isinstance(entry, dict) or entry.get("settings") is None:
        return
    entry["settings"] = None
    coordinator.async_update_listeners()


async def refresh_ldi_settings(
    coordinator: BoschCameraCoordinator, cam_id: str, *, force: bool = False
) -> bool:
    """Read the camera's settings locally; at most every two minutes.

    Runs only after a successful probe and never twice at once per camera.
    The stamp is taken before the request. A failed batch or a camera that
    cannot be addressed clears the settings. Endpoints the model lacks (404)
    are asked again only hourly. True when a batch ran.
    """
    source = ldi_source(coordinator, cam_id)
    if source is None:
        _clear_settings(coordinator, cam_id)
        return False
    entry = _rest_entry(coordinator, cam_id)
    if entry.get("result") != ldi_rest.PROBE_OK or entry.get("settings_busy"):
        return False
    now = time.monotonic()
    due = now - entry.get(
        "settings_checked_at", float("-inf")
    ) >= LDI_SETTINGS_POLL_SEC or now >= entry.get("settings_due", float("inf"))
    if not (force or due):
        return False
    entry["settings_checked_at"] = now
    # A regular poll inside the write hold must not cancel the re-read that
    # follows the hold: its value could still be the pre-write one.
    if now >= entry.get("settings_due", float("inf")):
        entry["settings_due"] = float("inf")
    entry["settings_busy"] = True
    fail_seq = entry.get("probe_fail_seq", 0)
    missing: dict[str, float] = entry.setdefault("settings_missing", {})
    skip = frozenset(
        path for path, at in missing.items() if now - at < _LDI_MISSING_RECHECK_SEC
    )
    ip, user, password = source
    try:
        batch = await ldi_rest.get_settings(
            coordinator.hass, ip, user, password, skip=skip
        )
    finally:
        entry["settings_busy"] = False
    if (
        batch.result != ldi_rest.PROBE_OK
        or entry.get("result") != ldi_rest.PROBE_OK
        or entry.get("probe_fail_seq", 0) != fail_seq
    ):
        # A probe that failed while the batch ran already cleared the settings;
        # the batch must not bring them back.
        _clear_settings(coordinator, cam_id)
        return True
    for path, _specs in ldi_rest.SETTINGS_ENDPOINTS:
        if path in batch.missing_paths:
            missing[path] = now
        elif path not in skip:
            missing.pop(path, None)
    entry["settings"] = {
        "at": now,
        "fields": {
            key: field.value
            for key, field in batch.fields.items()
            if field.status == ldi_rest.FIELD_OK
        },
    }
    coordinator.async_update_listeners()
    return True


async def poll_ldi_local(coordinator: BoschCameraCoordinator, cam_id: str) -> None:
    """One coordinator tick for a local-only camera: probe, then settings."""
    await refresh_ldi_rest(coordinator, cam_id)
    await refresh_ldi_settings(coordinator, cam_id)


def note_cloud_write(
    coordinator: BoschCameraCoordinator, cam_id: str, endpoint: str
) -> None:
    """Hold back the local value of settings a cloud write just changed.

    Also asks for a fresh local read right after the hold ends. Never raises:
    it runs inside every cloud write.
    """
    keys = _CLOUD_WRITE_SETTINGS.get(endpoint)
    if not keys or not isinstance(getattr(coordinator, "ldi_rest_state", None), dict):
        return
    try:
        entry = _rest_entry(coordinator, cam_id)
        now = time.monotonic()
        writes: dict[str, float] = entry.setdefault("writes", {})
        for key in keys:
            writes[key] = now
        # The newest write decides: a read before its hold ends would be hidden.
        entry["settings_due"] = now + LDI_WRITE_HOLD_SEC + 1
    except (AttributeError, TypeError, ValueError) as err:
        _LOGGER.debug("local settings write note for %s: %s", cam_id[:8], err)


# Cloud body field -> local setting key, per whole-block cloud endpoint.
CFG_LOCAL_KEYS: dict[str, dict[str, str]] = {
    "audioDetectionConfig": {
        "detectGlassBreak": ldi_rest.SETTING_GLASS_BREAK,
        "detectFireAlarm": ldi_rest.SETTING_FIRE_ALARM,
    },
    "intrusionDetectionConfig": {
        "enabled": ldi_rest.SETTING_INTRUSION_ENABLED,
        "sensitivity": ldi_rest.SETTING_INTRUSION_SENSITIVITY,
        "distance": ldi_rest.SETTING_INTRUSION_DISTANCE,
    },
    "alarm_settings": {
        "alarmMode": ldi_rest.SETTING_ALARM_ENABLE,
        "preAlarmMode": ldi_rest.SETTING_PRE_ALARM_ENABLE,
        "alarmDelayInSeconds": ldi_rest.SETTING_ALARM_DELAY,
        "alarmActivationDelaySeconds": ldi_rest.SETTING_ALARM_ACTIVATION_DELAY,
        "preAlarmDelayInSeconds": ldi_rest.SETTING_PRE_ALARM_DELAY,
    },
}


# Value ranges the entities accept; a local number outside is not merged.
_CFG_NUMBER_RANGES: dict[str, tuple[float, float]] = {
    ldi_rest.SETTING_INTRUSION_SENSITIVITY: (0, 7),
    ldi_rest.SETTING_INTRUSION_DISTANCE: (1, 8),
    ldi_rest.SETTING_ALARM_DELAY: (10, 300),
    ldi_rest.SETTING_ALARM_ACTIVATION_DELAY: (0, 600),
    ldi_rest.SETTING_PRE_ALARM_DELAY: (0, 300),
}


def merge_local_into_cfg(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    cfg: dict[str, Any],
    endpoint: str,
) -> dict[str, Any]:
    """`cfg` with the camera's own values filled in before a whole-block write.

    The cloud PUT bodies carry every field of the block. The cached cloud copy
    can lag behind what the camera reports locally, and a stale sibling field
    would then overwrite the camera's value. Call this before changing the one
    field the caller means to write.
    """
    merged = dict(cfg)
    for field, key in CFG_LOCAL_KEYS.get(endpoint, {}).items():
        if field not in merged:
            continue
        local = ldi_setting(coordinator, cam_id, key)
        if local is None:
            continue
        current = merged[field]
        if isinstance(local, bool):
            if isinstance(current, str):
                merged[field] = "ON" if local else "OFF"
            elif isinstance(current, bool):
                merged[field] = local
        elif isinstance(current, int | float) and not isinstance(current, bool):
            lo, hi = _CFG_NUMBER_RANGES.get(key, (float("-inf"), float("inf")))
            if lo <= local <= hi:
                merged[field] = round(local) if isinstance(current, int) else local
    return merged


def ldi_setting(
    coordinator: BoschCameraCoordinator, cam_id: str, key: str
) -> bool | int | float | None:
    """A setting as the camera reported it locally, None when not usable.

    None when the camera has no local password, the value is older than
    `LDI_SETTINGS_MAX_AGE_SEC`, or a cloud write to it is still settling
    (inside the hold, or the read started before the write). Callers then
    use their cloud cache. Never raises: it is read on every state update.
    """
    try:
        store = getattr(coordinator, "ldi_rest_state", None)
        entry = store.get(cam_id) if isinstance(store, dict) else None
        if not isinstance(entry, dict):
            return None
        settings = entry.get("settings")
        if not isinstance(settings, dict):
            return None
        at = settings.get("at")
        if isinstance(at, bool) or not isinstance(at, int | float):
            return None
        now = time.monotonic()
        if now - at > LDI_SETTINGS_MAX_AGE_SEC:
            return None
        written = (entry.get("writes") or {}).get(key)
        if isinstance(written, int | float) and (
            now - written < LDI_WRITE_HOLD_SEC or at < written + LDI_WRITE_HOLD_SEC
        ):
            return None
        if not ldi_wanted(coordinator, cam_id):
            return None
        value = (settings.get("fields") or {}).get(key)
        return value if isinstance(value, bool | int | float) else None
    except (AttributeError, TypeError, ValueError):
        return None


async def _drop_stale_stream(coordinator: BoschCameraCoordinator, cam_id: str) -> None:
    """Invalidate HA's cached Stream so it is rebuilt against the new URL."""
    cam_ent = coordinator.camera_entities.get(cam_id)
    stale = getattr(cam_ent, "stream", None) if cam_ent is not None else None
    if stale is None or cam_ent is None:
        return
    try:
        await asyncio.wait_for(stale.stop(), timeout=_STALE_STREAM_STOP_TIMEOUT)
    except Exception as err:
        _LOGGER.debug("stale Stream.stop() for %s: %s", cam_id[:8], err)
    cam_ent.stream = None


async def _stop_cloud_plumbing(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> None:
    """Stop the TLS proxy and viewing front doors of an earlier cloud session."""
    await coordinator.stop_viewing_front_door(cam_id)
    await coordinator.stop_remote_viewing_front_door(cam_id)
    await coordinator.stop_tls_proxy(cam_id)


async def _abort(coordinator: BoschCameraCoordinator, cam_id: str) -> None:
    """Undo a half-built local session."""
    coordinator.live_connections.pop(cam_id, None)
    coordinator.stream_warming.discard(cam_id)
    session = coordinator.get_session(cam_id)
    session.warming_started = float("-inf")
    session.stream_ready_event.set()
    await ldi_go2rtc.unregister_stream(coordinator, cam_id)


async def _probe_camera(
    coordinator: BoschCameraCoordinator, cam_id: str, ip: str, user: str, password: str
) -> ldi_rest.LdiProbe:
    """Ask the camera over REST, retrying transient silence.

    A definite answer (up, or password rejected) ends the retries. The result
    also refreshes the cached local privacy state and firmware.
    """
    probe = ldi_rest.LdiProbe(ldi_rest.PROBE_UNREACHABLE)
    for attempt in range(_PROBE_ATTEMPTS):
        probe = await ldi_rest.probe_camera(
            coordinator.hass, ip, user, password, want_version=True
        )
        if probe.result in (ldi_rest.PROBE_OK, ldi_rest.PROBE_AUTH):
            break
        if attempt + 1 < _PROBE_ATTEMPTS:
            await asyncio.sleep(_PROBE_RETRY_WAIT)
    entry = _rest_entry(coordinator, cam_id)
    entry["checked_at"] = time.monotonic()
    _apply_probe(coordinator, cam_id, probe)
    return probe


def _main_source_url(
    coordinator: BoschCameraCoordinator, cam_id: str, source: tuple[str, str, str]
) -> str:
    """Source of the main stream: stream and audio as the camera is set up."""
    ip, user, password = source
    return ldi_source_url(
        ip,
        user,
        password,
        inst=ldi_inst_for_quality(_quality(coordinator, cam_id)),
    )


async def ensure_ldi_stream(
    coordinator: BoschCameraCoordinator, cam_id: str, *, low: bool = False
) -> str | None:
    """Restream URL of the camera's go2rtc stream, registering it if needed.

    `low` asks for the low-quality stream (an external recorder's second
    switch). When the main stream already is the low one it is reused;
    otherwise it gets its own go2rtc stream, which is a second camera session.

    None means fail closed: no usable local source, or go2rtc is missing or
    not taking the stream. A go2rtc failure is tracked for the Repairs issue
    and cleared again once the stream is back.
    """
    source = ldi_source(coordinator, cam_id)
    if source is None:
        return None
    ip, user, password = source
    main_inst = ldi_inst_for_quality(_quality(coordinator, cam_id))
    if low and main_inst != LDI_INST_LOW:
        return await ldi_go2rtc.ensure_stream(
            coordinator,
            cam_id,
            ldi_source_url(ip, user, password, inst=LDI_INST_LOW),
            variant=VARIANT_LOW,
        )
    url = await ldi_go2rtc.ensure_stream(
        coordinator, cam_id, _main_source_url(coordinator, cam_id, source)
    )
    if url is None:
        record_ldi_result(coordinator, cam_id, RESULT_NO_GO2RTC)
        return None
    status: dict[str, dict[str, Any]] = getattr(coordinator, "ldi_open_status", {})
    if status.get(cam_id, {}).get("reason") == RESULT_NO_GO2RTC:
        status.pop(cam_id)
    return url


async def open_ldi_connection(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    source: tuple[str, str, str],
    *,
    is_renewal: bool = False,
) -> dict[str, Any] | None:
    """Open the local-only session; None on any failure, never a cloud call."""
    ip, user, password = source
    session = coordinator.get_session(cam_id)
    session.stream_ready_event.clear()
    coordinator.stream_warming.add(cam_id)
    session.warming_started = time.monotonic()
    try:
        await _drop_stale_stream(coordinator, cam_id)
        await _stop_cloud_plumbing(coordinator, cam_id)
        probe = await _probe_camera(coordinator, cam_id, ip, user, password)
        if probe.result != ldi_rest.PROBE_OK or probe.privacy_on is True:
            if (
                probe.privacy_on is True
                or coordinator.shc_state_cache.get(cam_id, {}).get("privacy_mode")
                is True
            ):
                result_kind = RESULT_PRIVACY
            elif probe.result == ldi_rest.PROBE_AUTH:
                result_kind = RESULT_AUTH
            else:
                result_kind = RESULT_UNREACHABLE
            record_ldi_result(coordinator, cam_id, result_kind)
            _LOGGER.warning(
                "Local data interface stream for %s is not reachable (%s)",
                cam_id[:8],
                {
                    RESULT_PRIVACY: "privacy mode is on",
                    RESULT_AUTH: "the local password was rejected",
                    RESULT_UNREACHABLE: "camera offline or not answering",
                }[result_kind],
            )
            await _abort(coordinator, cam_id)
            return None

        restream = await ldi_go2rtc.ensure_stream(
            coordinator,
            cam_id,
            _main_source_url(coordinator, cam_id, (ip, user, password)),
            force=True,
        )
        if restream is None:
            record_ldi_result(coordinator, cam_id, RESULT_NO_GO2RTC)
            _LOGGER.warning(
                "Local data interface stream for %s needs go2rtc, which is not "
                "available",
                cam_id[:8],
            )
            await _abort(coordinator, cam_id)
            return None

        result: dict[str, Any] = {
            "urls": [f"{ip}:{LDI_RTSP_PORT}"],
            "_connection_type": "LOCAL",
            "_ldi": True,
            "rtspsUrl": restream,
            "rtspUrl": restream,
            "_bufferingTime": 500,
        }
        coordinator.live_connections[cam_id] = result
        coordinator.live_opened_at[cam_id] = time.monotonic()
        # Cloud-equivalent stream id (1 high, 4 low): the quality select reads
        # an effective id of 2 as "low was clamped to the balanced stream",
        # which is not the case here.
        coordinator._quality_effective_inst[cam_id] = (
            4
            if ldi_inst_for_quality(_quality(coordinator, cam_id)) == LDI_INST_LOW
            else 1
        )
        coordinator.stream_warming.discard(cam_id)
        session.stream_ready_event.set()
        session.generation += 1
    except asyncio.CancelledError:
        # Never leave the camera "warming": waiters would hang until timeout.
        coordinator.live_connections.pop(cam_id, None)
        coordinator.stream_warming.discard(cam_id)
        session.warming_started = float("-inf")
        session.stream_ready_event.set()
        raise
    except Exception as err:
        _LOGGER.warning(
            "Local data interface stream for %s failed: %s",
            cam_id[:8],
            ldi_go2rtc.redact_urls(str(err)),
        )
        record_ldi_result(coordinator, cam_id, RESULT_UNREACHABLE)
        await _abort(coordinator, cam_id)
        return None

    record_ldi_result(coordinator, cam_id, RESULT_OK)

    cam_entity = coordinator.camera_entities.get(cam_id)
    if cam_entity is not None:
        try:
            await cam_entity.async_refresh_providers()
        except Exception as err:
            _LOGGER.debug(
                "post-connect refresh_providers failed for %s: %s", cam_id[:8], err
            )
    if not is_renewal:
        coordinator.async_update_listeners()
    coordinator.hass.async_create_task(coordinator.check_and_recover_webrtc(cam_id))
    if coordinator.nvr_user_intent.get(cam_id):
        from . import nvr_recorder

        coordinator.hass.async_create_task(
            nvr_recorder.start_recorder(
                coordinator, cam_id, reason="local data interface session opened"
            ),
            name=f"bosch_nvr_start_{cam_id[:8]}",
        )
    if coordinator.entry.options.get("enable_green_it", False):
        coordinator.replace_reaper_task(
            cam_id, coordinator.idle_session_reaper(cam_id, session.generation)
        )
    _LOGGER.info("Local data interface stream opened for %s", cam_id[:8])
    return result
