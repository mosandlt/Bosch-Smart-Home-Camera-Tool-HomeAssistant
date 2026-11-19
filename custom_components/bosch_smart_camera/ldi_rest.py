"""Read-only client for the camera's local REST interface.

The camera serves a small read-only REST API on its LAN address
(``https://<camera>/sh/...``, HTTP Basic auth with the local user). This
module only reads; every write (privacy, light, ...) stays on the cloud.

TLS: the camera chain is verified against the published device root CA
(pinned below). The certificate's common name is the camera's MAC address,
so hostname checking is off and only the chain is verified.

Every function is one GET and returns an `LdiRestResponse` instead of
raising: `status` is None when the camera did not answer at all. Callers keep
the request rate low (see `ldi_local.refresh_ldi_rest`).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import ssl
from dataclasses import dataclass
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

LDI_REST_TIMEOUT = 5.0
# The camera's answers are tiny; anything larger is not a valid body.
_MAX_BODY_BYTES = 65536

# Published root CA for the camera's local interface (RSA Root CA G1 Prod).
# SHA-256 fingerprint:
#   16:AC:1F:4C:B5:43:2F:F2:EA:29:30:E5:0E:B8:63:04:
#   77:5A:98:2F:FB:D4:C2:8E:01:AB:35:E2:EC:F4:BB:74
LDI_ROOT_CA_PEM = """\
-----BEGIN CERTIFICATE-----
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
BPePFXgk
-----END CERTIFICATE-----
"""

PATH_PING = "/sh/ping"
PATH_VERSION = "/sh/data/version"
PATH_PRIVACY_STATE = "/sh/data/privacyState"
PATH_WIFI_INFO = "/sh/data/wifiInfo"
PATH_CURRENT_NOISE = "/sh/data/audio/currentNoise"

PROBE_OK = "ok"
PROBE_AUTH = "auth"
PROBE_UNREACHABLE = "unreachable"
PROBE_ERROR = "error"

_CONNECT_ERRORS = (TimeoutError, aiohttp.ClientError, OSError, ValueError)

_SSL_CONTEXT: ssl.SSLContext | None = None
_SSL_CONTEXT_LOCK: asyncio.Lock | None = None


@dataclass(frozen=True)
class LdiRestResponse:
    """Outcome of one GET. `status` None: no answer (offline, TLS failure)."""

    status: int | None
    data: Any = None

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def unauthorized(self) -> bool:
        return self.status in (401, 403)

    @property
    def unavailable(self) -> bool:
        """404: this camera model does not offer the endpoint (not an error)."""
        return self.status == 404

    @property
    def unreachable(self) -> bool:
        return self.status is None


@dataclass(frozen=True)
class LdiProbe:
    """Result of `probe_camera`: reachability class plus the local truth."""

    result: str
    privacy_on: bool | None = None
    firmware: str | None = None


def _build_ssl_context() -> ssl.SSLContext:
    """Context trusting only the published camera root CA.

    Loads certificate data, so it runs in an executor. With `cadata` given,
    `create_default_context` does not add the system roots.
    """
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH, cadata=LDI_ROOT_CA_PEM
    )
    context.check_hostname = False  # CN is the camera MAC; chain is verified
    context.verify_mode = ssl.CERT_REQUIRED
    return context


async def async_get_camera_ssl_context(hass: HomeAssistant) -> ssl.SSLContext:
    """Cached camera-CA context, built off the event loop."""
    global _SSL_CONTEXT, _SSL_CONTEXT_LOCK
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    if _SSL_CONTEXT_LOCK is None:
        _SSL_CONTEXT_LOCK = asyncio.Lock()
    async with _SSL_CONTEXT_LOCK:
        if _SSL_CONTEXT is None:
            _SSL_CONTEXT = await hass.async_add_executor_job(_build_ssl_context)
    return _SSL_CONTEXT


def _basic_auth(user: str, password: str) -> str:
    """HTTP Basic header value (built by hand: aiohttp.BasicAuth is deprecated)."""
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


async def _read_capped(resp: aiohttp.ClientResponse) -> str:
    """Body text, or "" when it exceeds the cap (not a valid camera answer)."""
    raw = bytearray()
    async for chunk in resp.content.iter_chunked(4096):
        raw += chunk
        if len(raw) > _MAX_BODY_BYTES:
            return ""
    return raw.decode("utf-8", errors="replace")


async def _get(
    hass: HomeAssistant, ip: str, user: str, password: str, path: str
) -> LdiRestResponse:
    """One authenticated GET; never raises for network-level failures."""
    ctx = await async_get_camera_ssl_context(hass)
    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(LDI_REST_TIMEOUT):
            async with session.get(
                f"https://{ip}{path}",
                headers={"Authorization": _basic_auth(user, password)},
                ssl=ctx,
                allow_redirects=False,  # never forward the password elsewhere
            ) as resp:
                status = resp.status
                body = await _read_capped(resp) if status == 200 else ""
    except _CONNECT_ERRORS as err:
        _LOGGER.debug("local REST %s failed: %s", path, type(err).__name__)
        return LdiRestResponse(None)
    data: Any = None
    if body.strip():
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            data = None
    return LdiRestResponse(status, data)


async def ping(
    hass: HomeAssistant, ip: str, user: str, password: str
) -> LdiRestResponse:
    """GET /sh/ping (200 with an empty body when the camera is up)."""
    return await _get(hass, ip, user, password, PATH_PING)


async def get_version(
    hass: HomeAssistant, ip: str, user: str, password: str
) -> LdiRestResponse:
    """GET /sh/data/version -> {"firmwareVersion": str}."""
    return await _get(hass, ip, user, password, PATH_VERSION)


async def get_privacy_state(
    hass: HomeAssistant, ip: str, user: str, password: str
) -> LdiRestResponse:
    """GET /sh/data/privacyState -> {"privacyModeState": ON|OFF|...}."""
    return await _get(hass, ip, user, password, PATH_PRIVACY_STATE)


async def get_wifi_info(
    hass: HomeAssistant, ip: str, user: str, password: str
) -> LdiRestResponse:
    """GET /sh/data/wifiInfo -> {ssid, regionCode, linkQuality, ip, mac}."""
    return await _get(hass, ip, user, password, PATH_WIFI_INFO)


async def get_current_noise(
    hass: HomeAssistant, ip: str, user: str, password: str
) -> LdiRestResponse:
    """GET /sh/data/audio/currentNoise -> {"noiseLevel": int}."""
    return await _get(hass, ip, user, password, PATH_CURRENT_NOISE)


def privacy_on_from(data: object) -> bool | None:
    """True/False from a privacyState body, None when absent or transitional."""
    state = data.get("privacyModeState") if isinstance(data, dict) else None
    if state in ("ON", "TURNING_ON"):
        return True
    if state == "OFF":
        return False
    return None


def firmware_from(data: object) -> str | None:
    """Firmware string from a version body, None when absent or malformed."""
    value = data.get("firmwareVersion") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value.strip() else None


# Settings read locally for entity states (see `get_settings`). Keys are
# stable names the entities ask for; the JSON field names are the camera's.
SETTING_STATUS_LED = "status_led"
SETTING_RECORD_SOUND = "record_sound"
SETTING_GLASS_BREAK = "glass_break"
SETTING_FIRE_ALARM = "fire_alarm"
SETTING_INTRUSION_ENABLED = "intrusion_enabled"
SETTING_INTRUSION_SENSITIVITY = "intrusion_sensitivity"
SETTING_INTRUSION_DISTANCE = "intrusion_distance"
SETTING_ALARM_ENABLE = "alarm_enable"
SETTING_PRE_ALARM_ENABLE = "pre_alarm_enable"
SETTING_ALARM_DELAY = "alarm_delay"
SETTING_ALARM_ACTIVATION_DELAY = "alarm_activation_delay"
SETTING_PRE_ALARM_DELAY = "pre_alarm_delay"
SETTING_PRIVACY_SOUND = "privacy_sound"
SETTING_ICON_LED_BRIGHTNESS = "icon_led_brightness"

FIELD_OK = "ok"
FIELD_UNAVAILABLE = "unavailable"  # 404: the model has no such endpoint
FIELD_INVALID = "invalid"  # 200 but missing or wrongly typed value
FIELD_ERROR = "error"  # any other HTTP status

_KIND_BOOL = "bool"
_KIND_INT = "int"
_KIND_NUMBER = "number"

SETTINGS_ENDPOINTS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    ("/sh/data/statusLed", ((SETTING_STATUS_LED, "enable", _KIND_BOOL),)),
    (
        "/sh/data/recording/audioRecording",
        ((SETTING_RECORD_SOUND, "enable", _KIND_BOOL),),
    ),
    (
        "/sh/data/alarm/audioDetection/config",
        (
            (SETTING_GLASS_BREAK, "detectGlassBreak", _KIND_BOOL),
            (SETTING_FIRE_ALARM, "detectFireAlarm", _KIND_BOOL),
        ),
    ),
    (
        "/sh/data/intrusionSystem/config",
        (
            (SETTING_INTRUSION_ENABLED, "enabled", _KIND_BOOL),
            (SETTING_INTRUSION_SENSITIVITY, "sensitivity", _KIND_INT),
            (SETTING_INTRUSION_DISTANCE, "distance", _KIND_INT),
        ),
    ),
    (
        "/sh/data/alarm/config",
        (
            (SETTING_ALARM_ENABLE, "enable", _KIND_BOOL),
            (SETTING_PRE_ALARM_ENABLE, "enablePreAlarm", _KIND_BOOL),
            (SETTING_ALARM_DELAY, "alarmDelay", _KIND_NUMBER),
            (SETTING_ALARM_ACTIVATION_DELAY, "alarmActivationDelay", _KIND_NUMBER),
            (SETTING_PRE_ALARM_DELAY, "preAlarmDelay", _KIND_NUMBER),
        ),
    ),
    ("/sh/data/privacyMode/jingle", ((SETTING_PRIVACY_SOUND, "enable", _KIND_BOOL),)),
    (
        "/sh/data/lighting/iconLed/brightness",
        ((SETTING_ICON_LED_BRIGHTNESS, "value", _KIND_INT),),
    ),
)

LDI_SETTINGS_DEADLINE = 20.0


@dataclass(frozen=True)
class LdiSetting:
    """One field of a settings read; `value` is set only when status is ok."""

    status: str
    value: bool | int | float | None = None


@dataclass(frozen=True)
class LdiSettings:
    """Outcome of `get_settings`.

    `result` is a PROBE_* class for the whole batch: only PROBE_OK carries
    fields. `missing_paths` are the endpoints that answered 404.
    """

    result: str
    fields: dict[str, LdiSetting]
    missing_paths: frozenset[str] = frozenset()


def _typed(value: object, kind: str) -> bool | int | float | None:
    """`value` when it has the expected JSON type (bool is no number here)."""
    if isinstance(value, bool):
        return value if kind == _KIND_BOOL else None
    if kind == _KIND_INT and isinstance(value, int):
        return value
    if kind == _KIND_NUMBER and isinstance(value, int | float):
        try:
            return value if math.isfinite(value) else None  # json allows NaN
        except OverflowError:  # int too large for float
            return None
    return None


def _fields_from(
    resp: LdiRestResponse, specs: tuple[tuple[str, str, str], ...]
) -> dict[str, LdiSetting]:
    if resp.unavailable:
        return {key: LdiSetting(FIELD_UNAVAILABLE) for key, _, _ in specs}
    if not resp.ok:
        return {key: LdiSetting(FIELD_ERROR) for key, _, _ in specs}
    body = resp.data if isinstance(resp.data, dict) else {}
    out: dict[str, LdiSetting] = {}
    for key, field, kind in specs:
        value = _typed(body.get(field), kind)
        out[key] = (
            LdiSetting(FIELD_INVALID) if value is None else LdiSetting(FIELD_OK, value)
        )
    return out


async def get_settings(
    hass: HomeAssistant,
    ip: str,
    user: str,
    password: str,
    *,
    skip: frozenset[str] = frozenset(),
) -> LdiSettings:
    """Read the settings entities show, one endpoint after the other.

    A 404 or a bad body affects only its own fields. A rejected password or a
    camera that stops answering ends the batch: the result then carries no
    fields, so nothing half-read is mistaken for the camera's state. `skip`
    paths (known 404s) are not asked and reported unavailable. Never raises.
    """
    fields: dict[str, LdiSetting] = {}
    missing: set[str] = set()
    try:
        async with asyncio.timeout(LDI_SETTINGS_DEADLINE):
            for path, specs in SETTINGS_ENDPOINTS:
                if path in skip:
                    fields.update(
                        {key: LdiSetting(FIELD_UNAVAILABLE) for key, _, _ in specs}
                    )
                    continue
                resp = await _get(hass, ip, user, password, path)
                if resp.unauthorized:
                    return LdiSettings(PROBE_AUTH, {})
                if resp.unreachable:
                    return LdiSettings(PROBE_UNREACHABLE, {})
                if resp.unavailable:
                    missing.add(path)
                fields.update(_fields_from(resp, specs))
    except TimeoutError:
        _LOGGER.debug("local REST settings read exceeded its deadline")
        return LdiSettings(PROBE_UNREACHABLE, {})
    return LdiSettings(PROBE_OK, fields, frozenset(missing))


def _classify(resp: LdiRestResponse) -> str:
    if resp.ok:
        return PROBE_OK
    if resp.unauthorized:
        return PROBE_AUTH
    if resp.unreachable:
        return PROBE_UNREACHABLE
    return PROBE_ERROR


async def probe_camera(
    hass: HomeAssistant,
    ip: str,
    user: str,
    password: str,
    *,
    want_version: bool = False,
) -> LdiProbe:
    """Reachability, password check and privacy state in one cheap request.

    The privacy state endpoint answers all three. A model without it (404)
    is checked with /sh/ping instead. The firmware is read only on request.
    """
    resp = await get_privacy_state(hass, ip, user, password)
    privacy_on: bool | None = None
    if resp.unavailable:
        resp = await ping(hass, ip, user, password)
    elif resp.ok:
        privacy_on = privacy_on_from(resp.data)
    result = _classify(resp)
    firmware: str | None = None
    if result == PROBE_OK and want_version:
        firmware = firmware_from((await get_version(hass, ip, user, password)).data)
    return LdiProbe(result, privacy_on, firmware)
