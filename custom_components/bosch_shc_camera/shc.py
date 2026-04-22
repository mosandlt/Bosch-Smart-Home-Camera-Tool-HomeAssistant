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
            _schedule_privacy_off_snapshot(coordinator, cam_id)
        return True
    return False


def _schedule_privacy_off_snapshot(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> None:
    """Trigger a fresh snapshot after privacy mode was disabled.

    Delay depends on the camera's hardware:
    - **Outdoor cameras** (no physical shutter, instant-on): 0.5s — just enough
      for the cloud API to propagate the privacy-off state so /snap.jpg returns
      a fresh frame instead of the privacy placeholder.
    - **Indoor cameras** (physical motor-driven shutter + lens cover): 5.0s —
      Gen1 360 motor-drives the lens upward, Gen2 Indoor II tilts the head.
      Snap.jpg returns the privacy placeholder until the shutter fully opens
      AND the encoder produces a valid frame. User-observed: 4s occasionally
      returned a placeholder frame for Gen2 Indoor II that bytes-matched the
      next poll, stalling the card spinner on the old image; 5s covers the
      slowest observed shutter-open + encoder-ready cycle.
    """
    cam = coordinator._camera_entities.get(cam_id)
    if not cam:
        return
    hw = coordinator._hw_version.get(cam_id, "")
    hw_lower = hw.lower()
    is_indoor = (
        hw in ("INDOOR", "CAMERA_360", "HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2")
        or "indoor" in hw_lower
        or "360" in hw_lower
    )
    delay = 5.0 if is_indoor else 0.5
    _LOGGER.debug(
        "Privacy-OFF snapshot trigger for %s (hw=%s, delay=%.1fs)",
        cam_id[:8], hw, delay,
    )
    coordinator.hass.async_create_task(
        cam._async_trigger_image_refresh(delay=delay)
    )


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
                            _schedule_privacy_off_snapshot(coordinator, cam_id)
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
                                        _schedule_privacy_off_snapshot(coordinator, cam_id)
                                    return True
                    _LOGGER.warning(
                        "cloud_set_privacy_mode: HTTP %d for %s", resp.status, cam_id
                    )
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("cloud_set_privacy_mode error for %s: %s", cam_id, err)

    # -- Gen2 LOCAL RCP fallback (cloud outage) --------------------------------
    # When the Bosch cloud (auth server or API) is unreachable, Gen2 cameras
    # still answer unauthenticated RCP commands on their LAN IP. Try this
    # before SHC — LOCAL RCP works directly against the camera without any
    # Bosch infrastructure involved.
    if _is_gen2(coordinator, cam_id):
        creds = coordinator._local_creds_cache.get(cam_id)
        cam_host = creds.get("host") if creds else coordinator._rcp_lan_ip_cache.get(cam_id)
        if cam_host:
            from .rcp import rcp_local_write_privacy
            ok = await rcp_local_write_privacy(coordinator.hass, cam_host, enabled)
            if ok:
                _LOGGER.info(
                    "cloud_set_privacy_mode: cloud failed, Gen2 LOCAL RCP succeeded for %s",
                    cam_id,
                )
                coordinator._shc_state_cache.setdefault(cam_id, {})[
                    "privacy_mode"
                ] = enabled
                coordinator._privacy_set_at[cam_id] = time.monotonic()
                coordinator.async_update_listeners()
                return True
            _LOGGER.debug(
                "cloud_set_privacy_mode: Gen2 LOCAL RCP fallback failed for %s — "
                "camera may not accept unauthenticated writes",
                cam_id,
            )

    # -- SHC local API fallback (offline mode) ---------------------------------
    if shc_ready(coordinator):
        _LOGGER.info(
            "cloud_set_privacy_mode: cloud failed, falling back to SHC for %s", cam_id
        )
        return await async_shc_set_privacy_mode(coordinator, cam_id, enabled)

    # -- All fallbacks exhausted — surface a persistent notification ----------
    if coordinator._auth_outage_count > 0:
        try:
            await coordinator.hass.services.async_call(
                "persistent_notification", "create",
                {
                    "title": "Bosch Kamera — Privacy-Befehl nicht zugestellt",
                    "message": (
                        f"Privacy-Mode {'ON' if enabled else 'OFF'} für `{cam_id[:8]}…` "
                        "konnte nicht gesetzt werden: Bosch-Cloud nicht erreichbar "
                        "und lokaler Fallback fehlgeschlagen.\n\n"
                        "Sobald die Cloud wieder online ist, bitte erneut schalten."
                    ),
                    "notification_id": f"bosch_privacy_queued_{cam_id[:8]}",
                },
            )
        except Exception:
            pass
    return False


def _is_gen2(coordinator: BoschCameraCoordinator, cam_id: str) -> bool:
    """Check if a camera is Gen2 (uses different lighting endpoints)."""
    from .models import get_model_config
    hw = coordinator._hw_version.get(cam_id, "CAMERA")
    return get_model_config(hw).generation >= 2


async def async_cloud_set_camera_light(
    coordinator: BoschCameraCoordinator, cam_id: str, on: bool
) -> bool:
    """Turn the camera light on (True) or off (False).

    Strategy: Cloud API first (~150ms), SHC local API fallback (~1100ms).
    Gen1: PUT /lighting_override with frontLightOn + wallwasherOn
    Gen2: PUT /lighting/switch/front + /lighting/switch/topdown with enabled
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

        gen2 = _is_gen2(coordinator, cam_id)
        ok = False

        if gen2:
            # Gen2: separate endpoints for front and top-down lights
            base = f"{CLOUD_API}/v11/video_inputs/{cam_id}/lighting/switch"
            body_toggle = {"enabled": on}
            try:
                async with asyncio.timeout(10):
                    async with session.put(f"{base}/front", json=body_toggle, headers=headers) as r1:
                        ok1 = r1.status in (200, 201, 204)
                    async with session.put(f"{base}/topdown", json=body_toggle, headers=headers) as r2:
                        ok2 = r2.status in (200, 201, 204)
                    ok = ok1 or ok2
                    if not ok:
                        _LOGGER.warning("cloud_set_camera_light (gen2): front=%d topdown=%d for %s", r1.status, r2.status, cam_id)
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.warning("cloud_set_camera_light (gen2) error for %s: %s", cam_id, err)
        else:
            # Gen1: single endpoint with combined body
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
                        ok = resp.status in (200, 201, 204)
                        if not ok:
                            _LOGGER.warning("cloud_set_camera_light: HTTP %d for %s", resp.status, cam_id)
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.warning("cloud_set_camera_light error for %s: %s", cam_id, err)

        if ok:
            cache_entry = coordinator._shc_state_cache.setdefault(cam_id, {})
            cache_entry["camera_light"] = on
            cache_entry["front_light"] = on
            cache_entry["wallwasher"] = on
            coordinator._light_set_at[cam_id] = time.monotonic()
            coordinator.async_update_listeners()
            _LOGGER.debug(
                "cloud_set_camera_light: %s -> %s (gen%d)",
                cam_id[:8], "ON" if on else "OFF", 2 if gen2 else 1,
            )
            coordinator.hass.async_create_task(coordinator.async_request_refresh())
            return True

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
    """Set individual light component.

    Gen1: PUT /v11/video_inputs/{id}/lighting_override
      component: "front" (bool), "wallwasher" (bool), or "intensity" (float 0.0-1.0).
    Gen2: PUT /v11/video_inputs/{id}/lighting/switch/front or /topdown
      component: "front" (bool), "wallwasher" (bool), or "intensity" (int 0-100).
    """
    token = coordinator.token
    if not token:
        return False

    session = async_get_clientsession(coordinator.hass, verify_ssl=False)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    cache = coordinator._shc_state_cache.get(cam_id, {})
    gen2 = _is_gen2(coordinator, cam_id)
    ok = False

    if gen2:
        # Gen2: separate endpoints per light group
        base = f"{CLOUD_API}/v11/video_inputs/{cam_id}/lighting/switch"
        if component == "front":
            url = f"{base}/front"
            body = {"enabled": value}
        elif component == "wallwasher":
            # Wallwasher controls BOTH top + bottom LEDs.
            # Must sync brightness via /lighting/switch AND toggle via /topdown
            # to keep light entities and wallwasher switch in sync.
            lsc = coordinator._lighting_switch_cache.get(cam_id, {})
            front_settings = lsc.get("frontLightSettings", {"brightness": 0, "color": None, "whiteBalance": -1.0})
            if not hasattr(coordinator, "_last_topdown_brightness"):
                coordinator._last_topdown_brightness = {}
            if value:
                # Turn ON: restore last brightness, then enable topdown
                saved = coordinator._last_topdown_brightness.get(cam_id, {})
                top_bri = saved.get("top", 100)
                bot_bri = saved.get("bottom", 100)
                top_settings = {**lsc.get("topLedLightSettings", {"color": None, "whiteBalance": -1.0}), "brightness": top_bri}
                bot_settings = {**lsc.get("bottomLedLightSettings", {"color": None, "whiteBalance": -1.0}), "brightness": bot_bri}
            else:
                # Turn OFF: save current brightness, then zero it
                cur_top = lsc.get("topLedLightSettings", {}).get("brightness", 0)
                cur_bot = lsc.get("bottomLedLightSettings", {}).get("brightness", 0)
                if cur_top > 0 or cur_bot > 0:
                    coordinator._last_topdown_brightness[cam_id] = {"top": cur_top or 100, "bottom": cur_bot or 100}
                top_settings = {**lsc.get("topLedLightSettings", {"color": None, "whiteBalance": -1.0}), "brightness": 0}
                bot_settings = {**lsc.get("bottomLedLightSettings", {"color": None, "whiteBalance": -1.0}), "brightness": 0}
            full_body = {
                "frontLightSettings": front_settings,
                "topLedLightSettings": top_settings,
                "bottomLedLightSettings": bot_settings,
            }
            # Step 1: Set brightness via /lighting/switch
            try:
                async with asyncio.timeout(10):
                    async with session.put(base, json=full_body, headers=headers) as resp:
                        if resp.status in (200, 201, 204):
                            try:
                                rsp = await resp.json()
                                coordinator._lighting_switch_cache[cam_id] = rsp
                            except Exception:
                                coordinator._lighting_switch_cache[cam_id] = full_body
                        else:
                            _LOGGER.warning("cloud_set_light_component (gen2): lighting/switch HTTP %d for %s", resp.status, cam_id[:8])
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.warning("cloud_set_light_component (gen2) lighting/switch error for %s: %s", cam_id, err)
            # Step 2: Toggle topdown switch
            url = f"{base}/topdown"
            body = {"enabled": value}
        elif component == "intensity":
            # Gen2 brightness is 0-100 (Gen1 is 0.0-1.0)
            brightness = int(value * 100) if isinstance(value, float) and value <= 1.0 else int(value)
            url = base
            body = {
                "frontLightSettings": {"brightness": brightness, "whiteBalance": -1.0, "color": None},
                "topLedLightSettings": {"brightness": brightness, "whiteBalance": -1.0, "color": None},
                "bottomLedLightSettings": {"brightness": brightness, "whiteBalance": -1.0, "color": None},
            }
        else:
            return False
        try:
            async with asyncio.timeout(10):
                async with session.put(url, json=body, headers=headers) as resp:
                    ok = resp.status in (200, 201, 204)
                    if not ok:
                        _LOGGER.warning("cloud_set_light_component (gen2): HTTP %d for %s %s", resp.status, cam_id[:8], component)
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("cloud_set_light_component (gen2) error for %s: %s", cam_id, err)
    else:
        # Gen1: single endpoint with combined body
        front = cache.get("front_light") or False
        wall = cache.get("wallwasher") or False
        intensity = cache.get("front_light_intensity") or 1.0

        if component == "front":
            front = value
        elif component == "wallwasher":
            wall = value
        elif component == "intensity":
            intensity = value

        body = {
            "frontLightOn": front,
            "wallwasherOn": wall,
            "frontLightIntensity": intensity,
        }
        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/lighting_override"
        try:
            async with asyncio.timeout(10):
                async with session.put(url, json=body, headers=headers) as resp:
                    ok = resp.status in (200, 201, 204)
                    if not ok:
                        _LOGGER.warning("cloud_set_light_component: HTTP %d for %s", resp.status, cam_id)
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("cloud_set_light_component error for %s: %s", cam_id, err)

    if ok:
        cache_entry = coordinator._shc_state_cache.setdefault(cam_id, {})
        if component == "front":
            cache_entry["front_light"] = value
        elif component == "wallwasher":
            cache_entry["wallwasher"] = value
        elif component == "intensity":
            cache_entry["front_light_intensity"] = value
        cache_entry["camera_light"] = cache_entry.get("front_light") or cache_entry.get("wallwasher")
        coordinator._light_set_at[cam_id] = time.monotonic()
        coordinator.async_update_listeners()
        _LOGGER.debug(
            "cloud_set_light_component: %s %s=%s (gen%d)",
            cam_id[:8], component, value, 2 if gen2 else 1,
        )
        coordinator.hass.async_create_task(coordinator.async_request_refresh())
        return True
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
    # Block pan while privacy mode is active (camera shutter closed, motor disabled)
    privacy = coordinator._shc_state_cache.get(cam_id, {}).get("privacy_mode")
    if privacy:
        _LOGGER.debug("cloud_set_pan: blocked — Privacy Mode is ON for %s", cam_id)
        return False

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
