"""Local-only video source over the camera's local data interface.

When a camera reports the local data interface as active and the user stored
its password, the camera is read over its LAN address (RTSP over TLS, Digest
auth) and never touches the Bosch cloud: no token check, no PUT /connection,
no REMOTE fallback. go2rtc is the single upstream reader; the live view, the
Mini-NVR recorder and the external-recorder endpoint all read its local
restream (see ldi_go2rtc.py), because the camera serves only a few
concurrent sessions. Without go2rtc there is no stream. Failure leaves the
camera without a stream until the next local attempt.
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from bosch_shc_camera_client.tls_proxy import _digest_auth

from . import ldi_go2rtc
from .local_data_interface import STATE_ACTIVE, firmware_supports_ldi

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)

LDI_RTSP_PORT = 9554
LDI_USER = "localuser"
LDI_PASSWORDS_OPTION = "local_passwords"
LDI_STREAM_PATH = "/live"

_STALE_STREAM_STOP_TIMEOUT = 5
_PROBE_ATTEMPTS = 2
_PROBE_RETRY_WAIT = 2


RESULT_OK = "ok"
RESULT_PRIVACY = "privacy"
RESULT_AUTH = "auth"
RESULT_UNREACHABLE = "unreachable"
RESULT_NO_IP = "no_ip"
RESULT_NO_GO2RTC = "no_go2rtc"

_PROBE_STATUS_RE = re.compile(r"^RTSP/\d\.\d\s+(\d{3})")


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


def _insecure_context() -> ssl.SSLContext:
    """TLS context for the camera's self-signed certificate (no disk access)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # camera serves a self-signed certificate
    return ctx


def ldi_source_url(ip: str, user: str, password: str) -> str:
    """go2rtc source for the camera; userinfo is percent-encoded."""
    return (
        f"rtsps://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{ip}:{LDI_RTSP_PORT}{LDI_STREAM_PATH}"
    )


async def _probe_describe_status(
    ip: str, user: str, password: str, timeout: float
) -> int | None:
    """RTSP status of one authenticated DESCRIBE straight at the camera.

    None = no answer. Only the status line is read; no media session is set up.
    """
    uri = f"rtsps://{ip}:{LDI_RTSP_PORT}{LDI_STREAM_PATH}"
    writer: asyncio.StreamWriter | None = None
    try:
        reader, conn = await asyncio.wait_for(
            asyncio.open_connection(ip, LDI_RTSP_PORT, ssl=_insecure_context()),
            timeout,
        )
        writer = conn
        writer.write(
            f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n".encode()
        )
        await writer.drain()
        first = (await asyncio.wait_for(reader.read(4096), timeout)).decode(
            "utf-8", errors="replace"
        )
        nonce = re.search(r'nonce="([^"]+)"', first)
        realm = re.search(r'realm="([^"]+)"', first)
        if not (nonce and realm):
            match = _PROBE_STATUS_RE.match(first)
            status = int(match.group(1)) if match else None
            # A challenge without a parsable nonce (split read) is not a
            # verdict on the password.
            return None if status == 401 else status
        auth = _digest_auth(user, password, "DESCRIBE", uri, realm[1], nonce[1])
        writer.write(
            f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n"
            f"Authorization: {auth}\r\n\r\n".encode()
        )
        await writer.drain()
        second = (await asyncio.wait_for(reader.read(4096), timeout)).decode(
            "utf-8", errors="replace"
        )
        match = _PROBE_STATUS_RE.match(second)
        return int(match.group(1)) if match else None
    except (OSError, TimeoutError):
        return None
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:  # best-effort close
                pass


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
) -> int | None:
    """Probe the camera, retrying transient silence; 200/401 end the retries."""
    timeout = float(coordinator.get_model_config(cam_id).describe_timeout)
    status: int | None = None
    for attempt in range(_PROBE_ATTEMPTS):
        status = await _probe_describe_status(ip, user, password, timeout)
        if status in (200, 401):
            break
        if attempt + 1 < _PROBE_ATTEMPTS:
            await asyncio.sleep(_PROBE_RETRY_WAIT)
    return status


async def ensure_ldi_stream(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> str | None:
    """Restream URL of the camera's go2rtc stream, registering it if needed.

    None means fail closed: no usable local source, or go2rtc is missing or
    not taking the stream. A go2rtc failure is tracked for the Repairs issue
    and cleared again once the stream is back.
    """
    source = ldi_source(coordinator, cam_id)
    if source is None:
        return None
    ip, user, password = source
    url = await ldi_go2rtc.ensure_stream(
        coordinator, cam_id, ldi_source_url(ip, user, password)
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
        status = await _probe_camera(coordinator, cam_id, ip, user, password)
        if status != 200:
            if coordinator.shc_state_cache.get(cam_id, {}).get("privacy_mode") is True:
                result_kind = RESULT_PRIVACY
            elif status == 401:
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
            coordinator, cam_id, ldi_source_url(ip, user, password), force=True
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
        coordinator._quality_effective_inst[cam_id] = 1
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
