"""SHC local API and Cloud API setter functions for Bosch Smart Home cameras.

Extracted from the coordinator class to keep __init__.py focused on
polling / data merging.  Every function receives the coordinator instance
as its first argument instead of using `self`.

Public API (cloud setters — used by switch / number entities):
  async_cloud_set_privacy_mode(coordinator, cam_id, enabled)
  async_cloud_set_camera_light(coordinator, cam_id, on)
  async_cloud_set_light_component(coordinator, cam_id, component, value)
  async_cloud_set_notifications(coordinator, cam_id, enabled)
  async_cloud_set_pan(coordinator, cam_id, position)

SHC-only setters (used as fallback by the cloud setters above):
  async_shc_set_camera_light(coordinator, cam_id, on)
  async_shc_set_privacy_mode(coordinator, cam_id, enabled)

Low-level helpers:
  shc_configured(coordinator) -> bool
  shc_ready(coordinator) -> bool
  async_shc_request(coordinator, method, path, body) -> dict | list | None
  async_update_shc_states(coordinator, data) -> None
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession

if TYPE_CHECKING:
    from . import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)

CLOUD_API = "https://residential.cbs.boschsecurity.com"


# ── SHC availability helpers ────────────────────────────────────────────────

def shc_configured(coordinator: BoschCameraCoordinator) -> bool:
    """True if SHC local API is fully configured (IP + certs)."""
    opts = coordinator.options
    return bool(
        opts.get("shc_ip", "").strip()
        and opts.get("shc_cert_path", "").strip()
        and opts.get("shc_key_path", "").strip()
    )


def shc_ready(coordinator: BoschCameraCoordinator) -> bool:
    """True if SHC is configured AND currently considered available.

    When SHC is offline (too many consecutive failures), returns False
    unless the retry interval has elapsed.
    """
    if not shc_configured(coordinator):
        return False
    if coordinator._shc_available:
        return True
    # SHC is offline -- check if retry interval has passed
    now = time.monotonic()
    if now - coordinator._shc_last_check >= coordinator._SHC_RETRY_INTERVAL:
        return True  # allow one retry
    return False


def _shc_mark_success(coordinator: BoschCameraCoordinator) -> None:
    """Mark SHC as healthy after a successful request."""
    if not coordinator._shc_available:
        _LOGGER.info("SHC local API is back online")
    coordinator._shc_available = True
    coordinator._shc_fail_count = 0


def _shc_mark_failure(coordinator: BoschCameraCoordinator) -> None:
    """Track a failed SHC request; mark offline after N consecutive failures."""
    coordinator._shc_fail_count += 1
    coordinator._shc_last_check = time.monotonic()
    if (
        coordinator._shc_fail_count >= coordinator._SHC_MAX_FAILS
        and coordinator._shc_available
    ):
        coordinator._shc_available = False
        _LOGGER.warning(
            "SHC local API marked offline after %d consecutive failures -- "
            "will retry in %ds. Falling back to cloud API.",
            coordinator._shc_fail_count,
            coordinator._SHC_RETRY_INTERVAL,
        )


# ── SHC low-level request ───────────────────────────────────────────────────

async def async_shc_request(
    coordinator: BoschCameraCoordinator,
    method: str,
    path: str,
    body: dict | None = None,
) -> dict | list | None:
    """Make a request to the SHC local API using mutual TLS.

    Returns parsed JSON on success, None on failure.
    Requires shc_ip, shc_cert_path, shc_key_path in options.
    Tracks SHC health -- marks offline after repeated failures.
    """
    import ssl

    opts = coordinator.options
    shc_ip = opts.get("shc_ip", "").strip()
    cert_path = opts.get("shc_cert_path", "").strip()
    key_path = opts.get("shc_key_path", "").strip()
    if not shc_ip or not cert_path or not key_path:
        return None

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.load_cert_chain(cert_path, key_path)
    except Exception as err:
        _LOGGER.warning("SHC TLS setup failed (check cert/key paths): %s", err)
        _shc_mark_failure(coordinator)
        return None

    url = f"https://{shc_ip}:8444/smarthome{path}"
    headers = {"api-version": "3.2", "Content-Type": "application/json"}
    try:
        connector = aiohttp.TCPConnector(ssl=ctx)
        async with aiohttp.ClientSession(connector=connector) as s:
            async with asyncio.timeout(10):
                if method == "GET":
                    async with s.get(url, headers=headers) as r:
                        if r.status == 200:
                            _shc_mark_success(coordinator)
                            return await r.json()
                        _LOGGER.debug("SHC GET %s -> HTTP %d", path, r.status)
                        _shc_mark_failure(coordinator)
                elif method == "PUT":
                    async with s.put(url, json=body, headers=headers) as r:
                        _LOGGER.debug("SHC PUT %s -> HTTP %d", path, r.status)
                        if r.status in (200, 201, 204):
                            _shc_mark_success(coordinator)
                        else:
                            _shc_mark_failure(coordinator)
                        return {"status": r.status, "ok": r.status in (200, 201, 204)}
    except asyncio.TimeoutError:
        _LOGGER.debug("SHC request timeout: %s %s", method, path)
        _shc_mark_failure(coordinator)
    except aiohttp.ClientError as err:
        _LOGGER.debug("SHC request error %s %s: %s", method, path, err)
        _shc_mark_failure(coordinator)
    except Exception as err:
        _LOGGER.debug("SHC unexpected error %s %s: %s", method, path, err)
        _shc_mark_failure(coordinator)
    return None


# ── SHC state polling ────────────────────────────────────────────────────────

async def async_update_shc_states(
    coordinator: BoschCameraCoordinator, data: dict
) -> None:
    """Fetch CameraLight and PrivacyMode states from SHC for each camera.

    SHC is the PRIMARY source for privacy + light state when configured.
    Values from SHC overwrite any cloud-sourced values from step 4.
    Matches SHC devices to cloud cameras by device name (title).
    Refreshes the SHC device list at most once per 60 seconds.
    """
    if not shc_configured(coordinator):
        return

    # Re-fetch device list at most once per 60 s
    now = time.monotonic()
    if now - coordinator._last_shc_fetch >= 60 or not coordinator._shc_devices_raw:
        devices = await async_shc_request(coordinator, "GET", "/devices")
        if isinstance(devices, list):
            coordinator._shc_devices_raw = devices
            coordinator._last_shc_fetch = now

    shc_devices = coordinator._shc_devices_raw
    if not shc_devices:
        return

    for cam_id, cam in data.items():
        title = cam.get("info", {}).get("title", "").lower().strip()

        # Match SHC device by name (case-insensitive)
        device_id = None
        for dev in shc_devices:
            if dev.get("name", "").lower().strip() == title:
                device_id = dev.get("id")
                break
        if not device_id:
            _LOGGER.debug("SHC: no device found matching camera title '%s'", title)
            continue

        entry = coordinator._shc_state_cache.setdefault(cam_id, {
            "device_id": device_id,
            "camera_light": None,
            "privacy_mode": None,
        })
        entry["device_id"] = device_id

        # Fetch CameraLight service state (SHC is authoritative)
        svc = await async_shc_request(
            coordinator, "GET", f"/devices/{device_id}/services/CameraLight"
        )
        if isinstance(svc, dict):
            val = svc.get("state", {}).get("value", "")
            entry["camera_light"] = (val.upper() == "ON")

        # Fetch PrivacyMode service state (SHC is authoritative)
        svc = await async_shc_request(
            coordinator, "GET", f"/devices/{device_id}/services/PrivacyMode"
        )
        if isinstance(svc, dict):
            val = svc.get("state", {}).get("value", "")
            entry["privacy_mode"] = (val.upper() == "ENABLED")


# ── SHC setters ──────────────────────────────────────────────────────────────

async def async_shc_set_camera_light(
    coordinator: BoschCameraCoordinator, cam_id: str, on: bool
) -> bool:
    """Turn the camera indicator LED on (True) or off (False) via SHC API."""
    device_id = coordinator._shc_state_cache.get(cam_id, {}).get("device_id")
    if not device_id:
        _LOGGER.warning("SHC: no device_id cached for %s -- cannot control light", cam_id)
        return False
    result = await async_shc_request(
        coordinator,
        "PUT",
        f"/devices/{device_id}/services/CameraLight/state",
        {"@type": "cameraLightState", "value": "ON" if on else "OFF"},
    )
    if result and result.get("ok", result.get("status", 0) in (200, 201, 204)):
        coordinator._shc_state_cache[cam_id]["camera_light"] = on
        coordinator.async_update_listeners()
        coordinator.hass.async_create_task(coordinator.async_request_refresh())
        return True
    return False


async def async_shc_set_privacy_mode(
    coordinator: BoschCameraCoordinator, cam_id: str, enabled: bool
) -> bool:
    """Enable (True) or disable (False) privacy mode via SHC API (legacy fallback)."""
    device_id = coordinator._shc_state_cache.get(cam_id, {}).get("device_id")
    if not device_id:
        _LOGGER.warning("SHC: no device_id cached for %s -- cannot set privacy mode", cam_id)
        return False
    result = await async_shc_request(
        coordinator,
        "PUT",
        f"/devices/{device_id}/services/PrivacyMode/state",
        {"@type": "privacyModeState", "value": "ENABLED" if enabled else "DISABLED"},
    )
    if result and result.get("ok", result.get("status", 0) in (200, 201, 204)):
        coordinator._shc_state_cache[cam_id]["privacy_mode"] = enabled
        coordinator._privacy_set_at[cam_id] = time.monotonic()
        coordinator.async_update_listeners()
        coordinator.hass.async_create_task(coordinator.async_request_refresh())
        if not enabled:
            cam = coordinator._camera_entities.get(cam_id)
            if cam:
                coordinator.hass.async_create_task(
                    cam._async_trigger_image_refresh(delay=1.5)
                )
        return True
    return False


# ── Cloud API setters ────────────────────────────────────────────────────────

async def async_cloud_set_privacy_mode(
    coordinator: BoschCameraCoordinator, cam_id: str, enabled: bool
) -> bool:
    """Enable (True) or disable (False) privacy mode.

    Strategy: Cloud API first (~150ms), SHC local API fallback (~1100ms).
    Cloud is 10x faster due to connection pooling; SHC requires fresh mTLS
    handshake per request on an embedded controller.
    SHC fallback ensures control when cloud is unreachable (offline mode).
    """
    # -- Cloud API (primary -- fast) -------------------------------------------
    token = coordinator.token
    if token:
        session = async_get_clientsession(coordinator.hass, verify_ssl=False)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/privacy"
        body = {"privacyMode": "ON" if enabled else "OFF", "durationInSeconds": None}

        try:
            async with asyncio.timeout(10):
                async with session.put(url, json=body, headers=headers) as resp:
                    if resp.status == 401:
                        # Token expired -- refresh and retry once
                        _LOGGER.info(
                            "cloud_set_privacy_mode: 401 -- refreshing token and retrying"
                        )
                        try:
                            token = await coordinator._ensure_valid_token()
                            headers["Authorization"] = f"Bearer {token}"
                        except Exception:
                            pass  # fall through to SHC
                    if resp.status in (200, 201, 204):
                        coordinator._shc_state_cache.setdefault(cam_id, {})[
                            "privacy_mode"
                        ] = enabled
                        coordinator._privacy_set_at[cam_id] = time.monotonic()
                        coordinator.async_update_listeners()
                        _LOGGER.debug(
                            "cloud_set_privacy_mode: %s -> %s (HTTP %d)",
                            cam_id,
                            "ON" if enabled else "OFF",
                            resp.status,
                        )
                        coordinator.hass.async_create_task(
                            coordinator.async_request_refresh()
                        )
                        if not enabled:
                            cam = coordinator._camera_entities.get(cam_id)
                            if cam:
                                coordinator.hass.async_create_task(
                                    cam._async_trigger_image_refresh(delay=1.5)
                                )
                        return True
                    if resp.status == 401:
                        # Retry with refreshed token
                        async with asyncio.timeout(10):
                            async with session.put(
                                url, json=body, headers=headers
                            ) as resp2:
                                if resp2.status in (200, 201, 204):
                                    coordinator._shc_state_cache.setdefault(
                                        cam_id, {}
                                    )["privacy_mode"] = enabled
                                    coordinator._privacy_set_at[
                                        cam_id
                                    ] = time.monotonic()
                                    coordinator.async_update_listeners()
                                    coordinator.hass.async_create_task(
                                        coordinator.async_request_refresh()
                                    )
                                    if not enabled:
                                        cam = coordinator._camera_entities.get(cam_id)
                                        if cam:
                                            coordinator.hass.async_create_task(
                                                cam._async_trigger_image_refresh(
                                                    delay=1.5
                                                )
                                            )
                                    return True
                    _LOGGER.warning(
                        "cloud_set_privacy_mode: HTTP %d for %s", resp.status, cam_id
                    )
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("cloud_set_privacy_mode error for %s: %s", cam_id, err)

    # -- SHC local API fallback (offline mode) ---------------------------------
    if shc_ready(coordinator):
        _LOGGER.info(
            "cloud_set_privacy_mode: cloud failed, falling back to SHC for %s", cam_id
        )
        return await async_shc_set_privacy_mode(coordinator, cam_id, enabled)
    return False


async def async_cloud_set_camera_light(
    coordinator: BoschCameraCoordinator, cam_id: str, on: bool
) -> bool:
    """Turn the camera light on (True) or off (False).

    Strategy: Cloud API first (~150ms), SHC local API fallback (~1100ms).
    SHC fallback ensures control when cloud is unreachable (offline mode).
    Note: SHC CameraLight service only exists for cameras with physical lights
    (Garten/CAMERA_EYES). For cameras without it, SHC fallback will fail silently.
    """
    # -- Cloud API (primary -- fast) -------------------------------------------
    token = coordinator.token
    if token:
        session = async_get_clientsession(coordinator.hass, verify_ssl=False)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/lighting_override"
        cache = coordinator._shc_state_cache.get(cam_id, {})
        last_intensity = cache.get("front_light_intensity") or 1.0
        if on:
            body = {"frontLightOn": True, "wallwasherOn": True, "frontLightIntensity": last_intensity}
        else:
            body = {"frontLightOn": False, "wallwasherOn": False}

        try:
            async with asyncio.timeout(10):
                async with session.put(url, json=body, headers=headers) as resp:
                    if resp.status in (200, 201, 204):
                        cache_entry = coordinator._shc_state_cache.setdefault(cam_id, {})
                        cache_entry["camera_light"] = on
                        cache_entry["front_light"] = on
                        cache_entry["wallwasher"] = on
                        if on and last_intensity:
                            cache_entry["front_light_intensity"] = last_intensity
                        # Write-lock: prevent the next coordinator refresh from
                        # overwriting the optimistic state with stale cloud data
                        # (Bosch API propagation delay).
                        coordinator._light_set_at[cam_id] = time.monotonic()
                        coordinator.async_update_listeners()
                        _LOGGER.debug(
                            "cloud_set_camera_light: %s -> %s (HTTP %d)",
                            cam_id,
                            "ON" if on else "OFF",
                            resp.status,
                        )
                        coordinator.hass.async_create_task(
                            coordinator.async_request_refresh()
                        )
                        return True
                    _LOGGER.warning(
                        "cloud_set_camera_light: HTTP %d for %s", resp.status, cam_id
                    )
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("cloud_set_camera_light error for %s: %s", cam_id, err)

    # -- SHC local API fallback (offline mode) ---------------------------------
    if shc_ready(coordinator):
        _LOGGER.info(
            "cloud_set_camera_light: cloud failed, falling back to SHC for %s", cam_id
        )
        return await async_shc_set_camera_light(coordinator, cam_id, on)
    return False


async def async_cloud_set_light_component(
    coordinator: BoschCameraCoordinator, cam_id: str, component: str, value
) -> bool:
    """Set individual light component via PUT /v11/video_inputs/{id}/lighting_override.

    component: "front" (bool), "wallwasher" (bool), or "intensity" (float 0.0-1.0).
    Reads current state from cache to preserve the other components.
    """
    token = coordinator.token
    if not token:
        return False

    cache = coordinator._shc_state_cache.get(cam_id, {})
    # Build body from current cached state, then apply the change
    front = cache.get("front_light") or False
    wall = cache.get("wallwasher") or False
    intensity = cache.get("front_light_intensity") or 1.0

    if component == "front":
        front = value
    elif component == "wallwasher":
        wall = value
    elif component == "intensity":
        intensity = value
        front = True  # turning intensity on implies front light on

    body = {
        "frontLightOn": front,
        "wallwasherOn": wall,
    }
    if front:
        body["frontLightIntensity"] = intensity

    session = async_get_clientsession(coordinator.hass, verify_ssl=False)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/lighting_override"

    try:
        async with asyncio.timeout(10):
            async with session.put(url, json=body, headers=headers) as resp:
                if resp.status in (200, 201, 204):
                    cache_entry = coordinator._shc_state_cache.setdefault(cam_id, {})
                    cache_entry["front_light"] = front
                    cache_entry["wallwasher"] = wall
                    cache_entry["front_light_intensity"] = intensity
                    cache_entry["camera_light"] = front or wall
                    coordinator._light_set_at[cam_id] = time.monotonic()
                    coordinator.async_update_listeners()
                    _LOGGER.debug(
                        "cloud_set_light_component: %s %s=%s (front=%s wall=%s int=%.2f)",
                        cam_id[:8], component, value, front, wall, intensity,
                    )
                    coordinator.hass.async_create_task(
                        coordinator.async_request_refresh()
                    )
                    return True
                _LOGGER.warning(
                    "cloud_set_light_component: HTTP %d for %s", resp.status, cam_id
                )
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.warning("cloud_set_light_component error for %s: %s", cam_id, err)
    return False


async def async_cloud_set_notifications(
    coordinator: BoschCameraCoordinator, cam_id: str, enabled: bool
) -> bool:
    """Enable (FOLLOW_CAMERA_SCHEDULE) or disable (ALWAYS_OFF) notifications via cloud API.

    Uses PUT /v11/video_inputs/{id}/enable_notifications.
    """
    token = coordinator.token
    if not token:
        _LOGGER.warning("cloud_set_notifications: no token for %s", cam_id)
        return False

    session = async_get_clientsession(coordinator.hass, verify_ssl=False)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/enable_notifications"
    status = "FOLLOW_CAMERA_SCHEDULE" if enabled else "ALWAYS_OFF"
    body = {"enabledNotificationsStatus": status}

    try:
        async with asyncio.timeout(10):
            async with session.put(url, json=body, headers=headers) as resp:
                if resp.status in (200, 201, 204):
                    coordinator._shc_state_cache.setdefault(cam_id, {})[
                        "notifications_status"
                    ] = status
                    coordinator._notif_set_at[cam_id] = time.monotonic()
                    coordinator.async_update_listeners()
                    _LOGGER.debug(
                        "cloud_set_notifications: %s -> %s (HTTP %d)",
                        cam_id,
                        status,
                        resp.status,
                    )
                    coordinator.hass.async_create_task(
                        coordinator.async_request_refresh()
                    )
                    return True
                _LOGGER.warning(
                    "cloud_set_notifications: HTTP %d for %s", resp.status, cam_id
                )
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.warning("cloud_set_notifications error for %s: %s", cam_id, err)
    return False


async def async_cloud_set_pan(
    coordinator: BoschCameraCoordinator, cam_id: str, position: int
) -> bool:
    """Pan the 360 camera to an absolute position (-120 to +120 degrees).

    Uses PUT /v11/video_inputs/{id}/pan -- no SHC local API needed.
    """
    token = coordinator.token
    if not token:
        _LOGGER.warning("cloud_set_pan: no token for %s", cam_id)
        return False

    session = async_get_clientsession(coordinator.hass, verify_ssl=False)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/pan"

    try:
        async with asyncio.timeout(10):
            async with session.put(
                url, json={"absolutePosition": position}, headers=headers
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    actual = data.get("currentAbsolutePosition", position)
                    coordinator._pan_cache[cam_id] = actual
                    _LOGGER.debug(
                        "cloud_set_pan: %s -> %d deg (HTTP %d, ETA %dms)",
                        cam_id,
                        actual,
                        resp.status,
                        data.get("estimatedTimeToCompletion", 0),
                    )
                    coordinator.hass.async_create_task(
                        coordinator.async_request_refresh()
                    )
                    return True
                _LOGGER.warning(
                    "cloud_set_pan: HTTP %d for %s", resp.status, cam_id
                )
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.warning("cloud_set_pan error for %s: %s", cam_id, err)
    return False
