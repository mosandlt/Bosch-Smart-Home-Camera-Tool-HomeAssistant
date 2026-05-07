# Tests

Pytest suite for the Bosch Smart Home Camera HA integration.

**Current state (v11.0.10):** 2978 tests across 111 files, **95% total line-coverage** — HA Gold-tier threshold met.

Covers the HA Quality-Scale rules `config-flow-test-coverage` (Bronze), `test-coverage` (Gold).

## CI

Tests run automatically on every push to `main` and on every pull request via `.github/workflows/tests.yml` (Python 3.14, ubuntu-latest). The badge in the README reflects the current `main` status.

## Setup

```bash
python3.14 -m venv .venv-tests
source .venv-tests/bin/activate
# system dep needed by PyTurboJPEG (HA camera imports it at module level)
# macOS: brew install libjpeg-turbo
# Ubuntu: sudo apt-get install -y libturbojpeg0-dev
pip install -r requirements_test.txt
```

## Run

```bash
# from repo root
pytest tests/ --timeout=30 -q

# with coverage
pytest tests/ --cov=custom_components/bosch_shc_camera --cov-report=term-missing -q

# single file
pytest tests/test_init_sprint_kc.py -v
```

## Layout

| File(s) | Covers |
|---|---|
| `test_config_flow.py` | unique-config-entry, reauth, reconfigure, OAuth-create-entry |
| `test_diagnostics.py` | TO_REDACT covers all secret fields; nested FCM redaction |
| `test_camera_*.py` | camera entity lifecycle, image impl, async refresh, stream source |
| `test_switch_*.py` | all switch classes — live stream, privacy, notifications, light, NVR |
| `test_sensor_*.py` | all sensor classes — WiFi, motion, firmware, stream status, etc. |
| `test_light_*.py` | Gen1/Gen2 light entities, color temp, RGB, turn on/off |
| `test_number_*.py` | number entities — audio threshold, lens elevation, mic level, etc. |
| `test_media_source*.py` | LocalBackend, SmbBackend, NvrBackend, _browse tree, _FILE_RE |
| `test_smb*.py` | sync_local_save, SMB upload/cleanup/disk-check, FTP upload/cleanup |
| `test_tls_proxy*.py` | TLS proxy daemon threads, circuit breaker, pipe relay |
| `test_init_sprint_*.py` | coordinator methods: _async_update_data, _try_live_connection_inner, _auto_renew_local_session, token refresh, go2rtc, RCP cache |
| `test_fcm*.py` | FCM push handling, dedup, alerts, send_alert step1/step2 |
| `test_rcp*.py` | RCP session, XML parsing, all type codes |
| `test_buttons.py` | button entity press → coordinator refresh |
| `test_theoretical_bugs.py` | regression tests for reported user bugs |
| `conftest.py` | Shared fixtures: `mock_config_entry`, `mock_oauth_token`, `mock_cloud_api_video_inputs` |

## Adding tests

### Which style to use

There are two test styles in this repo. Pick based on what you're testing:

**Full HA style** — use when testing config flow, entity setup/teardown, HA state machine, or anything that needs a real HA core:
```python
# needs hass fixture from pytest-homeassistant-custom-component
async def test_something(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={...})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    ...
```

**SimpleNamespace stub style** — use for unit-testing coordinator methods, switch logic, or any pure-Python path that doesn't need HA core. Faster and simpler. Used in all `test_init_sprint_*.py`, `test_init_round*.py`, `test_switches_*.py`:
```python
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

coord = SimpleNamespace(
    token="tok-A",
    _entry=SimpleNamespace(data={...}, options={}, entry_id="..."),
    _some_cache={},
    hass=SimpleNamespace(async_create_task=MagicMock(), ...),
)
await BoschCameraCoordinator.some_method(coord, ...)
assert coord._some_cache == {...}
```

### The `_make_coord` pattern

For coordinator unit tests, define a `_make_coord(**overrides)` helper that pre-seeds all fields the method under test may touch, then override only what the test needs:

```python
def _make_coord(**overrides):
    base = dict(
        token="tok-A",
        _entry=SimpleNamespace(data={"bearer_token": "tok-A"}, options={}, entry_id="..."),
        _feature_flags={"x": 1},   # skip FF fetch
        _protocol_checked=True,    # skip protocol check
        _last_status=float('-inf'),
        _last_events=float('-inf'),
        _last_slow=time.monotonic(),  # recent → skip slow by default
        # ... all other fields the method touches ...
        hass=SimpleNamespace(
            async_create_task=MagicMock(side_effect=_create_task),
            bus=SimpleNamespace(async_fire=MagicMock()),
            ...
        ),
    )
    base.update(overrides)
    return SimpleNamespace(**base)
```

Individual tests then only pass what they're specifically testing:
```python
coord = _make_coord(
    _stream_fell_back={CAM_A: True},
    _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
)
```

### Mocking aiohttp sessions

Use the `_url_session(url_map)` pattern (routing by URL substring, longest-match wins):

```python
def _url_session(url_map: dict):
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    _sorted = sorted(url_map.items(), key=lambda kv: len(kv[0]), reverse=True)

    def _get(url, **kwargs):
        for pattern, resp in _sorted:
            if pattern in url:
                return resp
        return _make_resp(200, json_val=[])

    session.get = _get
    session.put = AsyncMock()
    return session
```

Then patch `async_get_clientsession` in the test:
```python
with patch("custom_components.bosch_shc_camera.async_get_clientsession", return_value=session):
    await BoschCameraCoordinator._async_update_data(coord)
```

### Timing sentinels — critical rule

**Never use `0.0` as a "never done" sentinel for `time.monotonic()` comparisons.**

The production code uses patterns like:
```python
do_events = (now - self._last_events) >= event_interval  # e.g. 300s
```

On a freshly booted CI VM, `time.monotonic()` is only ~100–300 seconds. If `_last_events = 0.0`, then `200 - 0.0 = 200 < 300` → `do_events = False`, and tests that rely on events running will fail silently.

**Always use `float('-inf')` as the "never done" sentinel:**
```python
# ✓ correct — works on any machine regardless of uptime
_last_events=float('-inf'),   # time.monotonic() - (-inf) = +inf >= any interval

# ✗ wrong — fails on CI VMs with low uptime
_last_events=0.0,
```

This applies to all timing fields: `_last_events`, `_last_slow`, `_last_status`, `_last_go2rtc_reload`, `_last_schemes_refresh`, `_local_promote_at.get(cam_id, float('-inf'))`, etc.

The same rule applies in production `dict.get()` defaults — the production constructor already uses `-86400.0` (24 h ago) for the same reason; `float('-inf')` is equivalent and clearer.

If a test is explicitly checking that an early-return path does NOT update a timestamp, it should set the field to a specific value and assert it stays at that value:
```python
coord._last_schemes_refresh = 0.0   # explicit starting value
await BoschCameraCoordinator._ensure_go2rtc_schemes_fresh(coord)
assert coord._last_schemes_refresh == 0.0  # early-return path left it unchanged — OK
```

### TLS proxy / thread tests

Tests that start a real TLS proxy server must shut it down with `shutdown(SHUT_RDWR)` before `close()`, otherwise the blocking `accept()` on Linux never unblocks and the `verify_cleanup` fixture reports lingering threads:

```python
import socket
srv.shutdown(socket.SHUT_RDWR)
srv.close()
```

PHACC's `verify_cleanup` fixture runs after every test and fails the test if threads are still alive. Make sure any daemon threads started by the test are stopped before the test returns.

### Full HA style — key rules

1. Use the `hass` fixture from `pytest_homeassistant_custom_component`.
2. Use `MockConfigEntry` to attach a config entry to `hass`.
3. For cloud-API behaviour, patch `custom_components.bosch_shc_camera.async_get_clientsession` (see `mock_cloud_api_video_inputs` in conftest).
4. Mark async tests with `async def` — auto-detected by pytest-asyncio in `asyncio_mode = auto` (set in `pyproject.toml` or `pytest.ini`).
5. Camera tests that bypass `__init__` via `BoschCamera.__new__` must set **both** `_attr_name` and `_display_name` on the fixture object.
6. `smbclient` is not installed in the test venv — always mock via `patch.dict(sys.modules, {"smbclient": fake_smb})`.

### Regression tests for bugs

Per project rule `TEST_EVERY_BUG`: every reproduced bug and every reported user issue gets a regression test **before** the fix is committed. Name the file `test_<bug-area>.py`, pin the exact input → expected output, and document the user/forum source in the docstring:

```python
class TestMyBugRegression:
    """Regression for issue #42 reported by user X on simon42.

    Root cause: ...
    Fixed in: v11.x.y
    """

    @pytest.mark.asyncio
    async def test_specific_behavior(self):
        """Exact description of what must hold."""
        ...
```
