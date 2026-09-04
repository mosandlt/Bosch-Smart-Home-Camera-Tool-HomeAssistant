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
import base64
import binascii
import contextlib
import json
import logging
import os
import secrets
import ssl
import time
import urllib.parse
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

import aiohttp
from bosch_shc_camera_client.media_transfer import (
    is_safe_bosch_url as _is_safe_bosch_url,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .cloud_ssl import async_get_bosch_cloud_session
from .const import DOMAIN
from .recorder import maybe_schedule_nvr_motion_clip
from .snapshot_store import save_snapshot
from .time_utils import parse_bosch_timestamp

# `_is_safe_bosch_url` was previously a byte-identical copy duplicated across
# fcm.py/smb.py/coordinator.py — now a single shared implementation in
# bosch_shc_camera_client.media_transfer, aliased here to keep the private
# name every call site in this file already uses.

# Max bytes accepted for an alert video clip download (Area 5, item 3 bug
# fix). 100 MB is generous for a Mini-NVR/Bosch event clip (typically low
# single-digit MB) while still guarding against a malformed/oversized
# response allocating unboundedly in the event-loop process.
_CLIP_MAX_BYTES = 100 * 1024 * 1024

# Event types that carry image data and warrant a live-snapshot refresh (Path A).
# Status-only types (connectivity events) are excluded — they carry no image
# data and the camera view hasn't changed. Hoisted to module level so it isn't
# rebuilt on every event-fetch pass.
_SNAP_EVENT_TYPES = frozenset(
    {"MOVEMENT", "PERSON", "VEHICLE", "ANIMAL", "AUDIO_ALARM", "BABY_CRY"}
)


def _safe_path_segment(seg: str) -> str:
    """Neutralise path-traversal in a filename segment.

    The alert snapshot filename embeds the cloud-provided camera title, which
    must never be able to escape the alert directory (e.g. a camera named
    "../../config/secrets"). Strips path separators and parent-dir tokens.
    """
    return str(seg).replace("/", "_").replace("\\", "_").replace("..", "_")


if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CLOUD_API = "https://residential.cbs.boschsecurity.com"

# Supervisor backoff ladder (seconds). The supervisor task waits this long
# between failed listener start/restart attempts. Step 0 (5 s) covers a
# transient connection drop after a push was received; later steps handle
# persistent Google registration problems. Resets to 0 after a successful
# push arrives so a quick recovery doesn't block the next outage detection.
FCM_SUPERVISOR_BACKOFF_SEC: tuple[float, ...] = (
    5.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    1800.0,
)

# How often the supervisor polls is_started() while the listener is running.
# 10 s means a listener death is detected within 10 s, not 60 s (the old
# coordinator-tick cadence). Short enough to be reactive; long enough to avoid
# spinning the event loop.
FCM_SUPERVISOR_POLL_SEC = 10.0

# After this many consecutive soft-restarts WITHOUT a real push arriving, the
# next restart escalates to a hard-heal (credential purge + re-register).
FCM_SUPERVISOR_SOFT_HEAL_MAX = 3

# How long a listener must stay continuously up (is_started()=True) before the
# `failures` backoff counter is reset just from uptime, even with zero pushes
# received — a quiet house shouldn't keep the backoff ladder pinned at a
# stale escalated value forever. 10 min matches the creds-staleness window
# used elsewhere in this file.
FCM_SUPERVISOR_SUSTAINED_UPTIME_SEC = 600.0

# Proactive Bosch-CBS re-registration cadence. Without this, the integration
# would skip the POST /v11/devices forever as long as the FCM token was
# unchanged. If Bosch drops the device registration server-side (FW upgrade,
# re-pair, or an undocumented TTL) while our token stays the same, push
# delivery silently dies and nothing ever re-announces us. A real phone app
# re-registers on every launch; we re-POST at least this often (wall-clock,
# persisted in `fcm_registered_at`) even when the token is unchanged, matching
# that behaviour.
FCM_REREGISTER_INTERVAL_SEC = 7 * 24 * 3600  # 7 days


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
      2. De-duplicate: at most one pass-through per 300 s (5 min) window.
         The library's reconnect cadence is ~63 s on a permanently-broken
         SSL session (upstream `_reset()` retry loop). A 60 s window would
         let every retry through; 300 s gives a heartbeat without flooding
         and matches what the watchdog needs to flip to polling-fallback.
    """

    _DEDUP_WINDOW_SECONDS = 300.0
    _SHARED_STALENESS_TIMESTAMPS: ClassVar[
        list[float]
    ] = []  # only creds-rejection markers

    # Credential-rejection markers: Google's gcm_register() endpoint returned
    # PHONE_REGISTRATION_ERROR (only path that emits this — see
    # firebase_messaging/fcmregister.py). Reaches us only when the library
    # falls through to gcm_register() because gcm_check_in(android_id,
    # security_token) failed or no credentials were persisted. Presence in the
    # log window is the authoritative signal that credentials are actually
    # stale — that's when a hard-heal (purge + fresh register) is warranted.
    _CREDS_STALENESS_MARKERS = (
        "PHONE_REGISTRATION_ERROR",  # GCM auth rejected
        "Unable to complete gcm auth request",  # final-give-up after PHONE_REGISTRATION_ERROR retries
        "Unable to establish subscription",  # fcm.py's wrapper for the above
    )

    # Connectivity-loop marker: WAN blip / SSL reset. Tracked only for log
    # deduplication — NOT used for health decisions (the supervisor detects
    # listener death via is_started()=False, so error counting is unnecessary).
    _CONNECTIVITY_MARKERS = (
        "Unexpected exception during read",  # library reconnect loop
    )

    _FAILURE_MARKERS = _CONNECTIVITY_MARKERS + _CREDS_STALENESS_MARKERS

    def __init__(self) -> None:
        super().__init__()
        # Separate dedup clocks per failure class (bug fix): connectivity
        # noise and creds-staleness diagnostics are unrelated failure
        # classes. A shared clock let a connectivity WARNING suppress a
        # later creds-rejection ERROR (or vice versa) even though neither
        # is a duplicate of the other.
        self._last_passed_connectivity = float("-inf")
        self._last_passed_creds = float("-inf")

    def filter(self, record: logging.LogRecord) -> bool:
        # Only target known failure markers; other firebase_messaging logs
        # (INFO start/stop, debug traces) pass through untouched so we keep
        # diagnostic visibility.
        msg = record.getMessage() if hasattr(record, "getMessage") else str(record.msg)
        is_creds = any(marker in msg for marker in self._CREDS_STALENESS_MARKERS)
        is_connectivity = any(marker in msg for marker in self._CONNECTIVITY_MARKERS)
        if not (is_creds or is_connectivity):
            return True
        now = time.monotonic()
        if is_creds:
            # Track creds-rejection markers so the supervisor can decide
            # soft (preserve creds) vs hard (purge + re-register) —
            # PHONE_REGISTRATION_ERROR in the window means creds are
            # genuinely stale, otherwise it's a connectivity-only blip.
            self._SHARED_STALENESS_TIMESTAMPS.append(now)
            del self._SHARED_STALENESS_TIMESTAMPS[:-10]
            # Creds-staleness lines are a once-per-give-up diagnostic, not
            # the multi-thousand-line recursive connectivity trace this
            # filter exists to tame — keep their traceback intact.
            if (now - self._last_passed_creds) < self._DEDUP_WINDOW_SECONDS:
                return False
            self._last_passed_creds = now
            return True
        # Connectivity noise: drop the multi-thousand-line traceback
        # unconditionally — the message itself is the diagnostic, the
        # trace is library-internal recursion that doesn't help triage.
        record.exc_info = None
        record.exc_text = None
        if (now - self._last_passed_connectivity) < self._DEDUP_WINDOW_SECONDS:
            return False
        self._last_passed_connectivity = now
        return True


def get_recent_fcm_creds_staleness_count(window_seconds: float = 600.0) -> int:
    """How many `PHONE_REGISTRATION_ERROR`-class markers fired in the
    last ``window_seconds``.

    The two-stage self-heal uses this to decide soft vs hard:
      - count == 0 → creds likely valid, try soft-heal first (no purge)
      - count >= 1 → creds genuinely rejected by Google, hard-heal (purge + register)

    Default window 600 s (10 min) is wide enough to catch the prior
    failure-storm but narrow enough that an old incident doesn't poison a
    fresh outage hours later.
    """
    if not _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS:
        return 0
    cutoff = time.monotonic() - window_seconds
    return sum(1 for ts in _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS if ts >= cutoff)


def reset_fcm_creds_staleness_counter() -> None:
    """Clear the creds-staleness timestamp list after a hard-heal registration."""
    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()


def reset_fcm_error_counter() -> None:
    """Backward-compat shim used by tests; delegates to reset_fcm_creds_staleness_counter."""
    reset_fcm_creds_staleness_counter()


async def async_start_fcm_push(coordinator: Any) -> None:
    """Backward-compat shim; production code uses async_ensure_fcm_supervisor.

    Lazy-inits fcm_start_lock, then delegates to _async_start_fcm_push_locked.
    Used by legacy tests that target the inner lock-and-start logic directly.
    """
    lock = getattr(coordinator, "fcm_start_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        coordinator.fcm_start_lock = lock
    async with lock:
        await _async_start_fcm_push_locked(coordinator)


def _install_fcm_noise_filter() -> None:
    """Install the noise filter on all relevant loggers once.

    Three loggers matter here:
      1. ``firebase_messaging.fcmpushclient`` — the vanilla library path (when
         ``_QuietFcmPushClient`` is not used or the patch falls back); emits
         "Unexpected exception during read" connectivity noise.
      2. ``firebase_messaging.fcmregister`` — where ``gcm_register()`` /
         ``gcm_check_in()`` actually log ``PHONE_REGISTRATION_ERROR`` and the
         other _CREDS_STALENESS_MARKERS (GitHub #68). This is a SIBLING of
         ``fcmpushclient`` in the logger hierarchy, not a descendant — a
         `logging.Filter` attached to one logger is only consulted for
         records that logger itself emits, never for a sibling's records
         propagating through a shared ancestor. Without this, staleness
         markers from the real failure path were silently never recorded and
         ``get_recent_fcm_creds_staleness_count()`` stayed 0 forever in
         production.
      3. ``custom_components.bosch_shc_camera.fcm`` (``_LOGGER``) — the
         ``_QuietFcmPushClient._listen()`` override also logs via ``_LOGGER``
         in its fallback ``else`` branch (for non-ConnectionReset OSErrors or
         if the run_state guard does not fire).

    A single shared ``_FCMNoiseFilter`` instance is installed on all three so
    ``_last_passed`` and ``_SHARED_STALENESS_TIMESTAMPS`` are identical
    regardless of which logger emits the record — the 300 s dedup window
    spans all three sources.

    Idempotent: re-running finds the existing instance and returns early.
    """
    # Find or create the shared filter instance. Bug fix: the original
    # idempotency guard only inspected `lib_logger.filters` — if HA's
    # `logger` integration reload cleared filters asymmetrically (e.g. only
    # the library logger, not the other two), a second _FCMNoiseFilter
    # instance got installed on the still-populated loggers, double-counting
    # _SHARED_STALENESS_TIMESTAMPS. Check all three loggers up front instead.
    lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
    register_logger = logging.getLogger("firebase_messaging.fcmregister")
    loggers = (lib_logger, register_logger, _LOGGER)

    shared_filter: _FCMNoiseFilter | None = None
    for logger_ in loggers:
        for f in logger_.filters:
            if isinstance(f, _FCMNoiseFilter):
                shared_filter = f
                break
        if shared_filter is not None:
            break

    if shared_filter is None:
        shared_filter = _FCMNoiseFilter()

    for logger_ in loggers:
        if shared_filter not in logger_.filters:
            logger_.addFilter(shared_filter)


def _urlsafe_b64decode_padded(data: str) -> bytes:
    """Decode urlsafe base64 that may be missing its '=' padding.

    Webpush keys and the crypto-key/salt headers (RFC 8291) are transmitted
    without padding; Python's ``urlsafe_b64decode`` requires it and raises
    ``binascii.Error`` on unpadded input. Root cause of GitHub #68's delayed
    notifications: matches upstream sdb9696/firebase-messaging#37 (open,
    unmerged as of 2026-08).
    """
    return base64.urlsafe_b64decode(data.encode("ascii") + b"=" * (-len(data) % 4))


def _decode_message_header(data: str) -> bytes:
    """Decode a per-message crypto-key/salt header (GitHub #68 live-deploy
    finding, 2026-08-18).

    ``crypto-key``/``encryption`` are proto3 ``string`` fields, so RFC
    8291-legal content can legitimately include non-ASCII bytes — but
    ``_urlsafe_b64decode_padded``'s ``.encode("ascii")`` raises
    ``UnicodeEncodeError`` (a ``ValueError`` subclass, not a
    ``binascii.Error``) for those, which isn't in ``_listen()``'s
    ``skip_exceptions`` and would otherwise escape to the broad ``except
    Exception`` and terminate the whole client over one bad message — the
    same crash-loop class as the "Invalid EC key." incident, just triggered
    by a different malformed header. Normalize to ``binascii.Error`` so any
    header-decode failure is uniformly treated as a single-message fault.
    """
    try:
        return _urlsafe_b64decode_padded(data)
    except UnicodeEncodeError as ex:
        raise binascii.Error(str(ex)) from ex


def _decode_credential_material(data: str) -> bytes:
    """Decode OUR OWN stored credential material (private/secret keys)
    (GitHub #68 live-deploy finding, 2026-08-18).

    Uses the same padding math as ``_decode_message_header``, but a decode
    failure here means our STORED credentials are corrupt — a client-wide
    fault that must propagate to trigger the supervisor's hard-heal, never
    be silently skip-and-acked forever like a single bad message. Without
    this, a credential whose base64 length happened to land on ``% 4 == 1``
    raised ``binascii.Error`` — which IS in ``skip_exceptions`` — so every
    push was silently skipped-and-acked with zero self-recovery, permanently
    degrading to the slow poll fallback. Re-raising as a plain ``ValueError``
    matches what ``load_der_private_key`` already raises for other forms of
    credential corruption, so both are handled identically downstream.
    """
    try:
        return _urlsafe_b64decode_padded(data)
    except (binascii.Error, UnicodeEncodeError) as ex:
        raise ValueError(f"corrupt stored credential material: {ex}") from ex


_DecryptRawData = Callable[[dict[str, dict[str, str]], str, str, bytes], bytes]


def _build_decrypt_raw_data_override(
    fcm_push_client_cls: type,
) -> tuple[_DecryptRawData | None, tuple[type[Exception], ...]]:
    """Build the GitHub #68 padded-decrypt override, plus the tuple of
    exception types ``_listen()`` should treat as "skip this one message".

    Returns ``(override_or_None, skip_exceptions)``. Deliberately independent
    of the ``_listen`` signature guard in ``_patch_class()``: if a future
    firebase-messaging upgrade renames/reshapes ``_decrypt_raw_data``, only
    this override degrades (falls back to the upstream, unpadded version) —
    the issue #33 ``_listen`` fix must keep working regardless. ``skip_exceptions``
    is likewise built independently of whether the override itself succeeds:
    even the vanilla (unpadded) ``_decrypt_raw_data`` can still reach
    ``http_ece.decrypt()`` and raise ``ECEException`` whenever a message's
    crypto-key/salt headers happen to already be a multiple of 4 in length
    (no padding needed) — worth catching either way.
    """
    skip_exceptions: tuple[type[Exception], ...] = (binascii.Error,)
    try:
        from http_ece import ECEException
    except ImportError:
        _LOGGER.debug(
            "FCM subclass: http_ece unavailable — GitHub #68 padding fix not "
            "applied (issue #33 fix still active)"
        )
        return None, skip_exceptions
    skip_exceptions = (binascii.Error, ECEException)

    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import (
            load_der_private_key,
        )
        from http_ece import decrypt as http_ece_decrypt
    except ImportError:
        _LOGGER.debug(
            "FCM subclass: cryptography unavailable — GitHub #68 padding fix "
            "not applied (issue #33 fix still active)"
        )
        return None, skip_exceptions

    import inspect

    decrypt_method = getattr(fcm_push_client_cls, "_decrypt_raw_data", None)
    if decrypt_method is None or list(inspect.signature(decrypt_method).parameters) != [
        "credentials",
        "crypto_key_str",
        "salt_str",
        "raw_data",
    ]:
        _LOGGER.debug(
            "FCM subclass: upstream _decrypt_raw_data() missing or its "
            "signature changed — GitHub #68 padding fix not applied "
            "(issue #33 fix still active)"
        )
        return None, skip_exceptions

    def _decrypt_raw_data(
        credentials: dict[str, dict[str, str]],
        crypto_key_str: str,
        salt_str: str,
        raw_data: bytes,
    ) -> bytes:
        """Decrypt an FCM data message, tolerating unpadded base64.

        Root cause of GitHub #68's delayed notifications: the upstream
        version decodes crypto_key_str/salt_str/the stored private+secret
        keys with plain ``urlsafe_b64decode``, which raises
        ``binascii.Error`` on RFC-8291-legal unpadded input. The caller
        (``_handle_data_message``) lets that propagate, and this
        integration's own ``_listen`` override (issue #65) only
        caught-and-skipped it — silently dropping the push and leaving
        the event to surface ~2-3 minutes later via the coordinator's
        slower poll fallback instead of near-instantly via push. Padding
        correctly here lets decryption actually succeed, matching
        upstream PR sdb9696/firebase-messaging#37 (open, unmerged as of
        2026-08).
        """
        crypto_key = _decode_message_header(crypto_key_str)
        salt = _decode_message_header(salt_str)
        der_data = _decode_credential_material(credentials["keys"]["private"])
        secret = _decode_credential_material(credentials["keys"]["secret"])
        # load_der_private_key() failing here means our OWN stored
        # credentials are corrupt — a client-wide fault, deliberately left
        # unguarded so its ValueError propagates to _listen()'s broad
        # except and triggers the supervisor's hard-heal.
        privkey = load_der_private_key(
            der_data, password=None, backend=default_backend()
        )
        # GitHub #68 live-deploy finding: http_ece.decrypt()'s own EC point
        # parsing of THIS MESSAGE's crypto-key bytes (inside derive_dh,
        # called deep within decrypt()) is NOT wrapped into ECEException the
        # way the AEAD/tag-mismatch path is — it raises a raw
        # ValueError("Invalid EC key.") straight out of the cryptography
        # library whenever a single message's (correctly padded,
        # successfully decoded) crypto-key bytes don't represent a valid
        # point on the curve (e.g. a subtype-mismatched message). Google MCS
        # then redelivers that exact poisoned message on every reconnect
        # since it's never acked, and — observed live — this crashed the
        # whole FcmPushClient on every single redelivery, escalating through
        # the hard-heal backoff for hours despite the fault being a single
        # bad message, not our credentials (which just loaded successfully
        # above). Pre-parse the point ourselves and convert ONLY that
        # failure to ECEException — deliberately NOT a broad try/except
        # around the whole decrypt() call below, which would also catch
        # ValueError from decrypt()'s OTHER internal calls that use OUR
        # stored private key (e.g. a non-EC or wrong-curve stored key
        # raising "format is invalid with this key" / "Error computing
        # shared key.") and silently mask that as a skippable one-off
        # message forever instead of a client-wide fault needing hard-heal.
        try:
            crypto_key_point = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), crypto_key
            )
        except ValueError as ex:
            raise ECEException(str(ex)) from ex

        decrypted: bytes = http_ece_decrypt(
            raw_data,
            salt=salt,
            private_key=privkey,
            dh=crypto_key_point,
            version="aesgcm",
            auth_secret=secret,
        )
        return decrypted

    return _decrypt_raw_data, skip_exceptions


def _extract_crypto_header(raw: str, prefix: str) -> str:
    """Extract a per-message crypto-key/salt header's value, tolerating the
    real-world header shapes upstream's blind slice gets wrong.

    Matches upstream sdb9696/firebase-messaging#42 + #44 (both open,
    unmerged as of 2026-08). Upstream's ``_handle_data_message`` does
    ``header[3:]``/``header[5:]`` assuming the ``crypto-key``/``encryption``
    header ALWAYS starts with exactly ``"dh="``/``"salt="``, is the FIRST
    (and only) ``;``-separated segment, and matches case-exactly. None of
    that is guaranteed:
    - ``Crypto-Key``/``Encryption`` are ``;``-separated parameter lists AND
      (per RFC 8188's ABNF) ``,``-separated element lists — the wanted
      parameter (``dh=``/``salt=``) is not required to come first; a VAPID
      ``p256ecdsa=`` segment or an RFC-8188 ``rs=``/``keyid=`` parameter can
      legally precede it in either separator form (#44's reported shape and
      its generalizations). A first-segment-only scan silently returns the
      WRONG value instead of the real key/salt bytes.
    - HTTP header parameter names are case-insensitive; a producer emitting
      ``DH=``/``Salt=`` is legal and upstream's positional slice tolerates
      it, so a literal-case-only match here would be a regression versus
      upstream, not just an incomplete fix (#42's more general framing).
    - Whitespace can surround a ``;``-segment; after removing the prefix,
      leftover whitespace between ``=`` and the value would throw off the
      padding math in :func:`_decode_message_header` (base64 length
      computed including a space `a2b_base64` itself discards).
    All three corrupt the extracted bytes just enough to still base64-decode
    but fail EC-point validation, surfacing live here as "Invalid EC key."
    — our own padding/skip fixes above then correctly skip the message
    instead of crashing the client, but the message is lost for no reason:
    it was decryptable all along. Scans every segment across both
    separators for a case-insensitive prefix match; falls back to the whole
    (stripped) raw string, unmodified, if no segment matches at all —
    passing an unexpected shape through rather than guessing.
    """
    for element in raw.split(","):
        for segment in element.split(";"):
            stripped = segment.strip()
            if stripped.lower().startswith(prefix.lower()):
                return stripped[len(prefix) :].strip()
    return raw.strip()


def _build_handle_data_message_override(
    fcm_push_client_cls: type,
) -> Callable[[Any, Any], None] | None:
    """Build the corrected ``_handle_data_message()`` override, or ``None``
    if the upstream signature no longer matches what this replicates.

    Deliberately independent of the ``_listen``/``_decrypt_raw_data``
    overrides: if this degrades, both of those keep working. The method
    body below is byte-identical to the installed ``firebase_messaging``
    version except the ``crypto_key``/``salt`` extraction lines, which use
    :func:`_extract_crypto_header` instead of upstream's blind
    ``header[3:]``/``header[5:]`` slice (see its docstring).

    Unlike ``_build_decrypt_raw_data_override`` (a leaf function guarded by
    its own signature alone), the replicated body here also calls SIX other
    private upstream methods (``_app_data_by_key``, ``_log_warn_with_limit``,
    ``_log_verbose``, ``_reset_error_count``, ``_try_increment_error_count``,
    ``_decrypt_raw_data``) that a future ``firebase_messaging`` release could
    rename/reshape/remove independently of ``_handle_data_message``'s own
    signature — the guard below checks all of them are still present and
    callable, not just the entry point, so an upstream change elsewhere in
    that dependency set can't silently attach a body that AttributeErrors at
    runtime (which would escape ``_listen``'s narrow ``skip_exceptions`` and
    reintroduce the exact 2026-08-18 crash-loop class on a path that
    couldn't fail that way before this override existed). Also rejects an
    upstream ``_handle_data_message`` that became a coroutine function — our
    replica is deliberately sync (matching every 0.4.x release), and
    installing a sync override under a caller that now ``await``s the
    result would TypeError on the very next message.
    """
    import inspect

    handler = getattr(fcm_push_client_cls, "_handle_data_message", None)
    required_helpers = (
        "_app_data_by_key",
        "_log_warn_with_limit",
        "_log_verbose",
        "_reset_error_count",
        "_try_increment_error_count",
        "_decrypt_raw_data",
    )
    if (
        handler is None
        or inspect.iscoroutinefunction(handler)
        or list(inspect.signature(handler).parameters) != ["self", "msg"]
        or not all(
            callable(getattr(fcm_push_client_cls, name, None))
            for name in required_helpers
        )
    ):
        _LOGGER.debug(
            "FCM subclass: upstream _handle_data_message() missing, async, "
            "its signature changed, or a helper it depends on is missing — "
            "crypto-key/salt header extraction fix not applied "
            "(padding/skip fixes still active)"
        )
        return None

    try:
        from firebase_messaging.fcmpushclient import ErrorType as _ErrorType
    except ImportError:
        _LOGGER.debug(
            "FCM subclass: firebase_messaging.ErrorType unavailable — "
            "crypto-key/salt header extraction fix not applied"
        )
        return None

    def _handle_data_message(self: Any, msg: Any) -> None:
        _LOGGER.debug(
            "Received data message Stream ID: %s, Last: %s, Status: %s",
            msg.stream_id,
            msg.last_stream_id_received,
            msg.status,
        )

        if (
            self._app_data_by_key(msg, "message_type", do_not_raise=True)
            == "deleted_messages"
        ):
            # The deleted_messages message does not contain data.
            return
        crypto_key = _extract_crypto_header(
            self._app_data_by_key(msg, "crypto-key"), "dh="
        )
        salt = _extract_crypto_header(self._app_data_by_key(msg, "encryption"), "salt=")
        subtype = self._app_data_by_key(msg, "subtype")
        if TYPE_CHECKING:
            assert self.credentials
        if subtype != self.credentials["gcm"]["app_id"]:
            self._log_warn_with_limit(
                "Subtype %s in data message does not match"
                + "app id client was registered with %s",
                subtype,
                self.credentials["gcm"]["app_id"],
            )
        if not self.credentials:  # pragma: no cover — self.credentials[...] above
            # already dereferences unconditionally; a falsy self.credentials
            # crashes there first (matches upstream's own identical ordering,
            # replicated as-is — not this fix's scope to change).
            return
        decrypted = self._decrypt_raw_data(
            self.credentials, crypto_key, salt, msg.raw_data
        )
        decrypted_json = None
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            decrypted_json = json.loads(decrypted.decode("utf-8"))

        if not decrypted_json:
            self._log_warn_with_limit(
                "Failed to decrypt data for message %s", msg.persistent_id
            )

        ret_val = decrypted_json if decrypted_json else decrypted
        self._log_verbose("Data for message %s is: %s", msg.persistent_id, ret_val)
        try:
            if not isinstance(ret_val, dict):
                ret_val = {"message": ret_val}
            self.callback(ret_val, msg.persistent_id, self.callback_context)
            self._reset_error_count(_ErrorType.NOTIFY)
        except Exception:
            _LOGGER.exception("Unexpected exception calling notification callback\n")
            self._try_increment_error_count(_ErrorType.NOTIFY)

    return _handle_data_message


class _QuietFcmPushClient:
    """FcmPushClient subclass that fixes the upstream state-machine bug described in
    github.com/sdb9696/firebase-messaging#33.

    Root cause (b — state-machine bug):
      In the library's ``_listen()`` while-loop, when an ``OSError``/``EOFError``
      is caught the existing quiet-path check is:

          if (isinstance(osex, (ConnectionResetError, TimeoutError, ...))
              and self.run_state == FcmPushClientRunState.RESETTING):
              <log quietly>
          else:
              _logger.exception("Unexpected exception during read\\n")  # ← noise

      ``run_state`` is only set to ``RESETTING`` *inside* ``_reset()``, which is
      called **after** the logging decision.  So the very first connectivity error
      always takes the loud ``_logger.exception`` path, even though the connection
      is about to be gracefully reset.  On a permanent WAN outage the library
      re-enters this path every ~63 s producing one error (or many thousands of
      lines without ``_FCMNoiseFilter``).

    Fix: override ``_listen()`` and set ``self.run_state = RESETTING`` immediately
    on catching the OS error, **before** the existing quiet-path check fires.  This
    makes the check evaluate to True on the first error, routing to the verbose
    (INFO-level) path instead of ``_logger.exception``.  The rest of the method body
    — including the call to ``_reset()`` — is byte-identical to the library version
    so no happy-path behaviour changes.

    Import-time guard: the class body is only evaluated when ``firebase_messaging``
    is importable (inside ``async_start_fcm_push``).  If the import fails we fall
    back to the vanilla ``FcmPushClient`` transparently.
    """

    # _make() is called inside async_start_fcm_push after a successful import so
    # the try/except there already handles ImportError.
    @staticmethod
    def _patch_class() -> type | None:
        """Return a patched FcmPushClient subclass, or None if the library is too
        new/old for safe subclassing (i.e. ``_listen`` signature changed)."""
        try:
            from firebase_messaging import FcmPushClient, FcmPushClientRunState
        except ImportError:
            return None

        try:
            return _QuietFcmPushClient._build_patched_class(
                FcmPushClient, FcmPushClientRunState
            )
        except Exception:
            # Defense-in-depth: any unexpected failure while building the
            # patched subclass (e.g. inspect.signature() raising ValueError
            # on a future library's callable shape, or a genuinely malformed
            # library internals) must degrade to the vanilla FcmPushClient,
            # matching every other guard in this class — not re-raise and
            # break the supervisor on every cycle.
            _LOGGER.exception(
                "FCM subclass: unexpected error building patched FcmPushClient — "
                "falling back to vanilla FcmPushClient (issue#33 noise may recur)"
            )
            return None

    @staticmethod
    def _build_patched_class(
        FcmPushClient: Any, FcmPushClientRunState: Any
    ) -> type | None:
        """Build the patched subclass. Raises on unexpected internal failures;
        callers (``_patch_class``) are responsible for the broad fallback."""
        import inspect

        # Safety guard: if the upstream _listen() signature ever changes (e.g.
        # gains a parameter) we must not silently break it.  Fall back to vanilla
        # if the signature is unexpected.
        sig = inspect.signature(FcmPushClient._listen)
        if list(sig.parameters) != ["self"]:
            _LOGGER.debug(
                "FCM subclass: upstream _listen() signature changed — "
                "falling back to vanilla FcmPushClient (issue#33 noise may recur)"
            )
            return None

        # The GitHub #68 padded-decrypt override is independent of the _listen
        # fix above and deliberately NOT allowed to gate it: if a future
        # firebase-messaging upgrade renames/reshapes _decrypt_raw_data, we still
        # want the issue #33 fix (and the skip-one-message safety net) to keep
        # working — only the #68 padding improvement itself should degrade, not
        # the whole patched class. See _build_decrypt_raw_data_override().
        decrypt_override, skip_exceptions = _build_decrypt_raw_data_override(
            FcmPushClient
        )

        # Likewise independent of the two overrides above: fixes upstream's
        # blind crypto-key/salt header slice (sdb9696/firebase-messaging#42
        # + #44). See _build_handle_data_message_override().
        handle_data_message_override = _build_handle_data_message_override(
            FcmPushClient
        )

        class _Patched(FcmPushClient):  # type: ignore[misc]
            """FcmPushClient with the run_state-before-log fix for issue #33 and
            (when decrypt_override is not None) the padded-base64-decode fix for
            GitHub #68 / upstream sdb9696/firebase-messaging#37."""

            async def _listen(self) -> None:
                """Override _listen to set RESETTING state before the error-log decision.

                Identical to upstream except for the single line that sets
                ``self.run_state = FcmPushClientRunState.RESETTING`` at the top of
                the ``except (OSError, EOFError)`` handler.  This makes the
                existing quiet-path check pass on the very first connectivity error,
                routing to INFO-level logging instead of ``_logger.exception``.
                """
                if not await self._connect_with_retry():
                    return

                try:
                    await self._login()

                    while self.do_listen:
                        try:
                            if self.run_state == FcmPushClientRunState.RESETTING:  # type: ignore[has-type]  # external FcmPushClient attr (untyped base)
                                await asyncio.sleep(1)
                            elif msg := await self._receive_msg():

                                async def _skip_and_ack(exc: Exception) -> None:
                                    # persistent_id logged alongside the error
                                    # (GitHub #68 follow-up, 2026-08-18): a
                                    # DISTINCT id each occurrence means this is
                                    # a genuinely new message from Bosch's
                                    # cloud each time (their bug, harmless
                                    # here regardless) — the SAME id repeating
                                    # would instead mean our ack below isn't
                                    # durably reaching Google, worth
                                    # investigating further if seen.
                                    #
                                    # Logged via our OWN module logger, not
                                    # self._log_warn_with_limit() (GitHub #68
                                    # live-deploy finding, 2026-08-18):
                                    # upstream's rate limiter caps at
                                    # config.log_warn_limit (default 5)
                                    # occurrences PER FORMAT STRING PER
                                    # FcmPushClient INSTANCE, never reset on
                                    # reconnect — so the diagnostic added
                                    # specifically to answer "does this same
                                    # id keep recurring?" went silent after
                                    # the 5th skip, for the rest of that
                                    # client's lifetime, defeating its own
                                    # purpose.
                                    persistent_id = getattr(msg, "persistent_id", None)
                                    # Rate-limited (not self._log_warn_with_limit(),
                                    # deliberately — see the comment above this
                                    # method about that helper's own 5-occurrence-
                                    # forever cap): this can fire for routine
                                    # non-webpush traffic on every reconnect, so an
                                    # unbounded warning here would be its own noise
                                    # source. A time-windowed limiter (like
                                    # _FCMNoiseFilter's dedup) naturally recovers
                                    # after a quiet period instead of going silent
                                    # forever after N occurrences.
                                    _skip_warn_now = time.monotonic()
                                    _last_skip_warn = getattr(
                                        self, "_bosch_skip_warn_last_ts", float("-inf")
                                    )
                                    if (_skip_warn_now - _last_skip_warn) >= 300.0:
                                        self._bosch_skip_warn_last_ts = _skip_warn_now
                                        _LOGGER.warning(
                                            "Skipping undecryptable FCM push message "
                                            "(id=%s): %s",
                                            persistent_id,
                                            exc,
                                        )
                                    # Mark it delivered anyway. Upstream's
                                    # _handle_message() only appends to
                                    # persistent_ids / sends the selective ack
                                    # AFTER _handle_data_message() returns
                                    # (fcmpushclient.py) — our exception above
                                    # aborted before either ran, so without
                                    # this the message is never acked and
                                    # Google MCS redelivers it (harmlessly,
                                    # but indefinitely — one rate-limited
                                    # warning per reconnect forever) since it
                                    # thinks we never received it.
                                    # Live-deploy finding (GitHub #68 follow-up,
                                    # 2026-08-18): `persistent_id` truthiness,
                                    # not `is not None` — the real protobuf
                                    # field defaults to `""`, never `None`, so
                                    # `is not None` was always true and could
                                    # append/ack a meaningless empty id.
                                    if persistent_id:
                                        self.persistent_ids.append(persistent_id)
                                        if self.config.send_selective_acknowledgements:
                                            # Bug fix: `_send_selective_ack` ->
                                            # upstream `_send_msg` does
                                            # `self.writer.write(buf)` with no
                                            # None-guard. If a concurrent
                                            # `_reset()`/`_do_writer_close()`
                                            # already cleared `self.writer` (a
                                            # mid-reset race on this exact
                                            # message), this raises
                                            # AttributeError — not in
                                            # skip_exceptions/RuntimeError/
                                            # (OSError, EOFError) — which would
                                            # otherwise escape to the broad
                                            # `except Exception` below and crash
                                            # the whole client over one in-flight
                                            # ack. Skip cleanly instead; Google
                                            # MCS redelivers an unacked message
                                            # on the next reconnect.
                                            if (
                                                getattr(self, "writer", None)
                                                is not None
                                            ):
                                                try:
                                                    await self._send_selective_ack(
                                                        persistent_id
                                                    )
                                                except AttributeError:
                                                    pass

                                try:
                                    await self._handle_message(msg)
                                except skip_exceptions as decode_ex:
                                    # binascii.Error: defense-in-depth only as of
                                    # GitHub #68 (when decrypt_override is active)
                                    # — the padded _decrypt_raw_data() override
                                    # (upstream sdb9696/firebase-messaging#37) now
                                    # handles the common unpadded-crypto-key/salt
                                    # case by actually decrypting successfully, so
                                    # this half should rarely fire anymore. Kept
                                    # for any other binascii.Error.
                                    # ECEException (when http_ece is importable):
                                    # added alongside the padding fix — every
                                    # failure path inside
                                    # http_ece.decrypt() (bad padding, truncated
                                    # message, decrypt-tag mismatch on a message
                                    # meant for a different subtype/app_id — the
                                    # library warns-but-still-attempts-decrypt on
                                    # a subtype mismatch) raises ECEException, a
                                    # bare Exception, not a ValueError. Before the
                                    # padding fix this was unreachable in practice
                                    # (every message failed earlier at the
                                    # unpadded-header decode step, always as
                                    # binascii.Error); now that headers decode
                                    # successfully, a single message with a
                                    # genuinely bad/mismatched ciphertext body
                                    # would otherwise fall through to the broad
                                    # `except Exception` below and tear down the
                                    # whole FcmPushClient over one bad payload.
                                    # Skip just this message instead — same
                                    # reasoning as the binascii.Error case.
                                    # Deliberately still narrower than ValueError:
                                    # _decrypt_raw_data() also raises plain
                                    # ValueError from corrupt *stored* credentials
                                    # (load_der_private_key /
                                    # _decode_credential_material on a malformed
                                    # private key) — a client-wide fault that must
                                    # still hit the broad except below to trigger
                                    # the supervisor's hard-heal (credential purge
                                    # + re-registration), not be masked as a
                                    # one-off bad message. Google MCS redelivers
                                    # an unacked message on every reconnect, so
                                    # rate-limit like the OSError path above
                                    # instead of a raw warning.
                                    await _skip_and_ack(decode_ex)
                                except RuntimeError as app_data_ex:
                                    # Live-deploy finding (GitHub #68 follow-up,
                                    # 2026-08-18): upstream's
                                    # _handle_data_message() (fcmpushclient.py)
                                    # only special-cases message_type ==
                                    # "deleted_messages" before unconditionally
                                    # looking up the "crypto-key"/"encryption"
                                    # app_data entries via _app_data_by_key(),
                                    # which raises a bare
                                    # RuntimeError(f"couldn't find in app_data
                                    # {key}") when either is absent — e.g. any
                                    # non-webpush control/diagnostic message
                                    # Bosch/Google sends, or (per RFC 8291) an
                                    # aes128gcm-encoded message, which carries
                                    # no separate crypto-key/salt headers at
                                    # all. That RuntimeError isn't in
                                    # skip_exceptions, so it used to reach the
                                    # broad except Exception below and
                                    # terminate the whole client over one
                                    # malformed/unsupported message — the same
                                    # crash-loop class as the EC-key incident.
                                    # Deliberately match ONLY this specific
                                    # message shape (not every RuntimeError —
                                    # a RuntimeError from somewhere else, e.g.
                                    # _send_selective_ack failing, is not a
                                    # single-message-scoped fault and must
                                    # still propagate).
                                    if "couldn't find in app_data" not in str(
                                        app_data_ex
                                    ):
                                        raise
                                    await _skip_and_ack(app_data_ex)

                        except (OSError, EOFError) as osex:
                            # FIX for issue #33: advance state to RESETTING here,
                            # before the quiet-path check below — the library only
                            # sets it inside _reset() which is called afterwards.
                            # Without this line, the first OS error always takes the
                            # _logger.exception() branch even though the connection
                            # is about to be gracefully reset.
                            if self.run_state not in (  # type: ignore[has-type]  # external FcmPushClient attr (untyped base)
                                FcmPushClientRunState.RESETTING,
                                FcmPushClientRunState.STOPPING,
                                FcmPushClientRunState.STOPPED,
                            ):
                                self.run_state = FcmPushClientRunState.RESETTING

                            quiet_reset = (
                                isinstance(
                                    osex,
                                    (
                                        ConnectionResetError,
                                        TimeoutError,
                                        asyncio.IncompleteReadError,
                                        ssl.SSLError,
                                    ),
                                )
                                and self.run_state == FcmPushClientRunState.RESETTING
                            )
                            if quiet_reset:
                                if (
                                    isinstance(osex, ssl.SSLError)
                                    and osex.reason
                                    != "APPLICATION_DATA_AFTER_CLOSE_NOTIFY"
                                ):
                                    self._log_warn_with_limit(
                                        "Unexpected SSLError reason during reset of %s",
                                        osex.reason,
                                    )
                                else:
                                    self._log_verbose(
                                        "Expected read error during reset: %s",
                                        type(osex).__name__,
                                    )
                            else:
                                _LOGGER.exception("Unexpected exception during read\n")

                            # Live-deploy finding (GitHub #68 follow-up,
                            # 2026-08-18): upstream's OWN quiet branch never
                            # calls _reset() either — only its else/loud
                            # branch does. That's fine upstream, because
                            # run_state only becomes RESETTING *inside*
                            # _reset() itself, so the quiet branch is only
                            # ever reached on the SECOND+ error of an
                            # already-in-progress reset. Our issue #33 fix
                            # sets run_state = RESETTING pre-emptively
                            # (above, before this except block) purely to
                            # route the FIRST error to the quiet log path
                            # too — but that pre-emptive flag now makes the
                            # quiet branch reachable on the FIRST error of
                            # EVERY routine WAN blip / MCS disconnect too,
                            # and since nothing else ever calls _reset() for
                            # it, _listen() just spun on
                            # `if run_state == RESETTING: sleep(1)` forever
                            # instead of reconnecting — recovery only came
                            # from the supervisor's outer teardown+rebuild,
                            # a full fresh Google registration instead of a
                            # cheap in-place reconnect. Call _reset() from
                            # both branches now (idempotent: guarded by
                            # reset_lock, a no-op if a reset is already
                            # under way) so the quiet logging fix no longer
                            # disables recovery.
                            # Import ErrorType lazily — it is a private enum
                            # in the library module, not exported via
                            # __all__. If the import fails (future refactor)
                            # we skip the error counter; the self-heal
                            # watchdog still fires.
                            try:
                                from firebase_messaging.fcmpushclient import (
                                    ErrorType as _ErrorType,
                                )

                                if self._try_increment_error_count(
                                    _ErrorType.CONNECTION
                                ):
                                    await self._reset()
                            except ImportError:
                                await self._reset()
                except Exception as ex:
                    import traceback as _tb

                    _LOGGER.error(
                        "Unknown error: %s, shutting down FcmPushClient.\n%s",
                        ex,
                        _tb.format_exc(),
                    )
                    self._terminate()
                finally:
                    await self._do_writer_close()

        if decrypt_override is not None:
            _Patched._decrypt_raw_data = staticmethod(decrypt_override)
        if handle_data_message_override is not None:
            _Patched._handle_data_message = handle_data_message_override

        return _Patched

    # Module-level cache so _patch_class() runs at most once per process.
    _patched_class: type | None | bool = False  # False = not yet computed


def _get_fcm_push_client_class() -> type | None:
    """Return the patched FcmPushClient subclass (or vanilla if patch failed).

    Cached after the first call.
    """
    if _QuietFcmPushClient._patched_class is False:
        _QuietFcmPushClient._patched_class = _QuietFcmPushClient._patch_class()
    result = _QuietFcmPushClient._patched_class
    if result is None:
        # Patch failed — fall back to vanilla
        try:
            from firebase_messaging import FcmPushClient

            return FcmPushClient  # type: ignore[no-any-return]  # value is correct at runtime; HA/external source is Any-typed
        except ImportError:
            return None
    return result  # type: ignore[return-value]  # False-sentinel already replaced before this point


# Firebase Cloud Messaging — push notifications from Bosch CBS
FCM_SENDER_ID = "404630424405"  # public app-level identifier — same in every Android APK; intentional in source


# ── Firebase config ──────────────────────────────────────────────────────────


async def fetch_firebase_config(hass: HomeAssistant) -> dict[str, str]:
    """Return Firebase config for the Bosch Smart Camera app.

    These are public app-level identifiers embedded in every copy of the
    Bosch Smart Camera APK — they identify the app to Firebase, not the user.
    The API key is restricted by Firebase project rules (not by secrecy).
    """
    project_id = "bosch-smart-cameras"
    app_id = f"1:{FCM_SENDER_ID}:android:9e5b6b58e4c70075"
    import base64

    # Vendor-sanctioned OSS Firebase API key — FCM permissions confirmed for OSS use.
    _k = base64.b64decode(
        "QUl6YVN5Q0toaGZ4ZlRzMUc3V3Z6VERBaU8wQWlzN0VIMjVEYk9z"
    ).decode()
    return {
        "project_id": project_id,
        "app_id": app_id,
        "api_key": _k,
    }


# ── FCM start / stop ────────────────────────────────────────────────────────


async def async_ensure_fcm_supervisor(coordinator: Any) -> None:
    """Start the FCM supervisor task if FCM is enabled and not already running.

    This is the single entry point for FCM lifecycle management. The supervisor
    task keeps the push listener alive with automatic restart and exponential
    backoff — call sites no longer need to manage heals or cool-downs.
    Idempotent: safe to call while the supervisor is already running.
    """
    if not coordinator.options.get("enable_fcm_push", False):
        return
    # Bug fix: the coordinator tick schedules this as a bare task with no
    # "shutting down" guard. If that task runs AFTER async_stop_fcm_supervisor
    # already set fcm_supervisor_task=None during unload (e.g. a reload race),
    # it could resurrect a new supervisor against a dead/unloading config
    # entry. `nvr_shutting_down` is set at the very start of the coordinator
    # teardown (__init__.py), before async_stop_fcm_push is even awaited —
    # reuse it here as the general "this config entry is unloading" signal.
    if getattr(coordinator, "nvr_shutting_down", False):
        return
    sup = getattr(coordinator, "fcm_supervisor_task", None)
    if sup is not None and not sup.done():
        return
    coordinator.fcm_supervisor_task = asyncio.ensure_future(
        _async_run_fcm_supervisor(coordinator),
    )
    coordinator.fcm_supervisor_task.set_name("bosch_shc_camera_fcm_supervisor")


async def async_stop_fcm_supervisor(coordinator: Any) -> None:
    """Cancel the FCM supervisor task, then stop the push listener."""
    sup = getattr(coordinator, "fcm_supervisor_task", None)
    if sup is not None and not sup.done():
        sup.cancel()
        try:
            await sup
        except asyncio.CancelledError:
            # GitHub #68 live-deploy finding, 2026-08-18: only swallow the
            # cancellation when it's genuinely the supervisor's OWN (i.e.
            # the sup.cancel() call just above — the expected/normal case)
            # — not a cancellation of the CALLER (this coroutine's own
            # task, e.g. HA's shutdown deadline cancelling __init__.py's
            # teardown while it happens to be suspended right here on
            # `await sup`). __init__.py's _async_cancel_coordinator_tasks
            # explicitly documents that async_stop_fcm_push "explicitly
            # re-raises asyncio.CancelledError" and builds its own
            # cleanup-continuation tracking (_cancelled_during_cleanup) on
            # that contract — a bare `except (asyncio.CancelledError,
            # Exception): pass` here broke it, silently discarding a real
            # shutdown-deadline cancellation instead of letting it
            # propagate as promised.
            #
            # `sup.cancelled()` is NOT a usable signal here: asyncio's Task
            # machinery cancels the future a task is currently awaiting as
            # PART of delivering a cancellation to that task (`_fut_waiter.
            # cancel()`), so cancelling the CALLER while it's suspended on
            # `await sup` cancels `sup` too either way — both cases end up
            # with `sup.cancelled() is True`. The reliable distinguishing
            # signal is whether the CURRENT task itself was ever the
            # cancellation's target: `Task.cancelling()` (Python 3.11+)
            # counts pending cancel() requests against THIS task
            # specifically — calling `sup.cancel()` on a different task
            # object doesn't touch it. It stays 0 for our own expected
            # self-inflicted stop; it's >0 only when something explicitly
            # cancelled the task running this coroutine.
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling() > 0:
                raise
        except Exception:  # noqa: S110 — the supervisor's own failure, not ours to raise
            pass
        coordinator.fcm_supervisor_task = None
    await async_stop_fcm_push(coordinator)


async def _async_start_fcm_push_locked(coordinator: Any) -> bool:
    """Start the FCM push listener. Caller must hold `coordinator.fcm_start_lock`.

    Returns True if the listener started successfully, False otherwise.
    """
    if coordinator.fcm_running:
        return True
    if not coordinator.options.get("enable_fcm_push", False):
        _LOGGER.debug("FCM push disabled in options")
        return False

    try:
        from firebase_messaging import FcmRegisterConfig
    except ImportError:
        _LOGGER.warning("firebase-messaging not installed — FCM push disabled")
        return False

    # Use our patched subclass that fixes the upstream state-machine bug (issue #33):
    # it sets run_state=RESETTING before the error-log decision so transient WAN
    # errors are routed to INFO-level rather than _logger.exception().
    FcmPushClient = _get_fcm_push_client_class()
    if FcmPushClient is None:
        _LOGGER.warning("firebase-messaging not installed — FCM push disabled")
        return False

    # FcmPushClientConfig landed in firebase-messaging 0.4; guard defensively
    # so older installs still start (without the hardening).
    try:
        from firebase_messaging import FcmPushClientConfig
    except ImportError:  # pragma: no cover — 0.4+ ships this symbol
        FcmPushClientConfig = None

    # Determine push mode — only "auto" (use OSS FCM key) or "polling" (skip FCM).
    # Legacy values "ios"/"android" from older versions coerce to "auto".
    push_mode = coordinator.options.get("fcm_push_mode", "auto")
    if push_mode not in ("auto", "polling"):
        push_mode = "auto"

    async def _build_fcm_cfg() -> dict[str, str]:
        """Return the OSS-sanctioned Firebase config (single source, no per-mode split)."""
        cfg = coordinator.entry.data.get("fcm_config") or {}
        if not cfg:
            cfg = await fetch_firebase_config(coordinator.hass)
            if cfg:
                coordinator.hass.config_entries.async_update_entry(
                    coordinator.entry,
                    data={**coordinator.entry.data, "fcm_config": cfg},
                )
        return cfg

    async def _try_fcm() -> bool:
        """Attempt FCM registration with the OSS key. Returns True on success."""
        fcm_cfg = await _build_fcm_cfg()
        if not fcm_cfg.get("api_key"):
            _LOGGER.warning("FCM: could not obtain Firebase config")
            return False

        fcm_config = FcmRegisterConfig(
            project_id=fcm_cfg["project_id"],
            app_id=fcm_cfg["app_id"],
            api_key=fcm_cfg["api_key"],
            messaging_sender_id=FCM_SENDER_ID,
        )

        # Load saved FCM credentials from config entry (survives HA restarts)
        saved_fcm_creds = coordinator.entry.data.get("fcm_credentials")

        # Bound AFTER coordinator.fcm_client is assigned below. Read via
        # late-binding closure (not captured by value) so the comparison in
        # _persist() below always reflects which client THIS _try_fcm() call
        # created — not whatever coordinator.fcm_client points to by the
        # time the callback actually fires.
        _this_client: Any = None

        def _on_creds_updated(creds: Any) -> None:
            """Save FCM credentials to config entry for persistence.

            WHY threadsafe: this callback fires from the FCM client's own
            thread (Firebase SDK), not from the HA event loop. Calling
            `async_update_entry` directly from a foreign thread corrupts
            HA's internal state. `call_soon_threadsafe` hops back onto
            the loop before scheduling the async task.
            """

            def _persist() -> None:
                # Guard against a stale client: a hard-heal purges creds and
                # starts a fresh client+checkin while an OLD client's
                # callback (fired from its own SDK thread, not necessarily
                # covered by the drain-wait in async_stop_fcm_push) can still
                # land on the loop afterwards. Without this check the late
                # callback would silently overwrite the fresh credentials
                # with stale ones, defeating the hard-heal it was meant to
                # recover from.
                if coordinator.fcm_client is not _this_client:
                    _LOGGER.debug(
                        "FCM: ignoring credentials_updated_callback from a "
                        "stale/replaced client"
                    )
                    return
                # Bug fix (F1): pass `_this_client` through so the persist
                # function re-checks identity right before the write (not
                # just here at schedule time) — closes the race where a
                # hard-heal purge lands between scheduling and the task
                # actually running. Also use spawn_tracked (not a bare
                # hass.async_create_task) so this is cancelled/awaited on
                # unload like other background tasks in this codebase.
                spawn = getattr(coordinator, "spawn_tracked", None)
                if spawn is not None:
                    spawn(
                        _async_persist_fcm_creds(coordinator, creds, _this_client),
                        name="bosch_shc_camera_fcm_persist_creds",
                    )
                else:  # pragma: no cover — defensive fallback for stub coordinators
                    coordinator.hass.async_create_task(
                        _async_persist_fcm_creds(coordinator, creds, _this_client)
                    )

            coordinator.hass.loop.call_soon_threadsafe(_persist)

        def _on_push(
            notification: dict[str, Any], persistent_id: str, obj: Any = None
        ) -> None:
            """Called when a push notification arrives from Bosch CBS."""
            _on_fcm_push(coordinator, notification, persistent_id, obj)

        # v10.3.22: harden against firebase-messaging#33. Default config aborts
        # the listener after 3 sequential CONNECTION errors (e.g. WAN blip) and
        # never reconnects — the client goes silent, our sensor keeps reporting
        # "fcm_push" while no pushes arrive. Passing None disables the abort;
        # library handles normal reconnect. Coordinator-tick watchdog below
        # (__init__.py) flips fcm_healthy=False if no push in 1h, so the
        # dashboard sensor still shows the degraded state.
        fcm_kwargs = {
            "callback": _on_push,
            "fcm_config": fcm_config,
            "credentials": saved_fcm_creds,
            "credentials_updated_callback": _on_creds_updated,
            # GitHub #68 live-deploy finding, 2026-08-18: without this,
            # FcmRegister (inside checkin_or_register()) lazily creates and
            # owns its OWN aiohttp.ClientSession, closed only on the SUCCESS
            # path (fcmpushclient.py) — a failed registration (the whole
            # point of the retry loop this file's supervisor runs) leaked
            # one session per attempt, compounding badly with a tight
            # retry cadence during an outage. Passing HA's shared session
            # means FcmRegister never owns a session to leak: its `_session`
            # property returns this one directly and its `close()` only
            # ever touches a lazily-created `_local_session`, which stays
            # None here — so it's also safe to pass on the SUCCESS path,
            # never closing HA's shared session out from under other users.
            "http_client_session": async_get_clientsession(coordinator.hass),
        }
        if FcmPushClientConfig is not None:
            fcm_kwargs["config"] = FcmPushClientConfig(
                abort_on_sequential_error_count=None,
            )
        coordinator.fcm_client = FcmPushClient(**fcm_kwargs)
        _this_client = coordinator.fcm_client

        try:
            coordinator.fcm_token = await coordinator.fcm_client.checkin_or_register()
            _LOGGER.debug("FCM registered — token: %s...", coordinator.fcm_token[:8])
        except Exception as err:
            # Log diagnostic details that survive _FCMNoiseFilter. The raw
            # error message often contains substrings the filter dedups
            # ("PHONE_REGISTRATION_ERROR", "Unable to establish subscription"),
            # which can starve the operator of visibility into WHY the heal
            # ladder keeps tripping. Mask the marker substrings so the
            # filter doesn't dedup this diagnostic line.
            err_type = type(err).__name__
            err_short = str(err)[:240].replace("\n", " ")
            # Mask FCMNoiseFilter markers so this line passes through.
            for marker in (
                "PHONE_REGISTRATION_ERROR",
                "Unable to complete gcm auth request",
                "Unable to establish subscription",
                "Unexpected exception during read",
            ):
                err_short = err_short.replace(
                    marker, marker.replace("_", "·").replace(" ", "·")
                )
            _LOGGER.warning(
                "FCM checkin/register raised %s — %s",
                err_type,
                err_short,
            )
            # Bug fix (F3): the client object was already created above
            # (holds a socket/connection + background resources) — discarding
            # the reference without stop() leaks an undrained client that
            # logs "Unexpected exception during read" every ~63s forever.
            failed_client = coordinator.fcm_client
            coordinator.fcm_client = None
            if failed_client is not None:
                with contextlib.suppress(Exception):
                    await failed_client.stop()
            return False

        # Register FCM token with Bosch CBS API. coordinator.fcm_push_mode is
        # still "unknown" at this point (set to "auto" only after client.start()).
        # Bug fix (F2): the result was previously discarded — on a
        # registration failure (timeout/401/500) Bosch never actually has our
        # push token, so listening would still succeed and fcm_healthy would
        # get set True with zero pushes ever arriving and no visible failure
        # signal. Treat a failed Bosch registration as a start failure so the
        # normal retry/backoff ladder applies instead.
        if not await register_fcm_with_bosch(coordinator):
            _LOGGER.warning(
                "FCM: Bosch CBS device registration failed — not starting listener"
            )
            failed_client = coordinator.fcm_client
            coordinator.fcm_client = None
            if failed_client is not None:
                with contextlib.suppress(Exception):
                    await failed_client.stop()
            return False

        # Start listening for pushes
        try:
            await coordinator.fcm_client.start()
            with coordinator.fcm_lock:
                coordinator.fcm_running = True
                coordinator.fcm_healthy = True
                coordinator.fcm_started_at = time.monotonic()
                coordinator.fcm_push_mode = "auto"
            _LOGGER.info(
                "FCM push listener started — near-instant event detection active"
            )
            return True
        except Exception as err:
            _LOGGER.warning("FCM push listener failed to start: %s", err)
            # Bug fix (F3): stop() the client before discarding it on this
            # failure path too — same leak as the checkin/register failure
            # above.
            failed_client = coordinator.fcm_client
            with coordinator.fcm_lock:
                coordinator.fcm_client = None
            if failed_client is not None:
                with contextlib.suppress(Exception):
                    await failed_client.stop()
            return False

    # Install once before any FCM client is created so the very first WAN
    # outage doesn't spam 12 k+ recursive-traceback lines at us.
    _install_fcm_noise_filter()

    if push_mode == "polling":
        _LOGGER.info("FCM push mode set to 'polling' — using standard API polling only")
        return False

    # "auto" — try FCM with the OSS-sanctioned key; on failure the supervisor
    # will retry automatically (see _async_run_fcm_supervisor backoff ladder).
    result = await _try_fcm()
    if not result:
        _LOGGER.info(
            "FCM registration failed — falling back to standard polling "
            "(supervisor will retry with exponential backoff)"
        )
    return result


async def register_fcm_with_bosch(coordinator: Any) -> bool:
    """Register our FCM token with Bosch CBS so it sends us push notifications.

    Endpoint: POST /v11/devices {"deviceType": "ANDROID", "deviceToken": token}
    Response: HTTP 204 on success. deviceType is always ANDROID — the OSS
    Firebase app registered with Bosch lives under the Android app_id.
    """
    if not coordinator.fcm_token or not coordinator.token:
        return False

    # Skip re-registration only when BOTH conditions hold:
    #   1. The same FCM device token was already registered in a previous run.
    #   2. The registration used deviceType=ANDROID (marker written since Fix C++).
    # If either is false the POST fires to heal any drift.
    #
    # Drift scenario: a migration that left fcm_registered_token intact but
    # wrote no fcm_registered_device_type marker would fire the old skip-logic
    # on token==token, leaving Bosch CBS with deviceType=IOS while the HA
    # client used the Android Firebase context — routing all FCM pushes to
    # the wrong sub-app. Fix: require the ANDROID marker before allowing the skip.
    stored_token: str | None = coordinator.entry.data.get("fcm_registered_token")
    stored_device_type: str | None = coordinator.entry.data.get(
        "fcm_registered_device_type"
    )
    # Proactive re-registration: even when the token is unchanged,
    # re-POST if the last successful registration is older than
    # FCM_REREGISTER_INTERVAL_SEC so a server-side-dropped Bosch device
    # registration self-heals without needing a token change or a hard-heal.
    registered_at_raw = coordinator.entry.data.get("fcm_registered_at")
    try:
        registered_at = float(registered_at_raw) if registered_at_raw else 0.0
    except (TypeError, ValueError):
        registered_at = 0.0
    registration_stale = (time.time() - registered_at) > FCM_REREGISTER_INTERVAL_SEC
    if (
        stored_token == coordinator.fcm_token
        and stored_device_type == "ANDROID"
        and not registration_stale
    ):
        _LOGGER.debug(
            "FCM: token unchanged + deviceType=ANDROID verified + registration "
            "fresh — skipping re-registration"
        )
        return True
    if (
        stored_token == coordinator.fcm_token
        and stored_device_type == "ANDROID"
        and registration_stale
    ):
        _LOGGER.info(
            "FCM: Bosch CBS registration older than %d days — re-POSTing to keep "
            "push delivery alive (token unchanged)",
            FCM_REREGISTER_INTERVAL_SEC // 86400,
        )
    if stored_token == coordinator.fcm_token and stored_device_type != "ANDROID":
        _LOGGER.info(
            "FCM CBS heal: token unchanged but deviceType marker is %r (not ANDROID) — "
            "forcing re-registration as deviceType=ANDROID",
            stored_device_type,
        )

    session = await async_get_bosch_cloud_session(coordinator.hass)
    headers = {
        "Authorization": f"Bearer {coordinator.token}",
        "Content-Type": "application/json",
    }
    payload = {"deviceType": "ANDROID", "deviceToken": coordinator.fcm_token}

    try:
        async with asyncio.timeout(10):
            async with session.post(
                f"{CLOUD_API}/v11/devices", headers=headers, json=payload
            ) as resp:
                if resp.status in (200, 201, 204):
                    coordinator.hass.config_entries.async_update_entry(
                        coordinator.entry,
                        data={
                            **coordinator.entry.data,
                            "fcm_registered_token": coordinator.fcm_token,
                            "fcm_registered_device_type": "ANDROID",
                            "fcm_registered_at": time.time(),
                        },
                    )
                    _LOGGER.info(
                        "FCM token registered with Bosch CBS as deviceType=ANDROID (HTTP %d)",
                        resp.status,
                    )
                    return True
                resp_body = await resp.text()
                if resp.status == 500 and "sh:internal.error" in resp_body:
                    # Bosch returns 500 "sh:internal.error" when the same device
                    # token is already registered — FCM push still works. Treat as
                    # success and save both markers so subsequent restarts skip the POST.
                    coordinator.hass.config_entries.async_update_entry(
                        coordinator.entry,
                        data={
                            **coordinator.entry.data,
                            "fcm_registered_token": coordinator.fcm_token,
                            "fcm_registered_device_type": "ANDROID",
                            "fcm_registered_at": time.time(),
                        },
                    )
                    _LOGGER.debug(
                        "FCM: token already registered with Bosch (HTTP 500 sh:internal.error) — skipping on next restart"
                    )
                    return True
                _LOGGER.warning(
                    "FCM token registration failed: HTTP %d — %s",
                    resp.status,
                    resp_body[:200],
                )
    except (TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.warning("FCM token registration error: %s", err)
    return False


async def deregister_fcm_with_bosch(coordinator: Any, device_token: str) -> bool:
    """Deregister a stale FCM device token from Bosch CBS.

    Endpoint: PUT /v11/registration/logout?deviceToken=<token>. Response:
    HTTP 204 on success. Best-effort — called right before a hard-heal purge
    discards `device_token` for a fresh one, so Bosch's backend doesn't
    accumulate an abandoned registration per purge (Bosch backend-load
    request, 2026-09-04). Never raises; a failure here must not block the
    purge/re-registration it's cleaning up after — including if the
    coordinator has no bearer token available yet (e.g. very early startup).
    """
    bearer_token = getattr(coordinator, "token", None)
    if not device_token or not bearer_token:
        return False

    session = await async_get_bosch_cloud_session(coordinator.hass)
    headers = {"Authorization": f"Bearer {bearer_token}"}

    try:
        async with asyncio.timeout(10):
            async with session.put(
                f"{CLOUD_API}/v11/registration/logout",
                headers=headers,
                params={"deviceToken": device_token},
            ) as resp:
                if resp.status in (200, 204):
                    _LOGGER.debug(
                        "FCM: deregistered stale device token with Bosch CBS (HTTP %d)",
                        resp.status,
                    )
                    return True
                resp_body = await resp.text()
                _LOGGER.info(
                    "FCM: stale device token deregistration failed (non-fatal): "
                    "HTTP %d — %s",
                    resp.status,
                    resp_body[:200],
                )
    except Exception as err:  # see docstring: must NEVER raise.
        # Bug-hunt finding (2026-09-04): this call sits INSIDE the hard-heal
        # purge's fcm_start_lock, before entry.data is rewritten. The
        # narrower `except (TimeoutError, aiohttp.ClientError)` this started
        # with let anything else (e.g. a malformed `device_token`/response
        # encoding edge case) escape into the purge's own broad
        # `except Exception:` handler — which aborts the ENTIRE purge and
        # retries at a fixed 5s cadence, silently blocking credential
        # recovery instead of just skipping this best-effort cleanup step.
        _LOGGER.info(
            "FCM: stale device token deregistration error (non-fatal): %s", err
        )
    return False


async def async_stop_fcm_push(coordinator: Any) -> None:
    """Stop the FCM push listener.

    firebase-messaging's ``client.stop()`` cancels its internal read/heartbeat
    tasks via ``task.cancel()`` but returns before those tasks finish their
    ``finally: await self._do_writer_close()`` cleanup. If we recreate the
    FcmPushClient before the old SSL shutdown completes (e.g. user toggles
    ``fcm_push_mode`` in the UI), the old read loop emits
    ``ERROR [firebase_messaging.fcmpushclient] Unexpected exception during read``
    once per ~63 s and never recovers — the state machine sees the SSL close
    fire outside of ``RESETTING`` state. Awaiting the cancelled tasks here
    drains the old SSL session before the new client starts. Library has no
    documented stop-and-restart pattern (upstream issues #23, #33 open).
    """
    with coordinator.fcm_lock:
        client = coordinator.fcm_client
    # Bug fix (F4): the old guard required `running` to also be True. If
    # cancellation happens WHILE `await client.start()` is in flight (e.g.
    # HA shutdown racing FCM startup), the supervisor can end up with
    # fcm_client set but fcm_running still False — a subsequent stop then
    # no-oped and the listener survived unload as an orphan. Attempt to
    # stop/close any existing client object regardless of the running flag;
    # stop() itself is idempotent/safe to call on a not-yet-fully-started
    # client (guarded by the try/except below either way).
    if client:
        try:
            await client.stop()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug("FCM stop raised: %s", err)
        pending = getattr(client, "tasks", None) or []
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=10.0,
                )
            except TimeoutError:
                _LOGGER.debug(
                    "FCM stop: %d background task(s) did not drain in 10 s — "
                    "proceeding (residual SSL close may log one final error)",
                    len(pending),
                )
            except asyncio.CancelledError:
                raise
        with coordinator.fcm_lock:
            coordinator.fcm_running = False
            coordinator.fcm_healthy = False
            coordinator.fcm_client = None
            coordinator.fcm_push_mode = "unknown"
        _LOGGER.info("FCM push listener stopped")


async def _async_run_fcm_supervisor(coordinator: Any) -> None:
    """FCM supervisor loop — keeps the push listener alive indefinitely.

    Replaces the watchdog + self-heal state machine. This task runs for the
    entire lifetime of the HA config entry. On each iteration it:
      1. Decides whether a hard-heal (credential purge + fresh registration)
         is needed (delivery-death flag, 3+ consecutive soft-only restarts,
         or PHONE_REGISTRATION_ERROR in the creds-staleness window). For the
         two CONFIRMED-problem reasons (delivery-death flag, creds
         staleness), if the previous such heal also failed to bring back a
         push, backs off through FCM_SUPERVISOR_BACKOFF_SEC before purging
         again instead of retrying at the same speed indefinitely (GitHub
         #68). The two benign reasons (soft-restart threshold, fresh
         install) never feed this backoff — a quiet house with no motion to
         push has no way to prove a benign heal actually worked.
      2. Starts the FCM listener inside the start-lock.
      3. Polls `is_started()` every FCM_SUPERVISOR_POLL_SEC seconds.
      4. When the listener dies, waits FCM_SUPERVISOR_BACKOFF_SEC[failures]
         before the next attempt (resets to 0 if a real push arrived).

    Root-cause context: `firebase-messaging`'s listener can terminate
    permanently after an SSL timeout / 3 sequential errors depending on the
    installed library version. The supervisor ensures recovery regardless.
    """
    failures = 0  # consecutive restarts WITHOUT a push received
    soft_streak = 0  # consecutive soft-only restarts (no hard-heal between)
    # Consecutive CONFIRMED-problem hard-heals with no push since the prior one.
    hard_heal_streak: int = 0
    push_ts_at_last_hard_heal: float = coordinator.fcm_last_push
    # Consecutive soft-restart-threshold-triggered hard-heals with no
    # SUSTAINED listener uptime since the previous one (Bosch backend-load
    # finding, 2026-09-04: on a persistently flaky WAN/router, 3 push-less
    # soft-restarts can recur every 1.5-5 min indefinitely — each one purges
    # good credentials and mints a genuinely new Bosch device-token
    # registration, since this benign-reason path deliberately skips the
    # CONFIRMED-problem backoff below (see the comment at that branch).
    # Uptime, not a push, is the recovery signal here — a quiet house has no
    # push to prove anything either way, but FCM_SUPERVISOR_SUSTAINED_UPTIME_SEC
    # of continuous is_started()=True is still real evidence the last heal
    # worked. Reset in the sustained-uptime block below.
    soft_trigger_heal_streak: int = 0
    # Live-deploy finding (GitHub #68 follow-up, 2026-08-18): whether this
    # supervisor has ever completed a hard-heal purge. Distinguishes a truly
    # fresh install (never purged, benign) from "we already purged and
    # re-registration still isn't restoring credentials" (the SAME ongoing
    # failure hard_heal_streak exists to detect) for the "no persisted
    # credentials" reason below.
    has_ever_hard_healed = False

    _LOGGER.debug("FCM supervisor started")

    while True:
        # Bug fix (F8): when fcm_push_mode="polling" (push deliberately
        # disabled by the user) with enable_fcm_push still True,
        # `_async_start_fcm_push_locked` always returns False — without this
        # short-circuit, the loop below would count that as a failure and,
        # after FCM_SUPERVISOR_SOFT_HEAL_MAX restarts, hard-heal (purge +
        # rewrite entry.data) forever, on every iteration up to the 1800s
        # backoff ceiling, for a mode the user explicitly chose. Skip the
        # heal/start machinery entirely and just re-check periodically in
        # case the user switches back to "auto".
        if coordinator.options.get("fcm_push_mode", "auto") == "polling":
            try:
                await asyncio.sleep(FCM_SUPERVISOR_POLL_SEC)
            except asyncio.CancelledError:
                break
            continue

        push_ts_before = coordinator.fcm_last_push

        # ── Decide heal strategy ────────────────────────────────────────────
        force_hard = getattr(coordinator, "fcm_force_hard_heal", False)
        needs_hard = (
            force_hard
            or soft_streak >= FCM_SUPERVISOR_SOFT_HEAL_MAX
            or get_recent_fcm_creds_staleness_count(600.0) > 0
            or not coordinator.entry.data.get("fcm_credentials")
        )

        # Bug fix (F6): the flag used to be cleared HERE, before the purge
        # block below runs. If the purge itself raised, the `continue`
        # re-entered the loop with the flag already gone, silently dropping a
        # confirmed delivery-death-triggered heal (force_hard would not be
        # observed again next iteration). Clear it only after the purge+
        # re-registration attempt completes (success or failure) — see the
        # `finally`-equivalent clearing further below.

        if needs_hard:
            # Reason (and whether it's a CONFIRMED problem, vs. a benign
            # trigger) decided upfront so both the streak logic below and the
            # log line use the same, pre-sleep-accurate value.
            confirmed_problem: bool
            if force_hard:
                reason = "polling confirmed delivery dead"
                confirmed_problem = True
            elif soft_streak >= FCM_SUPERVISOR_SOFT_HEAL_MAX:
                reason = (
                    f"{soft_streak} soft-restarts without a push — delivery likely dead"
                )
                # NOT confirmed: a quiet house (no motion → no push to prove
                # anything) can hit this threshold from ordinary WAN blips
                # whose re-registration already succeeded. Must not feed the
                # streak below, or a benign night keeps escalating backoff
                # toward FCM_SUPERVISOR_BACKOFF_SEC's 30-min ceiling for a
                # problem that was never actually still there.
                confirmed_problem = False
                # Separate, lighter backoff (Bosch backend-load finding,
                # 2026-09-04): does NOT require push confirmation like the
                # CONFIRMED-problem streak below — it only requires the
                # listener to have stayed up continuously for
                # FCM_SUPERVISOR_SUSTAINED_UPTIME_SEC since the last such
                # heal (reset in the sustained-uptime block). Prevents a
                # persistently flaky WAN from purging credentials + minting a
                # new Bosch FCM device-token registration every 1.5-5 min
                # indefinitely, without touching the deliberate "benign
                # heals never feed the CONFIRMED backoff" design above.
                soft_trigger_heal_streak += 1
                if soft_trigger_heal_streak > 1:
                    soft_heal_delay = FCM_SUPERVISOR_BACKOFF_SEC[
                        min(
                            soft_trigger_heal_streak - 2,
                            len(FCM_SUPERVISOR_BACKOFF_SEC) - 1,
                        )
                    ]
                    _LOGGER.info(
                        "FCM supervisor: soft-restart-threshold heal streak %d "
                        "(no sustained uptime since the last one) — waiting "
                        "%.0fs before purging credentials again",
                        soft_trigger_heal_streak,
                        soft_heal_delay,
                    )
                    try:
                        await asyncio.sleep(soft_heal_delay)
                    except asyncio.CancelledError:
                        break
            elif get_recent_fcm_creds_staleness_count(600.0) > 0:
                reason = "PHONE_REGISTRATION_ERROR in last 10 min — creds stale"
                confirmed_problem = True
            else:
                reason = "no persisted credentials"
                # A truly fresh install (never hard-healed yet) is benign —
                # nothing to escalate about. But once we've ALREADY purged
                # at least once this supervisor lifetime and credentials
                # still haven't come back, this is the SAME ongoing
                # registration failure hard_heal_streak exists to detect
                # (live-deploy finding, GitHub #68 follow-up 2026-08-18):
                # re-registration can keep failing without ever emitting one
                # of the three PHONE_REGISTRATION_ERROR markers (e.g. a
                # plain FCM-install RuntimeError, or a WAN/quota outage
                # during checkin) — without this, confirmed_problem stayed
                # False forever on this path, bypassing the backoff
                # entirely and pinning retries at FCM_SUPERVISOR_BACKOFF_SEC[0]
                # (5s) indefinitely, hammering Google's registration
                # endpoint exactly like the storm the backoff was built to
                # prevent.
                confirmed_problem = has_ever_hard_healed

            # A hard-heal purge+re-registration didn't restore delivery if no
            # push has arrived since the previous CONFIRMED-problem heal —
            # repeating it at the same near-immediate cadence just re-hits
            # Bosch/Google's registration endpoint indefinitely (GitHub #68:
            # PHONE_REGISTRATION_ERROR recurring), which can itself look like
            # abuse and get the client throttled further. Escalate backoff
            # across consecutive unsuccessful CONFIRMED heals instead of
            # always retrying at the same speed. Benign-reason heals neither
            # feed nor reset this streak — they're unrelated to it either way.
            if confirmed_problem:
                if push_ts_before > push_ts_at_last_hard_heal:
                    hard_heal_streak = 0
                hard_heal_streak += 1
                push_ts_at_last_hard_heal = push_ts_before

                if hard_heal_streak > 1:
                    heal_delay = FCM_SUPERVISOR_BACKOFF_SEC[
                        min(hard_heal_streak - 2, len(FCM_SUPERVISOR_BACKOFF_SEC) - 1)
                    ]
                    _LOGGER.info(
                        "FCM supervisor: hard-heal streak %d (no push since "
                        "the last one) — waiting %.0fs before purging "
                        "credentials again",
                        hard_heal_streak,
                        heal_delay,
                    )
                    try:
                        await asyncio.sleep(heal_delay)
                    except asyncio.CancelledError:
                        break

            _LOGGER.info("FCM supervisor: hard-heal (%s) — purging credentials", reason)

            try:
                async with coordinator.fcm_start_lock:
                    await async_stop_fcm_push(coordinator)
                    stale_token = coordinator.entry.data.get("fcm_registered_token")
                    if stale_token:
                        # Best-effort — clean up the about-to-be-abandoned
                        # registration on Bosch's backend (Bosch backend-load
                        # request, 2026-09-04). Never blocks/fails the purge.
                        await deregister_fcm_with_bosch(coordinator, stale_token)
                    new_data = {
                        k: v
                        for k, v in coordinator.entry.data.items()
                        if not k.startswith("fcm_")
                    }
                    purged = sorted(set(coordinator.entry.data) - set(new_data))
                    coordinator.hass.config_entries.async_update_entry(
                        coordinator.entry, data=new_data
                    )
                    _LOGGER.info(
                        "FCM supervisor: purged %d entry-data keys: %s",
                        len(purged),
                        purged,
                    )
                reset_fcm_creds_staleness_counter()
                has_ever_hard_healed = True
                soft_streak = 0
                # A hard-heal purge+re-registration is exactly the fix for a
                # credential-related failure, so also reset the backoff-delay
                # counter here — otherwise a freshly re-registered listener
                # dying again for an unrelated reason (a WAN blip, not
                # credentials) before any push arrives would compute its
                # retry delay off the stale, still-elevated failures value
                # and could wait up to 30 minutes despite the root cause
                # having just been fixed.
                failures = 0
                if force_hard:
                    coordinator.fcm_force_hard_heal = False
            except asyncio.CancelledError:
                raise
            except Exception:
                # An unhandled exception here (e.g. from async_update_entry)
                # used to propagate straight out of this loop, killing the
                # entire supervisor task — FCM push then stayed fully down
                # until the next coordinator-tick watchdog cycle noticed
                # sup.done() and restarted it, instead of the designed ~10s
                # poll cadence. Log and retry instead.
                _LOGGER.exception(
                    "FCM supervisor: hard-heal purge raised an exception — "
                    "retrying next iteration"
                )
                # Bug fix (F6): only clear the flag if the purge genuinely
                # completed — on failure, leave it set so the NEXT iteration
                # still observes needs_hard=True and retries the heal instead
                # of silently dropping a confirmed delivery-death-triggered
                # request.
                try:
                    await asyncio.sleep(FCM_SUPERVISOR_BACKOFF_SEC[0])
                except asyncio.CancelledError:
                    break
                continue

        # ── Start listener ─────────────────────────────────────────────────
        started = False
        try:
            lock = getattr(coordinator, "fcm_start_lock", None)
            if lock is None:
                lock = asyncio.Lock()
                coordinator.fcm_start_lock = lock
            async with lock:
                started = await _async_start_fcm_push_locked(coordinator)
        except asyncio.CancelledError:
            _LOGGER.debug("FCM supervisor cancelled during start")
            break
        except Exception:
            _LOGGER.exception("FCM supervisor: listener start raised exception")

        if not started:
            failures += 1
            soft_streak += 1
            delay = FCM_SUPERVISOR_BACKOFF_SEC[
                min(failures - 1, len(FCM_SUPERVISOR_BACKOFF_SEC) - 1)
            ]
            _LOGGER.info(
                "FCM supervisor: start failed — retry in %.0fs (attempt #%d)",
                delay,
                failures,
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
            continue

        # ── Listener running — poll until it dies ──────────────────────────
        _LOGGER.debug(
            "FCM supervisor: listener up — polling every %.0fs", FCM_SUPERVISOR_POLL_SEC
        )
        forced_heal = False
        # Bug fix (F5): `failures` was previously only reset by an actual
        # push arriving, never just by uptime — a listener healthy for days
        # in a quiet house (zero pushes to prove it) could keep the backoff
        # ladder pinned at its LAST recorded failure count (up to 1800s)
        # even though every start since then has succeeded. Reset it once
        # the listener has stayed up for a sustained period, independent of
        # whether a push ever arrived.
        listener_start_ts = time.monotonic()
        failures_reset_for_uptime = False
        soft_trigger_reset_for_uptime = False
        try:
            while True:
                await asyncio.sleep(FCM_SUPERVISOR_POLL_SEC)
                fcm_client = coordinator.fcm_client
                if fcm_client is None or not fcm_client.is_started():
                    break
                if (
                    not failures_reset_for_uptime
                    and failures > 0
                    and (time.monotonic() - listener_start_ts)
                    >= FCM_SUPERVISOR_SUSTAINED_UPTIME_SEC
                ):
                    _LOGGER.info(
                        "FCM supervisor: listener sustained for %.0fs without "
                        "dying — resetting failure/backoff counter",
                        FCM_SUPERVISOR_SUSTAINED_UPTIME_SEC,
                    )
                    failures = 0
                    failures_reset_for_uptime = True
                # Independent of `failures` (which a hard-heal purge already
                # zeroes) — sustained uptime is the recovery signal for the
                # soft-restart-threshold backoff too, see its definition above.
                if (
                    not soft_trigger_reset_for_uptime
                    and soft_trigger_heal_streak > 0
                    and (time.monotonic() - listener_start_ts)
                    >= FCM_SUPERVISOR_SUSTAINED_UPTIME_SEC
                ):
                    _LOGGER.info(
                        "FCM supervisor: listener sustained for %.0fs without "
                        "dying — resetting soft-restart-threshold heal streak",
                        FCM_SUPERVISOR_SUSTAINED_UPTIME_SEC,
                    )
                    soft_trigger_heal_streak = 0
                    soft_trigger_reset_for_uptime = True
                if getattr(coordinator, "fcm_force_hard_heal", False):
                    # Silent-delivery-death: the poll-based fallback detected a
                    # camera event FCM never delivered while is_started() still
                    # reports True. Break out NOW so the top-of-loop hard-heal
                    # fires promptly — otherwise the forced flag is only re-read
                    # once the client independently dies, which in this exact
                    # scenario (the whole reason the flag exists) may not happen
                    # for a long time, or ever.
                    forced_heal = True
                    _LOGGER.info(
                        "FCM supervisor: forced hard-heal requested while listener "
                        "still reported started — restarting to purge credentials"
                    )
                    break
        except asyncio.CancelledError:
            await async_stop_fcm_push(coordinator)
            _LOGGER.debug("FCM supervisor: cancelled while listener was running")
            break
        except Exception:
            # Anything unexpected here (e.g. fcm_client.is_started() raising)
            # used to propagate straight out of _async_run_fcm_supervisor,
            # killing the task outright — recovery then depended on the
            # coordinator-tick watchdog noticing sup.done(), not the designed
            # ~10s poll cadence. Treat it the same as a
            # normal listener termination: fall through to the stop+backoff
            # logic below instead of dying silently.
            _LOGGER.exception(
                "FCM supervisor: exception while polling listener — "
                "treating as terminated"
            )

        if forced_heal:
            _LOGGER.info("FCM supervisor: listener stopped for forced hard-heal")
        else:
            _LOGGER.info("FCM supervisor: listener terminated (is_started()=False)")
        await async_stop_fcm_push(coordinator)

        # ── Choose backoff ─────────────────────────────────────────────────
        push_received = coordinator.fcm_last_push > push_ts_before
        if forced_heal:
            # A hard-heal was explicitly requested (delivery-death watchdog).
            # Restart fast so the top-of-loop credential purge happens promptly
            # instead of sitting on an escalated backoff delay. The flag is left
            # set for the top of the loop to consume.
            delay = FCM_SUPERVISOR_BACKOFF_SEC[0]
            _LOGGER.info(
                "FCM supervisor: applying forced hard-heal — fast restart in %.0fs",
                delay,
            )
        elif push_received:
            # Listener was delivering — transient drop; fast restart, reset counters.
            failures = 0
            soft_streak = 0
            # Bug-hunt finding (2026-09-04): a received push is STRONGER
            # evidence delivery works than FCM_SUPERVISOR_SUSTAINED_UPTIME_SEC
            # of mere uptime (the only other reset for this streak) — without
            # this, a house where pushes reliably arrive but the listener
            # itself restarts every <10 min (never reaching the uptime
            # threshold) would still see this streak climb monotonically
            # toward the 1800s ceiling, delaying benign heals despite proven
            # delivery.
            soft_trigger_heal_streak = 0
            delay = FCM_SUPERVISOR_BACKOFF_SEC[0]
            _LOGGER.info(
                "FCM supervisor: transient drop (had pushes) — fast restart in %.0fs",
                delay,
            )
        else:
            failures += 1
            soft_streak += 1
            delay = FCM_SUPERVISOR_BACKOFF_SEC[
                min(failures - 1, len(FCM_SUPERVISOR_BACKOFF_SEC) - 1)
            ]
            _LOGGER.info(
                "FCM supervisor: no pushes since last start — retry in %.0fs "
                "(failure #%d, soft streak %d/%d)",
                delay,
                failures,
                soft_streak,
                FCM_SUPERVISOR_SOFT_HEAL_MAX,
            )

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            break

    _LOGGER.debug("FCM supervisor stopped")


async def _async_persist_fcm_creds(
    coordinator: Any, creds: dict[str, Any], expected_client: Any = None
) -> None:
    """Write FCM credentials into the config entry (must run in event loop).

    Bug fix (F1): the caller previously checked "is this client still
    current" only at SCHEDULE time (before queuing this as a task), not
    here at actual-write time. Race: old client's callback checks identity
    (passes, still current) -> task queued -> BEFORE it runs, the
    supervisor's hard-heal purges all `fcm_*` keys -> the stale queued task
    then re-writes the old/stale credentials, undoing the purge. Re-check
    identity right before the write, not just at schedule time.

    ``expected_client`` is optional (defaults to None = no re-check) so
    direct/legacy callers that don't have a client reference to compare
    against still persist unconditionally.
    """
    if expected_client is not None and coordinator.fcm_client is not expected_client:
        _LOGGER.debug(
            "FCM: skipping credentials persist — client was replaced/purged "
            "between schedule and write (stale callback)"
        )
        return
    try:
        coordinator.hass.config_entries.async_update_entry(
            coordinator.entry,
            data={**coordinator.entry.data, "fcm_credentials": creds},
        )
        _LOGGER.debug("FCM credentials saved to config entry")
    except Exception as err:
        _LOGGER.debug("FCM creds persist failed: %s", err)


# ── FCM push callback ───────────────────────────────────────────────────────


def _on_fcm_push(
    coordinator: Any, notification: dict[str, Any], persistent_id: str, obj: Any = None
) -> None:
    """Called when a push notification arrives from Bosch CBS.

    The push is a silent wake-up signal with no event payload.
    We immediately trigger an event fetch + snapshot refresh for all cameras.
    """
    with coordinator.fcm_lock:
        # Drop pushes that arrive after async_stop_fcm_push cleared the client —
        # a trailing push would otherwise reschedule async_handle_fcm_push on a
        # loop that already considers FCM down.
        if not coordinator.fcm_running:
            return
        coordinator.fcm_last_push = time.monotonic()
        coordinator.fcm_healthy = True
    _LOGGER.info(
        "FCM push received (id=%s, from=%s) — fetching events",
        persistent_id,
        notification.get("from", "?"),
    )

    # Schedule immediate event fetch + snapshot refresh on the HA event loop.
    # Create + track the task INSIDE the threadsafe callback so it holds a strong
    # reference in bg_tasks — an untracked task can be GC-cancelled mid-flight on
    # shutdown, leaving coordinator.data partially updated.
    def _spawn_fcm_handler() -> None:
        _t = coordinator.hass.async_create_task(async_handle_fcm_push(coordinator))
        coordinator.bg_tasks.add(_t)
        _t.add_done_callback(coordinator.bg_tasks.discard)

    coordinator.hass.loop.call_soon_threadsafe(_spawn_fcm_handler)


def _event_predates_session(coordinator: Any, ev: dict[str, Any]) -> bool:
    """True if ``ev``'s Bosch-cloud timestamp is older than this
    coordinator session's start (minus 60s clock-skew/processing slack).

    Same staleness test `smb.py`'s `sync_local_save` already uses against
    `coordinator._download_started_at` — reused here so a queued/redelivered
    FCM push (Google MCS resends unacked messages on reconnect, see the
    async_handle_fcm_push docstring) or the first push after a fresh
    install (which can surface pre-existing cloud events, not just ones
    that just happened) doesn't get treated as a brand-new real-time event.
    Only meaningful when no `last_event_ids` baseline exists yet for the
    camera — once a baseline exists, `newest_id != prev_id` already proves
    genuine forward progress and no timestamp check is needed.
    """
    ts = ev.get("timestamp", "")
    if not ts or len(ts) < 19:
        return False
    started_at = getattr(coordinator, "_download_started_at", 0.0)
    if not started_at:
        return False
    # Bug fix (B1): the previous `ts[:19]` + calendar.timegm(time.strptime(...))
    # discarded Bosch's real timezone offset (e.g.
    # "2026-06-18T06:06:30.499+02:00[Europe/Berlin]") and re-labelled local
    # wall-clock time as UTC. In CEST (+2h), a genuinely brand-new event was
    # computed as 7200s in the past — right after any HA restart the FIRST
    # real motion event got silently dropped as "stale". Use the shared,
    # timezone-correct parser instead (see time_utils.py's own docstring for
    # the full incident history).
    ev_dt = parse_bosch_timestamp(ts)
    if ev_dt is None:  # unparseable timestamp — don't block on it
        return False
    ev_epoch = ev_dt.timestamp()
    return ev_epoch < started_at - 60


async def async_handle_fcm_push(coordinator: Any, _attempt: int = 0) -> None:
    """Handle an FCM push — fetch fresh events for all cameras and fire HA events.

    Bosch's FCM push can beat its own /v11/events cloud index by a few seconds:
    the first fetch then returns no new event, and the alert would otherwise only
    arrive via the ~300 s safety poll ("alles über das normale pull verhalten").
    When a push finds nothing new, this handler retries a couple of times with a
    short backoff (`_attempt`) so the event is caught within seconds. Dedup via
    alert_sent_ids + last_event_ids makes a re-scan safe (no double alerts).
    """
    token = coordinator.token
    if not token or not coordinator.data:
        # Race: FCM push can arrive during setup, before the first coordinator
        # refresh has populated .data. Without this guard we crash with
        # `AttributeError: 'NoneType' object has no attribute 'keys'`.
        return

    session = await async_get_bosch_cloud_session(coordinator.hass)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # Bug fix (B3): was a single flag shared across ALL cameras in one
    # push-fetch cycle — if camera A's event dispatched but camera B's
    # hadn't landed in the cloud index yet, B's retry got suppressed too,
    # delaying B's alert to the next 300s poll. Scope it per-camera.
    _dispatched_new: dict[str, bool] = {}
    _any_fetch_ok = False  # B1 fix: track if ≥1 camera fetch returned HTTP 200
    for cam_id in list(coordinator.data.keys()):
        try:
            url = f"{CLOUD_API}/v11/events?videoInputId={cam_id}&limit=5"
            async with asyncio.timeout(10):
                async with session.get(url, headers=headers) as r:
                    if r.status != 200:
                        # Bug fix (B2): the non-200 status was previously
                        # dropped with zero logging and no distinction
                        # between a transient blip and a 401 (expired
                        # token, which would otherwise make every push a
                        # silent no-op until the next 300s poll). Log it,
                        # and for a 401 specifically trigger the same
                        # token-refresh path async_put_camera uses.
                        _LOGGER.warning(
                            "FCM push: events fetch for %s returned HTTP %d",
                            cam_id,
                            r.status,
                        )
                        if r.status == 401:
                            try:
                                token = await coordinator.ensure_valid_token(token)
                                headers["Authorization"] = f"Bearer {token}"
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                _LOGGER.debug(
                                    "FCM push: token refresh after 401 failed",
                                    exc_info=True,
                                )
                        continue
                    events = await r.json()
            _any_fetch_ok = True  # HTTP 200 received — cloud is reachable

            if not events:
                continue

            newest_id = events[0].get("id", "")
            prev_id = coordinator.last_event_ids.get(cam_id)

            # Per-event-ID dedup: concurrent FCM handlers (Bosch sometimes
            # sends two pushes ~10 s apart for the same event) otherwise both
            # pass the prev_id check and fire two alert chains.
            _now = time.monotonic()
            _sent = coordinator.alert_sent_ids
            if newest_id and _sent.get(newest_id, float("-inf")) > _now - 60.0:
                _LOGGER.debug(
                    "FCM push dedup: skipping duplicate alert for %s id=%s (already sent %.1fs ago)",
                    cam_id,
                    newest_id[:8],
                    _now - _sent[newest_id],
                )
                continue
            # Evict entries older than 120s on every call. Original
            # `if len(_sent) > 32` guard could starve eviction during
            # burst-event scenarios (4 cams × dense events all within
            # 120 s window → cache grows past 32 but eviction loop finds
            # nothing to evict, so it grows unbounded). Plain age-based
            # cleanup on every call has O(len) cost which is fine — len
            # stays small.
            # NOTE: _sent aliases coordinator.alert_sent_ids — must mutate it
            # IN PLACE (a dict-comprehension rebind would detach the alias and
            # lose every later write at `_sent[newest_id] = _now`). Single-pass
            # collect-then-pop keeps the shared dict intact.
            if _sent:
                _cutoff = _now - 120.0
                for _k in [k for k, v in _sent.items() if v < _cutoff]:
                    del _sent[_k]

            # `prev_id is None` means no baseline has been established yet
            # for this camera (e.g. within the ~60-90s after HA restart
            # before the coordinator's own polling tick seeds
            # `last_event_ids` — see event_dispatch.py's bootstrap
            # comment). Unlike that polling path, an FCM push only ever
            # arrives because Bosch's cloud just observed a genuinely new,
            # real-time event — there is no "historical backlog" to guard
            # against here, so it must never be silently dropped. The old
            # `prev_id is not None` guard treated this exactly like the
            # polling bootstrap case (seed the baseline, don't dispatch,
            # see the `elif newest_id:` below) and swallowed the very
            # first motion event after every restart with zero log trace —
            # root cause of GitHub #64: reporter consistently reproduced
            # by restarting HA and triggering exactly one motion event per
            # test round, which is exactly the event this ate every time.
            if (
                prev_id is None
                and newest_id
                and _event_predates_session(coordinator, events[0])
            ):
                # No baseline yet AND the event itself predates this HA
                # session — not a genuinely new real-time event (a queued/
                # redelivered push, or a fresh install surfacing
                # pre-existing cloud history). Seed the baseline like the
                # `elif newest_id:` fallback below would, but skip
                # dispatch — firing here would trade GitHub #64's silent
                # non-delivery for a false alert/clip instead of fixing it.
                coordinator.last_event_ids[cam_id] = newest_id
                _LOGGER.debug(
                    "FCM push: skipping stale first-seen event for %s id=%s "
                    "(predates this session's start, no baseline yet — "
                    "seeding last_event_ids without dispatch)",
                    cam_id,
                    newest_id[:8],
                )
                continue

            if newest_id and newest_id != prev_id:
                _dispatched_new[cam_id] = True
                # Record alert dispatch ASAP so a concurrent handler sees it
                _sent[newest_id] = _now
                # Update last event ID FIRST to prevent polling from
                # detecting the same event and sending duplicate alerts
                coordinator.last_event_ids[cam_id] = newest_id

                newest_event = events[0]
                event_type = newest_event.get("eventType", "")
                event_tags = newest_event.get("eventTags", []) or []
                cam_name = (
                    coordinator.data.get(cam_id, {})
                    .get("info", {})
                    .get("title", cam_id)
                )

                # Gen2 cameras (Outdoor II w/ DualRadar, Indoor II) send
                # eventType=MOVEMENT with eventTags=["PERSON"] when a human is
                # detected — the tag is more specific than the type, so upgrade.
                if "PERSON" in event_tags and event_type == "MOVEMENT":
                    event_type = "PERSON"

                # Diagnostic only: logs
                # Bosch's own event timestamp alongside our local wall-clock
                # receipt time, so a Bosch-cloud-side delay (movement/person
                # events issued close together after server-side AI analysis)
                # can be distinguished from an integration-side one by
                # comparing bosch_ts across consecutive events for a camera.
                # Full (not truncated) id, so a line here can be correlated
                # with the equivalent line in event_dispatch.py. Also logs
                # the next-newest event's own timestamp when present — if
                # Bosch batches a MOVEMENT+PERSON pair into the same
                # /v11/events response, only events[0] gets dispatched here,
                # so without this, one half of the pair we want to compare
                # would never appear in the log at all.
                _prev_event = events[1] if len(events) > 1 else None
                _LOGGER.debug(
                    "FCM push timing: %s event for %s (id=%s, bosch_ts=%s, "
                    "received_at=%s, prev_event_bosch_ts=%s)",
                    event_type,
                    cam_name,
                    newest_id,
                    newest_event.get("timestamp", ""),
                    dt_util.utcnow().isoformat(),
                    _prev_event.get("timestamp", "") if _prev_event else "n/a",
                )

                _LOGGER.info(
                    "FCM push -> new %s event for %s (id=%s, tags=%s)",
                    event_type,
                    cam_name,
                    newest_id[:8],
                    event_tags,
                )

                # Update cached events (next coordinator tick rebuilds data[]).
                coordinator.cached_events[cam_id] = events
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
                    "camera_id": cam_id,
                    "camera_name": cam_name,
                    "timestamp": newest_event.get("timestamp", ""),
                    "image_url": newest_event.get("imageUrl", ""),
                    "event_id": newest_id,
                    "source": "fcm_push",
                }
                if event_type == "MOVEMENT":
                    coordinator.hass.bus.async_fire(
                        "bosch_shc_camera_motion", event_payload
                    )
                elif event_type == "AUDIO_ALARM":
                    coordinator.hass.bus.async_fire(
                        "bosch_shc_camera_audio_alarm", event_payload
                    )
                elif event_type == "PERSON":
                    coordinator.hass.bus.async_fire(
                        "bosch_shc_camera_person", event_payload
                    )

                # Mini-NVR event_buffered clip assembly: on a movement/person
                # event for a camera in event_buffered mode with the NVR
                # switch ON and LOCAL, assemble
                # the pre-roll(+post-roll) clip and drop it into the NVR staging
                # tree so the existing drain watcher ships it. Independent of the
                # notification switches below — a user may want clips without
                # push alerts (or vice versa). Shared with event_dispatch.py's
                # polling path (GitHub #64 follow-up, 2026-08-13) — see
                # `maybe_schedule_nvr_motion_clip`'s docstring for why this
                # can no longer live only here.
                maybe_schedule_nvr_motion_clip(
                    coordinator,
                    cam_id,
                    event_type,
                    event_timestamp=newest_event.get("timestamp", ""),
                    source="fcm_push",
                )

                # Check notification switches before sending alert.
                # Master switch must be ON, AND the type-specific switch must
                # be ON for this event type.
                #
                # Bug fix (B5): the entity_id used to be hand-rebuilt from
                # `cam_name` (spaces + ä/ö/ü only) instead of resolved via
                # the entity registry — on non-trivial camera names (other
                # punctuation, non-German special chars) the lookup missed,
                # `_alert_blocked` silently stayed False, and alerts fired
                # even with the master notifications switch OFF (fail-open,
                # privacy-relevant). Resolve the real entity_ids by unique_id
                # instead — same unique_id scheme switch.py itself registers
                # (NOTIFICATIONS_DESCRIPTION / BoschNotificationTypeSwitch).
                _alert_blocked = False
                _ent_reg = er.async_get(coordinator.hass)
                _master_eid = _ent_reg.async_get_entity_id(
                    "switch", DOMAIN, f"bosch_shc_notifications_{cam_id.lower()}"
                )
                if _master_eid:
                    _master_state = coordinator.hass.states.get(_master_eid)
                    if _master_state and _master_state.state == "off":
                        _LOGGER.debug("Alert suppressed: %s is OFF", _master_eid)
                        _alert_blocked = True
                # Type-specific check. Map raw event types to the `ntype`
                # value BoschNotificationTypeSwitch uses in its own
                # unique_id (`bosch_shc_camera_{cam_id}_notif_{ntype}`) —
                # NOTE this is the camelCase API-native form ("cameraAlarm"),
                # not a snake_case slug. TROUBLE_CONNECT + TROUBLE_DISCONNECT
                # both follow the `trouble` switch — they're system events
                # and can be silenced together without affecting motion/
                # person alerts.
                _type_map = {
                    "MOVEMENT": "movement",
                    "PERSON": "person",
                    "AUDIO_ALARM": "audio",
                    "CAMERA_ALARM": "cameraAlarm",
                    "TROUBLE": "trouble",
                    "TROUBLE_CONNECT": "trouble",
                    "TROUBLE_DISCONNECT": "trouble",
                }
                _type_key = _type_map.get(event_type)
                if _type_key and not _alert_blocked:
                    _type_eid = _ent_reg.async_get_entity_id(
                        "switch", DOMAIN, f"bosch_shc_camera_{cam_id}_notif_{_type_key}"
                    )
                    if _type_eid:
                        _type_state = coordinator.hass.states.get(_type_eid)
                        if _type_state and _type_state.state == "off":
                            _LOGGER.debug("Alert suppressed: %s is OFF", _type_eid)
                            _alert_blocked = True

                if not _alert_blocked:
                    # Send alert notification (3-step: text + snapshot + video).
                    # Track in bg_tasks: async_send_alert runs ~minutes (image
                    # retries + clip poll/download); an untracked task can be
                    # GC-cancelled mid-flight on shutdown, leaving partial files.
                    _alert_task = coordinator.hass.async_create_task(
                        async_send_alert(
                            coordinator,
                            cam_name,
                            event_type,
                            newest_event.get("timestamp", ""),
                            newest_event.get("imageUrl", ""),
                            newest_event.get("videoClipUrl", ""),
                            newest_event.get("videoClipUploadStatus", ""),
                            event_id=newest_id,
                            cam_id=cam_id,
                        )
                    )
                    coordinator.bg_tasks.add(_alert_task)
                    _alert_task.add_done_callback(coordinator.bg_tasks.discard)
                else:
                    _LOGGER.info(
                        "Alert skipped for %s (%s) — notifications disabled",
                        cam_name,
                        event_type,
                    )

                # Path A — live-snap refresh: fire immediately on every real event so
                # the frontend gets a fresh camera frame within ~1-2 s of the event.
                # _SNAP_EVENT_TYPES (module-level) excludes status-only types.
                # WHY tracked: fire-and-forget tasks get GC-collected on HA shutdown
                # mid-flight, leaving half-written temp files. Strong reference +
                # discard callback allows async_unload_entry to cancel+await cleanly.
                cam_entity = coordinator.camera_entities.get(cam_id)
                if cam_entity and event_type in _SNAP_EVENT_TYPES:
                    # Stream-contention guard: while the RTSP live-stream is active,
                    # Path A's live-snap refresh (PUT /connection + snap.jpg) competes
                    # with the RTSP OPTIONS keepalive on the camera's single TLS
                    # control channel.  On Gen2 the 30-s RTSP session timeout means
                    # a delayed OPTIONS response (>30 s) tears down the producer →
                    # 5–10 s stream freeze.  Path B (alert step-2 in async_send_alert)
                    # already pushes the Bosch event image (with AI overlay) into
                    # cached_image via the same cloud session that fetches the
                    # notification snapshot — no extra camera-side TLS request needed.
                    # Skip Path A entirely when is_streaming=True; Path B is sufficient.
                    # Source: knowledge-base/stream-freeze-on-motion-event-contention.md
                    #
                    # Bug fix (B4, second half): Path B (alert step-2) only
                    # runs when the alert isn't `_alert_blocked` (user has
                    # notifications off). Skipping Path A purely on
                    # `is_streaming` — without checking whether Path B will
                    # actually run — left a streaming user with
                    # notifications off with NO snapshot refresh at all on a
                    # real event. The two skip-conditions must not both
                    # apply simultaneously.
                    if (
                        getattr(cam_entity, "is_streaming", False)
                        and not _alert_blocked
                    ):
                        _LOGGER.debug(
                            "FCM Path A: skipped for %s (%s) — camera is streaming, "
                            "Path B will update cache",
                            cam_name,
                            event_type,
                        )
                    else:
                        try:
                            # Per-model settle delay — Gen2 captures immediately (0 s),
                            # Gen1 needs ~1.5 s so the snap reflects the post-trigger frame.
                            from .models import get_model_config

                            hw_cache = getattr(coordinator, "hw_version", {})
                            hw = (
                                hw_cache.get(cam_id, "")
                                if hasattr(hw_cache, "get")
                                else ""
                            )
                            refresh_delay = get_model_config(hw).event_refresh_delay
                            task = coordinator.hass.async_create_task(
                                cam_entity.async_trigger_image_refresh(
                                    delay=refresh_delay
                                )
                            )
                            coordinator.bg_tasks.add(task)
                            task.add_done_callback(coordinator.bg_tasks.discard)
                            _LOGGER.debug(
                                "FCM Path A: live-snap refresh scheduled for %s (%s, delay=%.1fs)",
                                cam_name,
                                event_type,
                                refresh_delay,
                            )
                        except Exception as _snap_err:
                            _LOGGER.warning(
                                "FCM Path A: failed to schedule live-snap refresh for %s: %s",
                                cam_name,
                                _snap_err,
                            )

                # Notify all entity listeners
                coordinator.async_update_listeners()

                # Mark new event as read on the Bosch cloud (gated by user option).
                # BUG-4 fix: fire-and-forget via async_create_task so cameras
                # 2/3/4 are not blocked for up to 5s by camera 1's mark-read
                # HTTP PUT inside the per-cam loop.
                if coordinator.options.get("mark_events_read", False):

                    async def _mark_read_bg(
                        _coord: Any = coordinator, _eid: str = newest_id
                    ) -> None:
                        try:
                            await async_mark_events_read(_coord, [_eid])
                        except Exception:  # noqa: S110 # best-effort cloud housekeeping
                            pass

                    _mr_task = coordinator.hass.async_create_task(_mark_read_bg())
                    coordinator.bg_tasks.add(_mr_task)
                    _mr_task.add_done_callback(coordinator.bg_tasks.discard)

            elif newest_id:
                coordinator.last_event_ids[cam_id] = newest_id

        except (TimeoutError, aiohttp.ClientError) as err:
            # Transient cloud hiccup — the retry/backoff loop below (and the
            # 300 s safety poll) recover from it without operator action.
            # → DEBUG, not WARNING.
            _LOGGER.debug("FCM push event fetch network error for %s: %s", cam_id, err)
        except Exception as err:
            _LOGGER.debug("FCM push event fetch error for %s: %s", cam_id, err)

    # Push beat the cloud index → no new event this pass. Retry a couple of
    # times with a short backoff before falling back to the 300 s safety poll.
    # B1 fix: only retry when ≥1 fetch succeeded (HTTP 200) — if ALL cameras
    # failed with TimeoutError/ClientError the cloud endpoint is down and
    # retrying wastes round-trips + adds 2+4 s of sleep on a dead endpoint.
    _FCM_FETCH_RETRY_BACKOFFS = (2.0, 4.0)
    if (
        not _dispatched_new
        and _any_fetch_ok
        and _attempt < len(_FCM_FETCH_RETRY_BACKOFFS)
    ):
        await asyncio.sleep(_FCM_FETCH_RETRY_BACKOFFS[_attempt])
        if getattr(coordinator, "fcm_running", False):
            await async_handle_fcm_push(coordinator, _attempt + 1)


# ── Alert routing helpers ────────────────────────────────────────────────────


def get_alert_services(coordinator: Any, type_key: str) -> list[str]:
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
    svc: str,
    message: str,
    file_path: str | None = None,
    title: str | None = None,
    camera_entity_id: str | None = None,
) -> dict[str, Any]:
    """Build notify service call data with correct attachment format per service type.

    mobile_app (iOS + Android HA Companion): image served from /local/bosch_alerts/
    telegram_bot: uses photo field
    All others (Signal, email, ...): file path in data.attachments

    camera_entity_id: only honoured for mobile_app services, only when the
    caller resolved one (alert_notify_live_preview opt-in). This is the
    Companion App's own dynamic-content mechanism (documented for
    `entity_id:` in HA's own notification docs, iOS only — the Android
    Companion App's notification attachments only support a static
    camera_proxy image, not this live-stream mechanism): the app fetches
    its own fresh thumbnail on delivery and opens a live camera feed inside
    the notification when the recipient expands it. Additive to `image`,
    not a replacement — the static snapshot below is still attached and
    still shows instantly. Deliberately doesn't set
    `push.sound` here — that must stay exactly what it already was for the
    plain-text (no snapshot) case (nothing, i.e. no `data` key at all)
    unless a snapshot is also attached below; this option only adds a
    preview, it must not silently start forcing a sound on step-1 text
    alerts that never had one before.
    """
    data: dict[str, Any] = {"message": message}
    if title:
        data["title"] = title
    is_mobile_app = "mobile_app" in svc

    if not file_path:
        if is_mobile_app and camera_entity_id:
            data["data"] = {"entity_id": camera_entity_id}
        return data

    fname = os.path.basename(file_path)
    if is_mobile_app:
        # HA Companion App — image URL served without auth from /config/www/
        # Files deleted within seconds when alert_save_snapshots=False
        #
        # Bug fix (BUG2): `fname` embeds the cloud-provided camera title
        # (only lightly sanitised by `_safe_path_segment` — path separators
        # are stripped but `#`/`?`/`%`/spaces are not) and was previously
        # placed into this URL unencoded, producing a broken URL in the push
        # notification even though the file exists on disk correctly under
        # that exact name. URL-encode only here, at URL-construction time —
        # the filesystem path itself (used for the actual file I/O above)
        # must stay as-is.
        notify_data: dict[str, Any] = {
            "image": f"/local/bosch_alerts/{urllib.parse.quote(fname)}",
            "push": {"sound": "default"},  # iOS: play sound; Android ignores this key
        }
        if camera_entity_id:
            notify_data["entity_id"] = camera_entity_id
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
    coordinator: Any,
    cam_name: str,
    event_type: str,
    timestamp: str,
    image_url: str,
    clip_url: str = "",
    clip_status: str = "",
    event_id: str = "",
    cam_id: str = "",
) -> None:
    """Send a 3-step alert: instant text, snapshot image, video clip.

    Step 1: Immediate text notification (no delay)
    Step 2: Download snapshot from Bosch cloud (after 5s), send with image
    Step 3: Download video clip (after 15s total), send as attachment

    cam_id: stable camera ID (UUID). When provided, all sub-lookups use it
    directly instead of searching coordinator.data by the mutable title string.
    Callers that cannot supply cam_id (legacy / __init__ wrapper) leave it as
    "" and the title-fallback is used instead.
    """
    from .smb import sync_local_save, sync_smb_upload

    # Bosch has been observed sending "timestamp": null in event payloads;
    # newest_event.get("timestamp", "") only substitutes the default when the
    # key is ABSENT, not when its value is JSON null, so a bare None could
    # reach here and crash len(timestamp)/timestamp[:19] below. This runs
    # inside an untracked-by-caller hass.async_create_task, so an unguarded
    # TypeError here was silently swallowed by asyncio's default exception
    # handler — the HA event bus fired fine, but the text/snapshot/clip
    # notification steps never ran, with no visible symptom.
    timestamp = timestamp or ""

    opts = coordinator.options

    # Resolve the stable cam_id once at push-receipt time (start of coroutine).
    # Doing this early ensures all sub-lookups (Path B, Step 3, AI title-match)
    # use the stable ID rather than the mutable display title — fixes B04-BUG-2
    # and W-imageflip-BUG-2 (stale privacy / wrong cam on rename).
    _resolved_cam_id: str | None = cam_id if cam_id else None
    if not _resolved_cam_id:
        for _cid, _cdata in coordinator.data.items():
            if _cdata.get("info", {}).get("title", "") == cam_name:
                _resolved_cam_id = _cid
                break

    # Bug fix (Area 5, item 2): capture the event_id THIS alert call is for
    # ONCE, here, at the very start. The flow below runs for up to ~90s
    # (clip-ready polling); re-deriving a missing event_id from
    # `coordinator.last_event_ids` at each later point risked picking up a
    # NEWER, different, un-alerted event that had advanced in the meantime —
    # marking the wrong event read and using the wrong id for the clip
    # lookup/SMB path. Every later use of `event_id` in this function must
    # read this same captured value, never re-read the coordinator's cache.
    if not event_id and _resolved_cam_id:
        event_id = coordinator.last_event_ids.get(_resolved_cam_id, "")

    # Capture privacy state NOW (at push-receipt time, start of coroutine).
    # Path B runs up to ~30 s later; re-reading the live cache at that point
    # can pick up a post-privacy-off value and write a pre-privacy frame into
    # the cache — fixing W-imageflip-BUG-2.
    _shc_cache_early = getattr(coordinator, "shc_state_cache", {})
    _push_time_priv: bool = (
        _shc_cache_early.get(_resolved_cam_id, {}).get("privacy_mode", False)
        if _resolved_cam_id
        else False
    )
    # Bug fix (B4, first half): Path A (live-snap refresh) and Path B (this
    # coroutine's event-driven cache write, below) can race — Path B could
    # unconditionally overwrite the cache with an OLDER Bosch event frame
    # after Path A already wrote a FRESHER live frame. Capture "now" at
    # coroutine start so the Path B write below can detect whether a newer
    # frame (from Path A or anything else) landed in the cache while this
    # alert was in flight, and skip the stale overwrite if so.
    _pb_started_at: float = time.monotonic()

    # Per-type service routing: information/screenshot/video each fall back to alert_notify_service.
    # TROUBLE events use "system" — check that before bailing on missing information services.
    _is_trouble = event_type in ("TROUBLE_CONNECT", "TROUBLE_DISCONNECT")
    info_svcs = get_alert_services(coordinator, "information")
    _has_local_save = bool(opts.get("enable_local_save") and opts.get("download_path"))
    _has_smb_upload = bool(opts.get("enable_smb_upload") and opts.get("smb_server"))
    # GitHub #68 live-deploy finding, 2026-08-18: this guard only checked
    # "information" services (with alert_notify_service as its fallback) —
    # a user configuring ONLY alert_notify_screenshot/alert_notify_video (a
    # natural "just send me the picture" setup, no information/default
    # service set) had the ENTIRE alert silently no-op here, with zero log
    # line, before steps 2/3 ever ran. "screenshot"/"video" deliberately
    # don't fall back to alert_notify_service (see get_alert_services'
    # docstring), so they must be checked here explicitly, the same way
    # steps 2/3 already look them up individually. The extra
    # get_alert_services() calls are inline (not pre-computed) so they stay
    # lazily short-circuited exactly like the pre-existing `info_svcs`
    # check — the common case (information services configured) still does
    # only the one lookup already done above.
    if (
        not info_svcs
        and not get_alert_services(coordinator, "screenshot")
        and not get_alert_services(coordinator, "video")
        and not _is_trouble
        and not _has_local_save
        and not _has_smb_upload
    ):
        _LOGGER.debug(
            "async_send_alert: nothing configured for %s (no notify "
            "services, no local save, no SMB upload) — skipping",
            event_type,
        )
        return  # Nothing to do (no notifications, no local save, no SMB upload)

    # alert_notify_live_preview opt-in: resolve the real camera entity_id via
    # the entity registry (same unique_id scheme camera.py itself registers,
    # `bosch_shc_cam_{cam_id.lower()}`) so mobile_app_* notify payloads below
    # can carry it. None (default) means build_notify_data() falls back to
    # its pre-existing image-only behaviour unchanged. Deliberately skipped
    # for TROUBLE_CONNECT/TROUBLE_DISCONNECT (_is_trouble): a connectivity
    # alert about a camera being unreachable has no meaningful live feed to
    # offer, and for TROUBLE_DISCONNECT specifically the camera is by
    # definition offline — attaching entity_id would just make every
    # recipient device's app fail to fetch anything. Also skipped when
    # enable_snapshots is OFF: no camera entity exists in that case (see
    # camera.py's early return), so a leftover entity-registry entry from a
    # previous run would resolve to a dead entity_id.
    _live_preview_entity_id: str | None = None
    if (
        opts.get("alert_notify_live_preview")
        and not _is_trouble
        and opts.get("enable_snapshots", True)
        and _resolved_cam_id
    ):
        _live_preview_entity_id = er.async_get(coordinator.hass).async_get_entity_id(
            "camera", DOMAIN, f"bosch_shc_cam_{_resolved_cam_id.lower()}"
        )

    # alert_save_snapshots is the sole authority over whether files in
    # www/bosch_alerts/ get deleted after sending — its own description
    # ("if OFF, files are deleted within seconds after sending") must hold
    # unconditionally. alert_delete_after_send used to additionally gate the
    # actual os.remove() call, so leaving it OFF (its own text: "OFF = files
    # kept for reference") silently defeated alert_save_snapshots=OFF and
    # let files accumulate forever in www/bosch_alerts/. It is
    # now read only for backward-compat option-schema presence and has no
    # effect on cleanup — deletion is decided by alert_save_snapshots alone.
    save_snapshots = opts.get("alert_save_snapshots", False)
    ts_short = timestamp[11:19] if len(timestamp) >= 19 else timestamp

    # Event type → German label + emoji icon.
    # PERSON events are eventType=MOVEMENT + eventTags=["PERSON"] (Gen2
    # DualRadar) — the caller is expected to have already upgraded event_type
    # from "MOVEMENT" to "PERSON" when the tag is present (see __init__.py +
    # fcm.py push path).
    type_label = {
        "MOVEMENT": "Bewegung",
        "PERSON": "Person erkannt",
        "AUDIO_ALARM": "Audio-Alarm",
        "TROUBLE_CONNECT": "Verbindung hergestellt",
        "TROUBLE_DISCONNECT": "Verbindung getrennt",
        "CAMERA_ALARM": "Kamera-Alarm",
    }.get(event_type, event_type)
    type_icon = {
        "MOVEMENT": "\U0001f4f7",  # 📷
        "PERSON": "\U0001f9d1",  # 🧑
        "AUDIO_ALARM": "\U0001f50a",  # 🔊
        "TROUBLE_CONNECT": "\U0001f7e2",  # 🟢
        "TROUBLE_DISCONNECT": "\U0001f534",  # 🔴
        "CAMERA_ALARM": "\U0001f6a8",  # 🚨
    }.get(event_type, "\u26a0\ufe0f")  # ⚠️ fallback

    # www/bosch_alerts/ is served as /local/bosch_alerts/ with no auth — the HA
    # mobile_app push handler fetches the notification image URL directly at
    # the OS level, outside any HA session/auth context, so it cannot live
    # behind an authenticated view. A 128-bit random token per alert (below)
    # makes the served filename unguessable — HA's static file route doesn't
    # expose directory listing, so without the token an attacker has nothing
    # to enumerate or predict, unlike the previous camera-name+timestamp name.
    alert_dir = os.path.join(coordinator.hass.config.config_dir, "www", "bosch_alerts")
    await coordinator.hass.async_add_executor_job(os.makedirs, alert_dir, 0o755, True)
    ts_safe = timestamp[:19].replace(":", "-").replace("T", "_")
    alert_token = secrets.token_urlsafe(16)
    session = await async_get_bosch_cloud_session(coordinator.hass)
    headers = {"Authorization": f"Bearer {coordinator.token}", "Accept": "*/*"}
    files_to_cleanup: list[str] = []
    # Snapshot bytes captured in step 2 — passed to SMB/FTP upload so the
    # upload can use the already-in-memory bytes instead of re-downloading
    # from Bosch cloud (which would contend with the RTSP live-stream's TLS
    # control channel).  None until step 2 successfully downloads the image.
    _prefetched_snapshot: bytes | None = None

    async def _notify_type(
        type_key: str, message: str, file_path: str | None = None
    ) -> bool:
        """Send to services configured for this alert type (information/screenshot/video).

        Returns True iff at least one configured service was ACTUALLY
        delivered (the `hass.services.async_call` completed without
        raising) — not merely configured. Two distinct ways this can be
        False: get_alert_services() returned an empty list (e.g.
        "screenshot"/"video" don't fall back to alert_notify_service, so an
        unset alert_notify_video means zero services here), or every
        configured service's call raised (each failure is still logged at
        WARNING here, but that's easy to miss — the caller's "sent"/"skipped"
        summary log must not claim delivery either way). A naive
        `bool(services)` check would be true whenever anything was
        CONFIGURED, even if every single call failed — misreporting
        "attempted" as "delivered".

        `blocking=True` (GitHub #68 live-deploy finding, 2026-08-18): HA
        core's `ServiceRegistry.async_call` defaults to `blocking=False`,
        which schedules the handler as a fire-and-forget background task and
        returns immediately — any exception from the actual notify handler
        (SMTP down, a rejected push, a Signal/Telegram API error) is caught
        and logged entirely inside HA core's own wrapper, never reaching the
        `except Exception` below. Without blocking, `delivered` and every
        "Alert step N sent" log downstream is true whenever the service
        merely EXISTS, regardless of whether it actually delivered —
        exactly the attempted-vs-delivered distinction this docstring
        promises. Blocking trades a little latency (this is an
        event-triggered alert path, not a hot loop) for that guarantee
        actually holding.
        """
        services = get_alert_services(coordinator, type_key)
        delivered = False
        for svc in services:
            try:
                domain, service = svc.split(".", 1)
                call_data = build_notify_data(
                    svc, message, file_path, camera_entity_id=_live_preview_entity_id
                )
                await coordinator.hass.services.async_call(
                    domain, service, call_data, blocking=True
                )
                delivered = True
            except Exception as err:
                _LOGGER.warning("Alert send failed for %s (%s): %s", svc, type_key, err)
        return delivered

    # -- Step 1: Instant text alert ----------------------------------------
    # TROUBLE_CONNECT/DISCONNECT are connectivity events — route to "system",
    # not "information", and skip snapshot/clip steps (no media for these).
    _step1_key = "system" if _is_trouble else "information"
    try:
        _step1_sent = await _notify_type(
            _step1_key, f"{type_icon} {cam_name}: {type_label} ({ts_short})"
        )
        if _step1_sent:
            _LOGGER.debug("Alert step 1 (text) sent via %s", _step1_key)
        else:
            _LOGGER.debug(
                "Alert step 1 (text) NOT delivered via %s — either no notify"
                " service is configured (and alert_notify_service is also"
                " unset), or the configured service call(s) failed (see any"
                " 'Alert send failed' warning above)",
                _step1_key,
            )
    except Exception as err:
        _LOGGER.warning("Alert step 1 failed: %s", err)
        return

    if _is_trouble:
        return  # No snapshot/clip for connectivity events

    # -- Step 2: Snapshot image (after 3s, retries up to ~25s) ------------
    # The FCM push sometimes arrives before Bosch's event API has the imageUrl
    # populated. A single re-fetch at 5s can miss slow-cloud events (text
    # alert sent, snapshot silently skipped, JPG only appearing much later
    # via the SMB upload path). Retry at +3 / +10 / +25 s cumulative — covers
    # steady-state cloud and warm-up cases without delaying the common path
    # noticeably.
    #
    # Track whether image_url was empty at push-arrival time.
    # The 5s sleep before downloading is only needed when the URL was missing
    # on push arrival (retry loop already introduces cumulative delays for that
    # case, so the extra sleep is for the "URL present from the start" path only
    # — but it's unnecessary there too since Bosch's image is already ready if
    # the URL was provided). Move the sleep inside the empty-URL branch so the
    # fast path (URL known upfront) skips the 5s stall entirely.
    # Default False — only set True on an actually-delivered notify call.
    # Referenced by the "mark event read" gate at the end of this function
    # (Area 5, item 1 bug fix), which must see False, not an
    # UnboundLocalError, when step 2/3 never ran or never delivered.
    _step2_sent = False
    _step3_sent = False
    _image_url_was_empty = not image_url
    if _image_url_was_empty:
        # Use the stable cam_id resolved at push-receipt time (B04-BUG-2 fix).
        # Querying with an empty videoInputId returns EVERY camera's events and
        # event[0] would attach a foreign camera's image to this alert.
        events_url = (
            f"{CLOUD_API}/v11/events?videoInputId={_resolved_cam_id}&limit=5"
            if _resolved_cam_id
            else None
        )
        if events_url is None:
            _LOGGER.debug(
                "Alert: no camera matches title %r — skipping image re-fetch",
                cam_name,
            )
        for attempt, delay in enumerate((3, 7, 15), start=1):
            if events_url is None:
                break
            await asyncio.sleep(delay)
            try:
                async with asyncio.timeout(10):
                    async with session.get(events_url, headers=headers) as r:
                        if r.status == 200:
                            fresh_events = await r.json()
                            if fresh_events:
                                image_url = fresh_events[0].get("imageUrl", "")
                                clip_url = (
                                    fresh_events[0].get("videoClipUrl", "") or clip_url
                                )
                                clip_status = (
                                    fresh_events[0].get("videoClipUploadStatus", "")
                                    or clip_status
                                )
            except Exception as err:
                _LOGGER.debug("Alert: re-fetch attempt %d failed: %s", attempt, err)
                continue
            if image_url:
                _LOGGER.debug("Alert: re-fetched image_url on attempt %d", attempt)
                break
        if not image_url:
            _LOGGER.debug(
                "Alert: image_url still empty after 3 retries — skipping step 2"
            )

    # Reject an unsafe imageUrl BEFORE the download block so a rejected URL can
    # never reach session.get() (previously it set image_url="" but still fell
    # through to attempt the fetch with an empty URL).
    if image_url and not _is_safe_bosch_url(image_url):
        _LOGGER.warning("Alert: unsafe imageUrl rejected: %s", image_url[:60])
        image_url = ""

    if image_url:
        # Only wait when the URL was missing at push time and had to be
        # re-fetched — in that case the retry loop already slept up to 25 s,
        # but a brief extra settle avoids a race where Bosch's image is still
        # being finalized after the URL first appears.  When the URL was
        # provided with the original push the image is already ready and the
        # sleep is a pure 5 s stall with no benefit (BUG-5 fix).
        if _image_url_was_empty:
            await asyncio.sleep(2)
        # Neutralise path traversal: cam_name is the cloud-provided camera title
        # and must never escape alert_dir (e.g. a title like "../../config/secrets").
        # ts_safe and event_type are integration-generated, but sanitise defensively.
        snap_path = os.path.join(
            alert_dir,
            f"{_safe_path_segment(cam_name)}_{_safe_path_segment(ts_safe)}"
            f"_{_safe_path_segment(event_type)}_{alert_token}.jpg",
        )
        # Bug fix (BUG1): the download (bounded, must stay inside
        # asyncio.timeout(15)) is now isolated from everything that follows
        # it (AI description, notify, cleanup registration, Path B) — see
        # the AI-description comment below for the full incident this
        # closes.
        data: bytes | None = None
        _snap_status: int | None = None
        _snap_content_type = ""
        try:
            async with asyncio.timeout(15):
                async with session.get(image_url, headers=headers) as resp:
                    _snap_content_type = resp.headers.get("Content-Type", "")
                    _snap_status = resp.status
                    if resp.status == 200 and "image" in _snap_content_type:
                        data = await resp.read()
        except Exception as err:
            _LOGGER.warning("Alert step 2 (screenshot) download failed: %s", err)
            data = None

        if _snap_status is not None and not (
            _snap_status == 200 and "image" in _snap_content_type
        ):
            # No else-branch below (the 200+image body is large and deeply
            # nested) — log here instead so an expired/404/410 snapshot URL
            # doesn't skip step 2 with zero trace, making delivery failures
            # undiagnosable.
            _LOGGER.debug(
                "Alert step 2 (screenshot) skipped for %s: HTTP %s content-type=%r",
                cam_name,
                _snap_status,
                _snap_content_type,
            )

        if data:
            try:
                # Capture bytes for SMB/FTP upload (avoid re-download).
                _prefetched_snapshot = data
                try:
                    await coordinator.hass.async_add_executor_job(
                        _write_file, snap_path, data
                    )
                except Exception:
                    # Bug fix (Area 5, item 5): a write failure partway
                    # through (e.g. ENOSPC) can still leave a partial file
                    # on disk — check for and register it before
                    # re-raising, so it doesn't leak forever even with
                    # cleanup enabled. The success path below registers
                    # unconditionally (no need to stat the file we just
                    # wrote successfully ourselves).
                    if (
                        not save_snapshots
                        and await coordinator.hass.async_add_executor_job(
                            os.path.exists, snap_path
                        )
                    ):
                        files_to_cleanup.append(snap_path)
                    raise
                if not save_snapshots:
                    files_to_cleanup.append(snap_path)
                # Bug fix (BUG1): cleanup is now registered unconditionally
                # right after the write (above), regardless of anything
                # downstream (AI description, notify calls). Previously it
                # only happened after those steps, ALL still nested inside
                # the SAME asyncio.timeout(15) as the download — if the AI
                # call (own 20s internal timeout, longer than what was left
                # of the outer 15s budget after the download) ran long, the
                # outer timeout fired first and cleanup registration was
                # never reached, leaking the already-written JPG forever
                # (still served unauthenticated at /local/bosch_alerts/...).

                caption = f"\U0001f4f8 {cam_name} Snapshot ({ts_short})"
                # F2: optionally append an AI description of the snapshot to
                # the push. Rate-limited + daily-budgeted in
                # async_generate_ai_description.
                #
                # Bug fix (BUG1): this used to run INSIDE the download's
                # asyncio.timeout(15) block. A slow AI call could let the
                # OUTER timeout fire first (a TimeoutError from cancellation,
                # not caught by the narrower `except Exception` around just
                # the AI call at the time), propagating past step 2 entirely
                # — text alert sent, screenshot never delivered. Give the AI
                # call its own independent timeout budget, entirely outside
                # the download's timeout scope and after cleanup is already
                # registered above, so it can never affect screenshot
                # delivery or its cleanup registration.
                if opts.get("ai_notify_include_description"):
                    try:
                        # Use stable cam_id resolved at push-receipt time
                        # (B04-BUG-2: title-match fails on rename).
                        _ai_cid: str | None = _resolved_cam_id
                        if _ai_cid:
                            async with asyncio.timeout(20):
                                _desc = await coordinator.async_generate_ai_description(
                                    _ai_cid
                                )
                            if _desc:
                                _desc = _desc[:200].rstrip()
                                caption = f"{caption}\n\U0001f916 {_desc}"
                    except Exception as _ai_err:
                        _LOGGER.debug("AI notify-include failed: %s", _ai_err)
                _step2_sent = await _notify_type(
                    "screenshot",
                    caption,
                    snap_path,
                )
                if _step2_sent:
                    _LOGGER.debug("Alert step 2 (screenshot) sent: %s", snap_path)
                else:
                    _LOGGER.debug(
                        "Alert step 2 (screenshot) NOT delivered: "
                        "either alert_notify_screenshot is unset "
                        "(no fallback to alert_notify_service for "
                        "this step), or the configured service "
                        "call(s) failed (see any 'Alert send "
                        "failed' warning above): %s",
                        snap_path,
                    )

                # Path B — push the Bosch event image (with AI overlay /
                # motion box) into the camera entity cache so the image
                # entity gets a second update ~5-30 s after Path A's snap.
                #
                # Fixes applied here:
                #   W-imageflip-BUG-2: use _push_time_priv (captured at
                #     coroutine start) instead of re-reading the live cache.
                #     Re-reading can see privacy=False if privacy was turned
                #     off after the event arrived, writing a pre-privacy
                #     frame into cache.
                #   B04-BUG-1: use byte-identity (_existing != data) not
                #     byte-length (len mismatch) for dedup.  Same-length
                #     different-content images (same scene, same quality)
                #     would be incorrectly skipped with len comparison.
                #   B04-BUG-2: use _resolved_cam_id (stable, push-time)
                #     instead of title-lookup that fails on rename.
                #
                # Wrapped in try/except so any error here never affects
                # the alert pipeline (cleanup, clip download, etc.).
                try:
                    _cam_id_for_b: str | None = _resolved_cam_id
                    if _cam_id_for_b:
                        _cam_entities = getattr(coordinator, "camera_entities", {})
                        _cam_b = _cam_entities.get(_cam_id_for_b)
                        # Use privacy state captured at push-receipt time
                        # (W-imageflip-BUG-2 fix — not re-read from cache).
                        if _cam_b and not _push_time_priv:
                            _existing = _cam_b.cached_image
                            # Bug fix (B4, first half): a newer
                            # frame (Path A or another update)
                            # may have landed in the cache while
                            # this alert's own image download was
                            # in flight — don't clobber it with
                            # our older event-time frame.
                            _cached_ts = getattr(_cam_b, "last_image_fetch", 0.0)
                            if _cached_ts > _pb_started_at:
                                _LOGGER.debug(
                                    "FCM Path B: skipping %s — a "
                                    "newer frame was already "
                                    "cached since this alert "
                                    "started",
                                    cam_name,
                                )
                            # Byte-identity dedup (B04-BUG-1 fix):
                            # len equality is NOT image equality.
                            elif _existing is None or _existing != data:
                                _cam_b.cached_image = data
                                _cam_b.last_image_fetch = time.monotonic()
                                await save_snapshot(
                                    coordinator.hass, _cam_id_for_b, data
                                )
                                _img_entities = getattr(
                                    coordinator, "image_entities", {}
                                )
                                _img_ent = _img_entities.get(_cam_id_for_b)
                                if _img_ent is not None:
                                    await _img_ent.async_notify_refreshed()
                                _LOGGER.debug(
                                    "FCM Path B: event image pushed to %s cache (%d B)",
                                    cam_name,
                                    len(data),
                                )
                            else:
                                _LOGGER.debug(
                                    "FCM Path B: skipping %s — bytes identical (%d B)",
                                    cam_name,
                                    len(data),
                                )
                except Exception as _pb_err:
                    _LOGGER.warning(
                        "FCM Path B: failed to update %s cache: %s",
                        cam_name,
                        _pb_err,
                    )
            except Exception as err:
                _LOGGER.warning("Alert step 2 failed: %s", err)

    # -- Step 3: Video clip — poll until ready, then download + send -------
    # Bosch uploads clips asynchronously. The event initially has
    # clip_status=Pending (or no clipUrl at all). We poll the events API
    # every 10s for up to 90s until videoClipUploadStatus=Done.
    # Use stable cam_id resolved at push-receipt time (B04-BUG-2 fix).
    _clip_cam_id: str | None = _resolved_cam_id

    if _clip_cam_id:
        # Neutralise path traversal: cam_name is the cloud-provided camera title
        # and must never escape alert_dir (e.g. a title like "../../config").
        # Mirrors the snapshot path guard above — the .mp4 write below
        # (_write_file) would otherwise honour a malicious title verbatim.
        clip_path = os.path.join(
            alert_dir,
            f"{_safe_path_segment(cam_name)}_{_safe_path_segment(ts_safe)}"
            f"_{_safe_path_segment(event_type)}_{alert_token}.mp4",
        )
        auth_headers = {
            "Authorization": f"Bearer {coordinator.token}",
            "Accept": "application/json",
        }
        found_clip_url = clip_url if (clip_url and clip_status == "Done") else ""
        # Bug fix (Area 5, item 9): the probe below is NOT a HEAD request —
        # it's a full GET, and aiohttp's ClientResponse.release() (called
        # implicitly when the `async with` block exits without the body
        # being read) reads and discards the ENTIRE body anyway to reuse the
        # keep-alive connection, so the clip was effectively downloaded and
        # thrown away here, then downloaded a SECOND time below. Bosch's API
        # has no documented HEAD support and no other call site in this
        # codebase uses one, so read+cache the body here instead (same
        # `_prefetched_snapshot`-style pattern already used for the
        # screenshot) and reuse it below rather than re-fetching.
        _prefetched_clip: bytes | None = None

        # Try direct clip.mp4 download first (faster than polling)
        if not found_clip_url:
            # Bug fix (Area 5, item 2): use the event_id captured once at
            # the start of this coroutine — do not re-derive from
            # coordinator.last_event_ids here, it may have advanced since.
            if event_id:
                try:
                    async with asyncio.timeout(10):
                        async with session.get(
                            f"{CLOUD_API}/v11/events/{event_id}/clip.mp4",
                            headers={
                                "Authorization": f"Bearer {coordinator.token}",
                                "Accept": "*/*",
                            },
                        ) as r:
                            if r.status == 200 and "video" in r.headers.get(
                                "Content-Type", ""
                            ):
                                _probe_body = await r.read()
                                if _probe_body and len(_probe_body) <= _CLIP_MAX_BYTES:
                                    _prefetched_clip = _probe_body
                                found_clip_url = (
                                    f"{CLOUD_API}/v11/events/{event_id}/clip.mp4"
                                )
                                _LOGGER.debug(
                                    "Alert: direct clip.mp4 available for %s "
                                    "(%d bytes, cached)",
                                    cam_name,
                                    len(_probe_body),
                                )
                except Exception:  # noqa: S110 # best-effort direct-clip GET; failure falls through to poll path
                    pass

        if not found_clip_url and clip_status == "Unavailable":
            _LOGGER.debug(
                "Alert: clip status Unavailable from start — skipping poll for %s",
                cam_name,
            )
        elif not found_clip_url:
            # Poll for clip readiness (10s intervals, up to 90s)
            clip_unavailable = False
            for attempt in range(9):
                await asyncio.sleep(10)
                try:
                    async with asyncio.timeout(10):
                        async with session.get(
                            f"{CLOUD_API}/v11/events?videoInputId={_clip_cam_id}&limit=3",
                            headers=auth_headers,
                        ) as r:
                            if r.status != 200:
                                continue
                            fresh = await r.json()
                            for ev in fresh:
                                # Match by event_id (stable UUID) rather than
                                # timestamp[:19] — two events within the same
                                # second share the same prefix and the wrong
                                # clip could be attached (BUG-6 fix).
                                _ev_id = ev.get("id", "")
                                if event_id and _ev_id and _ev_id != event_id:
                                    continue
                                if not event_id and (
                                    (ev.get("timestamp") or "")[:19] != timestamp[:19]
                                ):
                                    # Fallback: no event_id known, use timestamp
                                    # (legacy path — event_id should always be
                                    # present for FCM-triggered alerts).
                                    continue
                                status = ev.get("videoClipUploadStatus", "")
                                url = ev.get("videoClipUrl", "")
                                if status == "Done" and url:
                                    found_clip_url = url
                                elif status == "Unavailable":
                                    clip_unavailable = True
                                    _LOGGER.debug(
                                        "Alert: clip Unavailable after %ds — stop polling for %s",
                                        (attempt + 1) * 10,
                                        cam_name,
                                    )
                                break
                    if found_clip_url:
                        _LOGGER.debug(
                            "Alert: clip ready after %ds for %s",
                            (attempt + 1) * 10,
                            cam_name,
                        )
                        break
                    if clip_unavailable:
                        break
                except Exception:  # noqa: S112 # resilient poll loop, transient network error on one attempt should not abort all retries
                    continue

        async def _process_downloaded_clip(clip_data: bytes | None) -> None:
            """Write the downloaded/prefetched clip bytes to disk, register
            cleanup, and send the video notify step.

            Shared by both the prefetched-probe path and the normal
            download path (Area 5, item 9 bug fix) so the write/cleanup/
            notify logic exists exactly once instead of being duplicated.
            """
            nonlocal _step3_sent
            if not (clip_data and len(clip_data) > 1000):
                return
            try:
                await coordinator.hass.async_add_executor_job(
                    _write_file, clip_path, clip_data
                )
            except Exception:
                # Bug fix (Area 5, item 5): a write failure partway through
                # (e.g. ENOSPC) can still leave a partial file on disk —
                # check for and register it before re-raising, so it
                # doesn't leak forever even with alert_save_snapshots off.
                # The success path below registers unconditionally.
                if not save_snapshots and await coordinator.hass.async_add_executor_job(
                    os.path.exists, clip_path
                ):
                    files_to_cleanup.append(clip_path)
                raise
            if not save_snapshots:
                files_to_cleanup.append(clip_path)
            size_kb = len(clip_data) // 1024
            vcaption = f"\U0001f3ac {cam_name} Video ({ts_short}, {size_kb} KB)"
            _step3_sent = await _notify_type(
                "video",
                vcaption,
                clip_path,
            )
            if _step3_sent:
                _LOGGER.info(
                    "Alert step 3 (video) sent: %s (%d KB)",
                    clip_path,
                    size_kb,
                )
            else:
                _LOGGER.info(
                    "Alert step 3 (video) downloaded but NOT"
                    " delivered — either alert_notify_video"
                    " is unset (no fallback to"
                    " alert_notify_service for this step),"
                    " or the configured service call(s)"
                    " failed (see any 'Alert send failed'"
                    " warning above): %s (%d KB)",
                    clip_path,
                    size_kb,
                )

        if found_clip_url and _is_safe_bosch_url(found_clip_url):
            try:
                if _prefetched_clip is not None:
                    # Bug fix (Area 5, item 9): reuse the body already read
                    # by the direct-clip probe above instead of downloading
                    # the identical URL a second time.
                    data = _prefetched_clip
                    await _process_downloaded_clip(data)
                else:
                    dl_headers = {
                        "Authorization": f"Bearer {coordinator.token}",
                        "Accept": "*/*",
                    }
                    async with asyncio.timeout(60):
                        async with session.get(
                            found_clip_url, headers=dl_headers
                        ) as resp:
                            if resp.status == 200:
                                # Bug fix (Area 5, item 3): no size cap
                                # previously — a large/malformed response
                                # could allocate unboundedly via a plain
                                # `await resp.read()`. Fast-reject on a
                                # reported Content-Length first (cheap, and
                                # avoids even attempting the read for an
                                # honestly-declared oversized response), then
                                # enforce the same cap on the actual byte
                                # count after reading (covers a missing or
                                # lying Content-Length header too).
                                _content_length = resp.headers.get("Content-Length")
                                _clip_oversized = False
                                if _content_length is not None:
                                    try:
                                        _clip_oversized = (
                                            int(_content_length) > _CLIP_MAX_BYTES
                                        )
                                    except ValueError:
                                        pass
                                data = None
                                if not _clip_oversized:
                                    data = await resp.read()
                                    if data and len(data) > _CLIP_MAX_BYTES:
                                        _clip_oversized = True
                                        data = None
                                if _clip_oversized:
                                    _LOGGER.warning(
                                        "Alert step 3 (video): clip for %s "
                                        "exceeded the %d MB size cap — "
                                        "aborting download",
                                        cam_name,
                                        _CLIP_MAX_BYTES // (1024 * 1024),
                                    )
                                await _process_downloaded_clip(data)
            except Exception as err:
                _LOGGER.warning("Alert step 3 (video) failed: %s", err)
        else:
            _LOGGER.debug(
                "Alert: no usable video clip URL for %s (either the clip "
                "never became ready within the 90s poll window, its URL "
                "failed the safe-Bosch-host check, or Bosch reported the "
                "clip as Unavailable)",
                cam_name,
            )

    # -- Mark event as read ------------------------------------------------
    # Bug fix (Area 5, item 2): use the event_id captured once at the start
    # of this coroutine, not a fresh re-read of coordinator.last_event_ids
    # (which could have advanced to a different, un-alerted event by now).
    # Bug fix (Area 5, item 1): only mark read if at least one delivery step
    # actually succeeded — otherwise a misconfigured/failed alert would
    # silently mark the event read on Bosch's side while the user was never
    # notified, and the poll-fallback path wouldn't re-surface it either.
    if (
        _clip_cam_id
        and event_id
        and coordinator.options.get("mark_events_read", False)
        and (_step1_sent or _step2_sent or _step3_sent)
    ):
        try:
            await async_mark_events_read(coordinator, [event_id])
        except Exception:  # noqa: S110 # best-effort cloud housekeeping; alert delivery already complete
            pass

    # -- SMB upload (immediate, alongside alert) ---------------------------
    if opts.get("enable_smb_upload") and opts.get("smb_server") and _clip_cam_id:
        try:
            # Build a minimal data dict for sync_smb_upload with just this event
            # Bug fix (Area 5, item 2): use the event_id captured once
            # at the start of this coroutine, never re-derived from
            # coordinator.last_event_ids mid-flow.
            ev_id = event_id or "unknown"
            ev_data = {
                "timestamp": timestamp,
                "eventType": event_type,
                "id": ev_id,
                "imageUrl": image_url,
                "videoClipUrl": found_clip_url if found_clip_url else "",
                "videoClipUploadStatus": "Done" if found_clip_url else "",
            }
            smb_data = {
                _clip_cam_id: {
                    "info": {"title": cam_name},
                    "events": [ev_data],
                }
            }
            # Pass pre-downloaded snapshot bytes so sync_smb_upload skips the
            # cloud re-download.  When the camera is streaming, re-downloading
            # via urllib in the executor would compete on the camera's single TLS
            # control channel → RTSP keepalive delay → stream freeze.  When
            # _prefetched_snapshot is None (step 2 skipped / image unavailable),
            # sync_smb_upload falls back to downloading via imageUrl as before.
            _smb_prefetch = _prefetched_snapshot
            _LOGGER.info(
                "Alert: SMB upload starting for %s (event=%s, img=%s, clip=%s, prefetch=%s)",
                cam_name,
                ev_id[:8] if ev_id else "?",
                bool(image_url),
                bool(found_clip_url),
                bool(_smb_prefetch),
            )
            # NOTE: sync_smb_upload runs in an executor thread, and asyncio
            # can only abandon the *await* on a timeout — it cannot kill the
            # underlying OS thread, which would otherwise keep running the
            # upload indefinitely on a hung NAS (thread leak, and a delayed
            # write could still land after a retry has already re-sent the
            # same event). The real cutoff now happens inside sync_smb_upload
            # itself via socket.setdefaulttimeout(_SMB_TRANSFER_TIMEOUT), which
            # bounds every blocking smbclient/smbprotocol call in the transfer
            # loop. This outer wait_for(timeout=30.0) is only a safety margin
            # in case that inner bound is ever bypassed (e.g. a future
            # smbclient version issuing calls outside the socket module) — it
            # is deliberately longer than _SMB_TRANSFER_TIMEOUT so the inner
            # timeout fires first under normal conditions.
            await asyncio.wait_for(
                coordinator.hass.async_add_executor_job(
                    sync_smb_upload,
                    coordinator,
                    smb_data,
                    coordinator.token,
                    _smb_prefetch,
                ),
                timeout=30.0,
            )
            _LOGGER.info("Alert: SMB upload completed for %s", cam_name)
        except TimeoutError:
            _LOGGER.warning("Alert: SMB upload timed out after 30s for %s", cam_name)
        except Exception as err:
            _LOGGER.warning("Alert: SMB upload failed for %s: %s", cam_name, err)

    # -- Local save (FCM-triggered, alongside SMB) -------------------------
    if opts.get("enable_local_save") and opts.get("download_path") and _clip_cam_id:
        try:
            # Bug fix (Area 5, item 2): use the event_id captured once
            # at the start of this coroutine, never re-derived from
            # coordinator.last_event_ids mid-flow.
            ev_id = event_id or "unknown"
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
        except TimeoutError:
            _LOGGER.warning("Alert: local save timed out after 30s for %s", cam_name)
        except Exception as err:
            _LOGGER.warning("Alert: local save failed for %s: %s", cam_name, err)

    # -- Cleanup local files -----------------------------------------------
    # Gate solely on files_to_cleanup — a file only lands there when
    # alert_save_snapshots is OFF (see the two `if not save_snapshots:`
    # append sites above), so that alone already decides deletion (#53 fix).
    if files_to_cleanup:
        # Bug fix (Area 5, item 7): 5s was tuned for Signal's fetch pattern,
        # but mobile_app/other services may fetch the URL lazily (user opens
        # the notification later), 404ing after the old 5s window. 30s is a
        # judgment-call middle ground: long enough to cover a user opening
        # the notification shortly after it lands (the common "saw it,
        # tapped it" case), while still keeping the unauthenticated exposure
        # window (item 8) short and not meaningfully regressing Signal's
        # original fast-fetch behavior (Signal fetches near-immediately on
        # push receipt, well under either value).
        await asyncio.sleep(30)
        for fpath in files_to_cleanup:
            try:
                await coordinator.hass.async_add_executor_job(os.remove, fpath)
            except OSError:
                pass


# ── Mark events as read ──────────────────────────────────────────────────────


async def async_mark_events_read(coordinator: Any, event_ids: list[str]) -> bool:
    """Mark events as read/seen on the Bosch cloud via PUT /v11/events.

    The /v11/events/bulk endpoint only supports `{ids, action: "DELETE"}` —
    there is no bulk mark-as-read. Best-effort — never raises.
    """
    if not event_ids:
        return True

    token = coordinator.token
    if not token:
        return False

    session = await async_get_bosch_cloud_session(coordinator.hass)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Bug fix (Area 5, item 6): `success = any(...)` semantics are kept
    # (best-effort, never raises) but a partial failure — and specifically a
    # 401 — must no longer be entirely silent. A 401 here means the token
    # was rejected for this write; the coordinator's other 401 handling
    # (e.g. async_put_camera) refreshes proactively on the next call, but at
    # minimum this must be visible in logs rather than swallowed identically
    # to a transient network blip.
    # NOTE: intentionally still serial (one HTTP call per event id) — not
    # parallelized in this pass; a real (if minor) improvement would be
    # asyncio.gather over event_ids, flagged here as a possible follow-up.
    success = False
    failed = 0
    saw_401 = False
    for eid in event_ids:
        try:
            async with asyncio.timeout(5):
                async with session.put(
                    f"{CLOUD_API}/v11/events",
                    headers=headers,
                    json={"id": eid, "isRead": True},
                ) as resp:
                    if resp.status in (200, 201, 204):
                        success = True
                    else:
                        failed += 1
                        if resp.status == 401:
                            saw_401 = True
        except Exception as err:
            failed += 1
            _LOGGER.debug("Mark-events-read request failed for %s: %s", eid[:8], err)

    if saw_401:
        _LOGGER.warning(
            "Mark-events-read: Bosch cloud rejected the token (HTTP 401) for "
            "%d/%d event(s) — will retry with a fresh token on the next call",
            failed,
            len(event_ids),
        )
    elif failed:
        _LOGGER.debug(
            "Mark-events-read: %d/%d event(s) failed (partial failure — not "
            "retried by this call)",
            failed,
            len(event_ids),
        )

    if success:
        _LOGGER.debug("Marked %d events as read", len(event_ids))
    return success


class FCMCoordinatorMixin:
    """Thin coordinator-facing methods delegating to this module's functions.

    Mixed into BoschCameraCoordinator (see __init__.py's class declaration)
    so `coordinator.async_start_fcm_push()` etc. keep working as methods —
    every one of them just forwards `self` to the corresponding free
    function above, which is where the actual FCM logic lives.
    """

    hass: HomeAssistant

    async def _fetch_firebase_config(self) -> dict[str, str]:
        """Fetch Firebase config (delegated to fetch_firebase_config)."""
        return await fetch_firebase_config(self.hass)

    async def async_start_fcm_push(self) -> None:
        """Start the FCM supervisor (delegated to async_ensure_fcm_supervisor)."""
        return await async_ensure_fcm_supervisor(self)

    async def _register_fcm_with_bosch(self) -> bool:
        """Register FCM token with Bosch CBS (delegated to register_fcm_with_bosch)."""
        return await register_fcm_with_bosch(self)

    async def async_stop_fcm_push(self) -> None:
        """Stop the FCM supervisor and push listener (delegated to async_stop_fcm_supervisor)."""
        return await async_stop_fcm_supervisor(self)

    async def _async_handle_fcm_push(self) -> None:
        """Handle an FCM push (delegated to async_handle_fcm_push)."""
        return await async_handle_fcm_push(self)

    def _get_alert_services(self, type_key: str) -> list[str]:
        """Return notify services for a given alert type (delegated to get_alert_services)."""
        return get_alert_services(self, type_key)

    @staticmethod
    def _build_notify_data(
        svc: str,
        message: str,
        file_path: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Build notify service call data (delegated to build_notify_data)."""
        return build_notify_data(svc, message, file_path, title)

    async def async_send_alert(
        self,
        cam_name: str,
        event_type: str,
        timestamp: str,
        image_url: str,
        clip_url: str = "",
        clip_status: str = "",
        event_id: str = "",
        cam_id: str = "",
    ) -> None:
        """Send a 3-step alert (delegated to async_send_alert).

        Bug fix (minor, Area 3): `cam_id` was previously omitted from this
        delegator, so any future caller through the mixin silently lost the
        Path B cache-push behavior (which requires the stable cam_id, not
        just the mutable display title).
        """
        return await async_send_alert(
            self,
            cam_name,
            event_type,
            timestamp,
            image_url,
            clip_url,
            clip_status,
            event_id=event_id,
            cam_id=cam_id,
        )

    async def async_mark_events_read(self, event_ids: list[str]) -> bool:
        """Mark events as read on the Bosch cloud (delegated to async_mark_events_read)."""
        return await async_mark_events_read(self, event_ids)

    @staticmethod
    def _write_file(path: str, data: bytes) -> None:
        """Write binary data to file (delegated to the module-level _write_file)."""
        _write_file(path, data)
