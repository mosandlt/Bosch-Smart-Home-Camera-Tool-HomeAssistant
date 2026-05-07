# Tests

Pytest suite for the Bosch Smart Home Camera HA integration.

**Current state (v11.0.10):** 2211 tests across 71 files, 78% total line-coverage.

Covers the HA Quality-Scale rules `config-flow-test-coverage` (Bronze) and `test-coverage` (Silver).

## Setup

```bash
python3 -m venv .venv-tests
source .venv-tests/bin/activate
pip install pytest pytest-asyncio pytest-homeassistant-custom-component pytest-cov pytest-socket
```

## Run

```bash
# from repo root
pytest tests/ -v

# with coverage
pytest tests/ --cov=custom_components/bosch_shc_camera --cov-report=term-missing -q
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
| `test_init_*.py` | coordinator methods, token refresh, go2rtc registration, RCP cache |
| `test_fcm*.py` | FCM push handling, dedup, alerts, send_alert step1/step2 |
| `test_rcp*.py` | RCP session, XML parsing, all type codes |
| `conftest.py` | Shared fixtures: `mock_config_entry`, `mock_oauth_token`, `mock_cloud_api_video_inputs` |

## Adding tests

1. Use the `hass` fixture from `pytest_homeassistant_custom_component` — it provides a real HA core instance.
2. Use `MockConfigEntry` to attach a config entry to `hass`.
3. For cloud-API behaviour, patch `custom_components.bosch_shc_camera.shc.async_get_clientsession` (see `mock_cloud_api_video_inputs` in conftest).
4. Mark async tests with `async def` (auto-detected by pytest-asyncio).
5. Camera tests that bypass `__init__` via `BoschCamera.__new__` must set **both** `_attr_name` and `_display_name` on the fixture object.
6. `smbclient` is not installed in the test venv — always mock via `patch.dict(sys.modules, {"smbclient": fake_smb})`.
