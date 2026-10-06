"""Bosch Smart Home Camera — Light Platform (Gen2 only).

Creates native HA light entities for Gen2 cameras (Eyes Außenkamera II):
  - Top LED Light   — RGB color OR white (color temperature) + brightness
                      (oberes Licht, "tausende Farben" / Weißtöne)
  - Bottom LED Light — RGB color OR white (color temperature) + brightness
                      (unteres Licht, "tausende Farben" / Weißtöne)
  - Front Light     — color temperature + brightness (Frontlicht, kaltweiß↔warmweiß)

Gen2 lighting API: PUT /v11/video_inputs/{id}/lighting/switch
Each light group uses EITHER color (HEX #RRGGBB) OR whiteBalance (-1.0 to 1.0), never both.
When color is set, whiteBalance becomes null (color mode).
When whiteBalance is set, color becomes null (temperature mode).

Gen1 cameras use a different API (lighting_override) and are handled by switch.py instead.
"""

import asyncio
import logging
import time
from typing import Any, ClassVar

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.color import color_temperature_to_rgb

from . import CLOUD_API, DOMAIN  # type: ignore[attr-defined]
from .cloud_ssl import async_get_bosch_cloud_session
from .dynamic_devices import register_dynamic_camera_listener
from .guards import _warn_if_privacy_on

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# whiteBalance <-> Kelvin mapping shared by every light group.
# -1.0 (cool/blue) = 6500 K, 0.0 = 4250 K, 1.0 (warm/orange) = 2000 K.
MIN_COLOR_TEMP_KELVIN = 2000
MAX_COLOR_TEMP_KELVIN = 6500

# Light-group modes as reported by GET /lighting/switch: a group carries
# EITHER `color` (hex) OR `whiteBalance`, never both.
_MODE_COLOR = "color"
_MODE_WHITE = "white"


def _wb_to_kelvin(wb: float) -> int:
    """Convert Bosch whiteBalance (-1.0 … 1.0) to Kelvin (6500 … 2000)."""
    return round(4250 - wb * 2250)


def _kelvin_to_wb(kelvin: float) -> float:
    """Convert Kelvin to Bosch whiteBalance, clamped to -1.0 … 1.0."""
    wb = round((4250 - kelvin) / 2250, 2)
    return max(-1.0, min(1.0, wb))


def _hex_to_rgb_list(color_hex: str) -> list[int] | None:
    """Decode '#RRGGBB' to [r, g, b]; None on malformed input."""
    h = color_hex.lstrip("#")
    try:
        return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)]
    except ValueError:
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = config_entry.runtime_data

    def _build_entities_for_cam(cam_id: str) -> list[Any]:
        cam_info = coordinator.data.get(cam_id, {}).get("info", {})
        # Prefer cam_info hardwareVersion (live cloud data); fall back to the
        # persistent `hw_version` store so a cold-start during a cloud outage
        # still creates the right entities for Outdoor II. `getattr` keeps the
        # test stubs that don't seed the `hw_version` dict happy.
        _hw_cache = getattr(coordinator, "hw_version", {}) or {}
        hw = cam_info.get("hardwareVersion") or _hw_cache.get(cam_id, "CAMERA")
        from .models import get_model_config

        cam_entities: list[Any] = []
        if get_model_config(hw).generation >= 2:
            has_light = cam_info.get("featureSupport", {}).get("light", False)
            # ONLY Outdoor II has controllable lights (RGB top + bottom + color-
            # temp front spotlight). Indoor II has NO visible light hardware —
            # only the IR night-vision LEDs which are not user-controllable.
            # Bosch's API correctly reports `featureSupport.light=false` for it.
            if has_light:
                cam_entities.append(BoschTopLedLight(coordinator, cam_id, config_entry))
                cam_entities.append(
                    BoschBottomLedLight(coordinator, cam_id, config_entry)
                )
                cam_entities.append(BoschFrontLight(coordinator, cam_id, config_entry))
        return cam_entities

    known_cam_ids: set[str] = set(coordinator.data)
    entities: list[Any] = []
    for cam_id in known_cam_ids:
        entities.extend(_build_entities_for_cam(cam_id))
    async_add_entities(entities, update_before_add=False)

    # Quality-Scale Gold `dynamic-devices`.
    config_entry.async_on_unload(
        register_dynamic_camera_listener(
            coordinator, known_cam_ids, async_add_entities, _build_entities_for_cam
        )
    )


class _BoschLightBase(CoordinatorEntity, LightEntity, RestoreEntity):  # type: ignore[misc]
    """Base class for Gen2 light entities.

    Inherits from RestoreEntity so `_last_color_hex`, `_last_brightness`,
    and `_last_white_balance` survive HA restarts — without this, after a
    restart the entity has no memory of the last user-picked color, and
    the card's color circles fall back to warm-white default.
    """

    _led_key: str = (
        ""  # "frontLightSettings", "topLedLightSettings", "bottomLedLightSettings"
    )
    _attr_has_entity_name = True

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry = entry
        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model = info.get("hardwareVersion", "CAMERA")
        from .models import get_display_name

        self._model_name = get_display_name(self._model)
        self._fw = info.get("firmwareVersion", "")
        self._mac = info.get("macAddress", "")

        # Local state cache
        self._brightness: int = 0
        self._last_brightness: int = (
            100  # remember last non-zero brightness for restore on turn_on
        )
        self._color_hex: str | None = None
        self._last_color_hex: str | None = None  # None = user has never picked a color
        self._white_balance: float | None = None
        self._last_white_balance: float | None = -1.0
        self._is_on: bool = False

    async def async_added_to_hass(self) -> None:
        """Restore last-known color/brightness/whiteBalance across HA restarts."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.attributes is None:
            return
        # Prefer the extra attribute we wrote ourselves (kept when light is off)
        lrc = last_state.attributes.get("last_rgb_color")
        if isinstance(lrc, (list, tuple)) and len(lrc) == 3:
            try:
                r, g, b = (int(lrc[0]), int(lrc[1]), int(lrc[2]))
                self._last_color_hex = f"#{r:02X}{g:02X}{b:02X}"
            except (ValueError, TypeError):
                pass
        lbri = last_state.attributes.get("last_brightness_pct")
        if isinstance(lbri, (int, float)) and 1 <= lbri <= 100:
            self._last_brightness = int(lbri)
        lwb = last_state.attributes.get("last_white_balance")
        if isinstance(lwb, (int, float)) and -1.0 <= lwb <= 1.0:
            self._last_white_balance = float(lwb)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose last-known values even when the light is off.

        HA's light platform blanks `rgb_color` / `brightness` when state=="off",
        so the Lovelace card can't read them to show the last user-picked
        color on the color circle. These extra attributes stay populated
        regardless of on/off state.
        """
        attrs: dict[str, Any] = {}
        color_hex = self._color_hex or self._last_color_hex
        if color_hex:
            h = color_hex.lstrip("#")
            try:
                attrs["last_rgb_color"] = [
                    int(h[0:2], 16),
                    int(h[2:4], 16),
                    int(h[4:6], 16),
                ]
            except ValueError:
                pass
        else:
            # Display-only warm-white default so the card's color dot isn't
            # grey before the user has ever picked a color. Never written to
            # the API — the turn_on fallback sends `color: null` instead so
            # the camera keeps its own default.
            attrs["last_rgb_color"] = [255, 180, 100]
        if self._last_brightness:
            attrs["last_brightness_pct"] = self._last_brightness
        if self._last_white_balance is not None:
            attrs["last_white_balance"] = self._last_white_balance
        return attrs

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._cam_id)},
            "name": f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model": self._model_name,
            "sw_version": self._fw,
            "connections": {("mac", self._mac)} if self._mac else set(),
        }

    @property
    def is_on(self) -> bool:
        self._load_state_from_cache()
        return self._is_on

    @property
    def brightness(self) -> int | None:
        """HA brightness is 0-255, API brightness is 0-100.

        When off, return last brightness so the UI slider keeps its position.
        """
        self._load_state_from_cache()
        if self._is_on:
            return int(self._brightness * 255 / 100) if self._brightness else 0
        return int(self._last_brightness * 255 / 100) if self._last_brightness else None

    @property
    def available(self) -> bool:
        """Cloud-primary with LAN-reachability fallback (Gen2 only).

        When the Bosch cloud is unreachable but the camera is pingable on
        the LAN, the coordinator's set-light path will fall through to a
        direct RCP write — so the entity must remain controllable. Without
        this fallback, every Bosch cloud 5xx leaves the light entities grey
        even though they are toggleable on the same LAN via the Bosch app.

        Exception: during a firmware install the camera reboots — writes
        would fail mid-flight, so flip unavailable until the slow-tier poll
        clears the `updating` flag.
        """
        is_updating = getattr(self.coordinator, "is_updating", None)
        if is_updating is not None and is_updating(self._cam_id):
            return False
        if self.coordinator.last_update_success:
            return True
        is_lan_reachable = getattr(self.coordinator, "is_lan_reachable", None)
        if is_lan_reachable is None:
            return False
        if not bool(is_lan_reachable(self._cam_id)):
            return False
        # See switch.py BoschPrivacyModeSwitch.available — same relaxation:
        # if hw_version isn't yet known (cold-start during cloud outage),
        # allow the toggle. The write fails cleanly for Gen1.
        from .shc import _is_gen2

        if _is_gen2(self.coordinator, self._cam_id):
            return True
        hw = self.coordinator.hw_version.get(self._cam_id)
        return hw in (None, "", "CAMERA")

    def _load_state_from_cache(self) -> None:
        """Sync state from coordinator lighting/switch cache.

        Called on every property access so HA reflects changes made via the
        Bosch app (polled by the coordinator).  Remembers last non-zero
        brightness and last color for restore-on-turn-on.
        """
        lsc = self.coordinator.lighting_switch_cache.get(self._cam_id, {})
        if not lsc:
            return
        led = lsc.get(self._led_key, {})
        bri = led.get("brightness", 0)
        color = led.get("color")
        wb = led.get("whiteBalance")
        self._brightness = bri
        self._is_on = bri > 0
        if bri > 0:
            self._last_brightness = bri
        if color:
            self._color_hex = color
            self._last_color_hex = color
            self._white_balance = None
        elif wb is not None:
            self._white_balance = wb
            self._last_white_balance = wb
            self._color_hex = None

    def _get_current_state(self) -> dict[str, Any]:
        """Get the current lighting/switch state from coordinator cache."""
        cached = self.coordinator.lighting_switch_cache.get(self._cam_id, {})
        # Default fallback if cache is empty
        return {
            "frontLightSettings": cached.get(
                "frontLightSettings",
                {"brightness": 0, "color": None, "whiteBalance": -1.0},
            ),
            "topLedLightSettings": cached.get(
                "topLedLightSettings",
                {"brightness": 0, "color": None, "whiteBalance": -1.0},
            ),
            "bottomLedLightSettings": cached.get(
                "bottomLedLightSettings",
                {"brightness": 0, "color": None, "whiteBalance": -1.0},
            ),
        }

    async def _put_lighting_switch(self, updates: dict[str, Any]) -> bool:
        """Send PUT /lighting/switch — ALWAYS sends full body with all 3 groups.

        The Bosch API requires all 3 light groups in every PUT request.
        `updates` contains only the keys to change; the rest is read from cache.
        """
        token = self.coordinator.token
        if not token:
            return False
        # Serialize the read-modify-write per camera. /lighting/switch REQUIRES
        # the full 3-group body in every PUT, so two concurrent sibling writes
        # (e.g. a scene toggling Top + Bottom LED, or Front + white-balance) that
        # each build their body from a pre-write cache snapshot would each
        # re-send the OTHER group's stale value — reverting it both in the cache
        # AND on the actual camera. The lock makes each write build its body from
        # a cache that already contains the prior write's result, and we merge
        # only the changed group(s) back (never the whole entry). Matches the
        # merge-only-own-key pattern number.py also uses.
        locks = getattr(self.coordinator, "lighting_switch_locks", None)
        if locks is None:
            locks = {}
            self.coordinator.lighting_switch_locks = locks
        lock = locks.get(self._cam_id)
        if lock is None:
            lock = asyncio.Lock()
            locks[self._cam_id] = lock
        async with lock:
            # Build full body INSIDE the lock so it reflects any sibling write
            # that just completed.
            body = self._get_current_state()
            for key, val in updates.items():
                if key in body:
                    body[key] = {**body[key], **val}  # merge, not replace
                else:
                    body[key] = val
            session = await async_get_bosch_cloud_session(self.hass)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            try:
                async with asyncio.timeout(10):
                    async with session.put(
                        f"{CLOUD_API}/v11/video_inputs/{self._cam_id}/lighting/switch",
                        headers=headers,
                        json=body,
                    ) as resp:
                        if resp.status in (200, 201, 204):
                            # /lighting/switch returns 204 No Content (empty body);
                            # 200/201 would carry authoritative JSON. Prefer the
                            # server body when present, else the optimistic `body`
                            # we sent — calling resp.json() unconditionally on a
                            # 204 raises, which would leave the cache never
                            # updated and is_on stuck False.
                            try:
                                rsp = await resp.json(content_type=None)
                            except Exception:
                                rsp = None
                            authoritative = (
                                rsp if (rsp and isinstance(rsp, dict)) else body
                            )
                            # Merge ONLY the group(s) we changed into the live
                            # cache — never overwrite the whole entry, or a sibling
                            # group written concurrently would be clobbered.
                            cur = self.coordinator.lighting_switch_cache.setdefault(
                                self._cam_id, {}
                            )
                            for key in updates:
                                if key in authoritative:
                                    cur[key] = authoritative[key]
                            return True
                        _LOGGER.warning(
                            "lighting/switch HTTP %d for %s",
                            resp.status,
                            self._cam_id[:8],
                        )
            except Exception as err:
                _LOGGER.warning(
                    "lighting/switch error for %s: %s", self._cam_id[:8], err
                )
            return False

    async def _put_switch_endpoint(self, endpoint: str, enabled: bool) -> bool:
        """Send PUT /lighting/switch/front or /topdown."""
        token = self.coordinator.token
        if not token:
            return False
        session = await async_get_bosch_cloud_session(self.hass)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            async with asyncio.timeout(10):
                async with session.put(
                    f"{CLOUD_API}/v11/video_inputs/{self._cam_id}/lighting/switch/{endpoint}",
                    headers=headers,
                    json={"enabled": enabled},
                ) as resp:
                    return resp.status in (200, 201, 204)
        except Exception as err:
            _LOGGER.warning("lighting/switch/%s error: %s", endpoint, err)
        return False

    def _sync_wallwasher_cache(self) -> None:
        """Sync camera_light/wallwasher switch state from lighting/switch cache.

        Called after ANY light entity (Front, Top LED, Bottom LED) turn_on/
        turn_off so the `switch.*_kameralicht` entity (which reads
        shc_state_cache, not lighting_switch_cache) reflects the change
        immediately instead of waiting for the next coordinator poll — and
        so the light_set_at write-lock protects the fresh value from being
        overwritten by a stale SHC/cloud poll in flight (see shc.py).
        """
        lsc = self.coordinator.lighting_switch_cache.get(self._cam_id, {})
        top_bri = lsc.get("topLedLightSettings", {}).get("brightness", 0)
        bot_bri = lsc.get("bottomLedLightSettings", {}).get("brightness", 0)
        front_bri = lsc.get("frontLightSettings", {}).get("brightness", 0)
        cache_entry = self.coordinator.shc_state_cache.setdefault(self._cam_id, {})
        # Mirror slow_tier.py's _process_cam_state derivation of all 4 fields
        # from the same 3 brightnesses — else switch.*_front_light and
        # number.*_front_light_intensity (unlike wallwasher/camera_light)
        # stay stale under this same call's light_set_at lock (GitHub #66).
        cache_entry["front_light"] = front_bri > 0
        cache_entry["front_light_intensity"] = front_bri / 100.0 if front_bri else 0.0
        cache_entry["wallwasher"] = top_bri > 0 or bot_bri > 0
        cache_entry["camera_light"] = front_bri > 0 or top_bri > 0 or bot_bri > 0
        self.coordinator.light_set_at[self._cam_id] = time.monotonic()
        self.coordinator.async_update_listeners()


# ─────────────────────────────────────────────────────────────────────────────
class _BoschRgbLedLight(_BoschLightBase):
    """Base for Top/Bottom LED light — RGB color OR white + brightness.

    The Bosch API lets each LED group run in one of two modes: `color`
    (hex) or `whiteBalance` (-1.0 cool … 1.0 warm). The Bosch app's white
    presets ("Kaltweiß", "Warmweiß", …) use the whiteBalance mode. Both
    modes are exposed to HA (ColorMode.RGB + ColorMode.COLOR_TEMP) and the
    reported `color_mode` follows the mode the camera actually reports, so
    a white set in the Bosch app shows up as a color temperature instead of
    a stale, unrelated RGB value.

    Turning the light on without color arguments restores the mode the
    group is currently in (per the camera), never a remembered RGB color
    on top of an active white setting. A color/temperature picked while the
    light is off is held as a pending value and applied on the next turn_on.
    """

    _led_key = ""
    _attr_supported_color_modes: ClassVar[set[ColorMode]] = {
        ColorMode.RGB,
        ColorMode.COLOR_TEMP,
    }
    _attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
    _attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN

    # Class-level defaults so instances built via __new__ (tests) work too.
    # Pending = color/temperature picked while the light was off; applied on
    # the next turn_on. Value is a '#RRGGBB' hex (color) or a whiteBalance
    # float (white).
    _pending_mode: str | None = None
    _pending_value: str | float | None = None
    # Mode restored from the last HA state; used only until the first
    # /lighting/switch poll fills the cache (or during a cloud outage).
    _restored_mode: str | None = None

    # The base class shows this as `last_rgb_color` when no color was ever
    # picked. It is a display value only and must not be restored as a pick.
    _DISPLAY_DEFAULT_RGB: ClassVar[list[int]] = [255, 180, 100]

    # ── mode bookkeeping ──────────────────────────────────────────────────
    def _cached_mode(self) -> str | None:
        """Mode reported by the camera for this group, or None if unknown."""
        lsc = self.coordinator.lighting_switch_cache.get(self._cam_id, {})
        led = lsc.get(self._led_key) or {}
        if led.get("color"):
            return _MODE_COLOR
        if led.get("whiteBalance") is not None:
            return _MODE_WHITE
        return None

    def _known_mode(self) -> str | None:
        """Pending pick > camera-reported mode > mode restored after restart."""
        if self._pending_mode is not None:
            return self._pending_mode
        cached = self._cached_mode()
        if cached is not None:
            return cached
        return self._restored_mode

    def _active_mode(self) -> str:
        mode = self._known_mode()
        if mode is not None:
            return mode
        # Nothing known at all (fresh install, empty cache): keep the
        # previous behavior — a remembered color wins, otherwise white.
        return _MODE_COLOR if self._last_color_hex else _MODE_WHITE

    def _clear_pending(self) -> None:
        self._pending_mode = None
        self._pending_value = None

    def _load_state_from_cache(self) -> None:
        super()._load_state_from_cache()
        # The light was switched on elsewhere (Bosch app, schedule): a value
        # preconfigured in HA while it was off is obsolete.
        if self._is_on and self._pending_mode is not None:
            self._clear_pending()

    def _current_white_balance(self) -> float:
        if self._pending_mode == _MODE_WHITE and isinstance(
            self._pending_value, (int, float)
        ):
            return float(self._pending_value)
        if self._white_balance is not None:
            return self._white_balance
        if self._last_white_balance is not None:
            return self._last_white_balance
        return -1.0

    def _current_color_hex(self) -> str | None:
        if self._pending_mode == _MODE_COLOR and isinstance(self._pending_value, str):
            return self._pending_value
        return self._color_hex or self._last_color_hex

    # ── HA properties ─────────────────────────────────────────────────────
    @property
    def color_mode(self) -> ColorMode:
        self._load_state_from_cache()
        if self._active_mode() == _MODE_WHITE:
            return ColorMode.COLOR_TEMP
        return ColorMode.RGB

    @property
    def color_temp_kelvin(self) -> int | None:
        self._load_state_from_cache()
        return _wb_to_kelvin(self._current_white_balance())

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        self._load_state_from_cache()
        color = self._current_color_hex()
        if color:
            rgb = _hex_to_rgb_list(color)
            if rgb is not None:
                return (rgb[0], rgb[1], rgb[2])
        # Default warm white when no color known (e.g. after HA restart with light off)
        return (255, 180, 100)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Add color-mode info on top of the base last_* attributes.

        `last_rgb_color` is what the Lovelace card paints its color dot
        with, so while the group is (known to be) in white mode it carries
        the RGB equivalent of the white temperature instead of a stale
        color. The last color the user actually picked stays available as
        `last_picked_rgb_color` and is what RestoreState reads back.
        """
        # Refresh from the camera cache first: HA may read the attributes
        # before any other property, and the white value must be current.
        self._load_state_from_cache()
        attrs = super().extra_state_attributes
        picked = self._current_color_hex()
        if picked:
            picked_rgb = _hex_to_rgb_list(picked)
            if picked_rgb is not None:
                attrs["last_picked_rgb_color"] = picked_rgb
                if self._known_mode() == _MODE_COLOR:
                    attrs["last_rgb_color"] = picked_rgb
        if self._known_mode() == _MODE_WHITE:
            kelvin = _wb_to_kelvin(self._current_white_balance())
            r, g, b = color_temperature_to_rgb(kelvin)
            attrs["last_rgb_color"] = [round(r), round(g), round(b)]
            attrs["last_color_temp_kelvin"] = kelvin
            attrs["last_color_mode"] = ColorMode.COLOR_TEMP.value
        else:
            # Same resolution as `color_mode`, so attribute and state agree.
            attrs["last_color_mode"] = (
                ColorMode.COLOR_TEMP.value
                if self._active_mode() == _MODE_WHITE
                else ColorMode.RGB.value
            )
        return attrs

    async def async_added_to_hass(self) -> None:
        """Restore the last *picked* color and the last mode.

        `last_rgb_color` is a display value (white-mode equivalent, or the
        warm-white default when nothing was picked), so the base class must
        not be trusted to restore it as a picked color.
        """
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.attributes is None:
            return
        attrs = last_state.attributes
        mode_attr = attrs.get("last_color_mode")
        if mode_attr is None:
            # Pre-fix state format: only drop the display-only default.
            lrc = attrs.get("last_rgb_color")
            is_default = isinstance(lrc, (list, tuple)) and (
                list(lrc) == self._DISPLAY_DEFAULT_RGB
            )
            if is_default:
                self._last_color_hex = None
            return
        self._restored_mode = (
            _MODE_WHITE if mode_attr == ColorMode.COLOR_TEMP.value else _MODE_COLOR
        )
        self._last_color_hex = None
        lpc = attrs.get("last_picked_rgb_color")
        if isinstance(lpc, (list, tuple)) and len(lpc) == 3:
            try:
                r, g, b = (int(lpc[0]), int(lpc[1]), int(lpc[2]))
                self._last_color_hex = f"#{r:02X}{g:02X}{b:02X}"
            except (ValueError, TypeError):
                pass
        if self._restored_mode == _MODE_COLOR and self._last_color_hex is None:
            self._restored_mode = None

    # ── service calls ─────────────────────────────────────────────────────
    async def async_turn_on(self, **kwargs: Any) -> None:
        # Privacy mode blocks /lighting/switch PUT with HTTP 443 — warn the user.
        if await _warn_if_privacy_on(self, "RGB Light"):
            return
        self._load_state_from_cache()
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        rgb = kwargs.get(ATTR_RGB_COLOR)
        kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        was_off = not self._is_on

        mode: str
        value: str | float
        if rgb:
            color_hex = f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
            self._color_hex = color_hex
            self._last_color_hex = color_hex
            self._white_balance = None
            mode, value = _MODE_COLOR, color_hex
        elif kelvin:
            wb = _kelvin_to_wb(kelvin)
            self._white_balance = wb
            self._last_white_balance = wb
            self._color_hex = None
            mode, value = _MODE_WHITE, wb
        elif self._active_mode() == _MODE_COLOR and self._current_color_hex():
            mode, value = _MODE_COLOR, str(self._current_color_hex())
        else:
            # White mode reported by the camera (e.g. set in the Bosch app),
            # or nothing known at all: keep/restore white instead of
            # replaying a stale RGB color.
            mode, value = _MODE_WHITE, self._current_white_balance()

        if brightness:
            # Round up so brightness=1 (card sentinel for "at least 1 step")
            # doesn't collapse to 0% and skip the last_brightness_pct attribute.
            self._last_brightness = max(1, round(brightness * 100 / 255))

        # Preconfigure while off: any color/brightness change is stored locally
        # but the light stays physically off. User must explicitly toggle the
        # switch row (turn_on with no kwargs) to apply the stored settings.
        if was_off and (rgb or kelvin or brightness):
            if rgb or kelvin:
                self._pending_mode = mode
                self._pending_value = value
            self.async_write_ha_state()
            return

        # Restore last brightness if not specified
        api_brightness = (
            max(1, round(brightness * 100 / 255))
            if brightness
            else (self._last_brightness or 100)
        )

        if mode == _MODE_COLOR:
            settings: dict[str, Any] = {
                "brightness": api_brightness,
                "color": value,
                "whiteBalance": None,
            }
        else:
            settings = {
                "brightness": api_brightness,
                "color": None,
                "whiteBalance": value,
            }
        body = {self._led_key: settings}

        # Only commit the optimistic on-state if the PUT actually succeeded —
        # otherwise is_on/brightness (raw instance vars) would show the light ON
        # after a failed write until the next slow poll corrects it.
        if await self._put_lighting_switch(body):
            self._brightness = api_brightness
            self._last_brightness = api_brightness
            self._is_on = True
            self._clear_pending()
            await self._put_switch_endpoint("topdown", True)
            # Only sync (and stamp the light_set_at write-lock) on confirmed
            # success — on a failed write this would freeze the stale
            # camera_light/wallwasher cache against SHC/cloud correction for
            # the full WRITE_LOCK_SECS window (GitHub #66 bug-hunt finding).
            self._sync_wallwasher_cache()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        # Remember current settings before turning off
        if self._brightness > 0:
            self._last_brightness = self._brightness
        if self._color_hex:
            self._last_color_hex = self._color_hex
        body = {self._led_key: {"brightness": 0}}
        if await self._put_lighting_switch(body):
            self._is_on = False
            self._brightness = 0
            # If BOTH top+bottom are now off, also disable topdown switch
            lsc = self.coordinator.lighting_switch_cache.get(self._cam_id, {})
            top_bri = lsc.get("topLedLightSettings", {}).get("brightness", 0)
            bot_bri = lsc.get("bottomLedLightSettings", {}).get("brightness", 0)
            if top_bri == 0 and bot_bri == 0:
                await self._put_switch_endpoint("topdown", False)
            self._sync_wallwasher_cache()
        self.async_write_ha_state()


class BoschTopLedLight(_BoschRgbLedLight):
    """Light entity: Top LED (oberes Licht) — RGB color + brightness."""

    _led_key = "topLedLightSettings"

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_top_led_light"
        self._attr_translation_key = "top_led_light"


# ─────────────────────────────────────────────────────────────────────────────
class BoschBottomLedLight(_BoschRgbLedLight):
    """Light entity: Bottom LED (unteres Licht) — RGB color + brightness."""

    _led_key = "bottomLedLightSettings"

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_bottom_led_light"
        self._attr_translation_key = "bottom_led_light"


# ─────────────────────────────────────────────────────────────────────────────
class BoschFrontLight(_BoschLightBase):
    """Light entity: Front spotlight — color temperature + brightness.

    Front light only supports white with color temperature (whiteBalance -1.0 to 1.0),
    NOT RGB colors. -1.0 = cool/blue, 0.0 = neutral, 1.0 = warm/orange.
    Mapped to HA color temp: 2000K (warm) to 6500K (cool).
    """

    _led_key = "frontLightSettings"
    _attr_color_mode = ColorMode.COLOR_TEMP
    _attr_supported_color_modes: ClassVar[set[ColorMode]] = {ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
    _attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_front_light_entity"
        self._attr_translation_key = "front_light_entity"
        self._white_balance = -1.0

    @property
    def color_temp_kelvin(self) -> int | None:
        """Convert whiteBalance (-1.0 to 1.0) to Kelvin (6500 to 2000).

        When off, return last value so the UI slider keeps its position.
        """
        self._load_state_from_cache()
        wb = self._white_balance
        if wb is None:
            wb = (
                self._last_white_balance
                if self._last_white_balance is not None
                else -1.0
            )
        # -1.0 (cool) = 6500K, 1.0 (warm) = 2000K
        return _wb_to_kelvin(wb)

    async def async_turn_on(self, **kwargs: Any) -> None:
        # Privacy mode blocks /lighting/switch PUT with HTTP 443 — warn the user.
        if await _warn_if_privacy_on(self, "Front Light"):
            return
        self._load_state_from_cache()
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        color_temp_k = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        was_off = not self._is_on

        if color_temp_k:
            # Convert Kelvin to whiteBalance: 6500K = -1.0, 2000K = 1.0
            wb = _kelvin_to_wb(color_temp_k)
            self._white_balance = wb
            self._last_white_balance = wb
        else:
            wb = self._white_balance if self._white_balance is not None else -1.0

        if brightness:
            self._last_brightness = max(1, round(brightness * 100 / 255))

        # Preconfigure while off: any change is stored locally, light stays off.
        # User must explicitly toggle the switch row to apply the stored values.
        if was_off and (brightness or color_temp_k):
            self.async_write_ha_state()
            return

        api_brightness = (
            max(1, round(brightness * 100 / 255))
            if brightness
            else (self._last_brightness or 100)
        )

        body = {
            self._led_key: {
                "brightness": api_brightness,
                "color": None,
                "whiteBalance": wb,
            }
        }
        if await self._put_lighting_switch(body):
            self._brightness = api_brightness
            self._last_brightness = api_brightness
            self._is_on = True
            await self._put_switch_endpoint("front", True)
            # Only sync (and stamp the light_set_at write-lock) on confirmed
            # success — see the matching comment in _BoschRgbLedLight.
            self._sync_wallwasher_cache()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        # Send brightness=0 to update cache + camera state (like the Bosch app does),
        # then disable via switch endpoint. Without the PUT, the cache retains the old
        # brightness, and any subsequent top/bottom LED PUT would re-enable the front light.
        # Only commit the optimistic off-state if the PUT succeeded.
        wb = self._white_balance if self._white_balance is not None else -1.0
        if await self._put_lighting_switch(
            {self._led_key: {"brightness": 0, "color": None, "whiteBalance": wb}}
        ):
            self._is_on = False
            self._brightness = 0
            await self._put_switch_endpoint("front", False)
            self._sync_wallwasher_cache()
        self.async_write_ha_state()
