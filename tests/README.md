# Tests

Pytest suite for the Bosch Smart Home Camera HA integration.

Covers Quality-Scale Bronze rule `config-flow-test-coverage` and a starter set for the
Silver rule `test-coverage`. Full 95% coverage of `__init__.py` (5000+ lines, cloud-API
mocking) is filed as a separate sprint.

## Setup

```bash
pip install pytest pytest-asyncio pytest-homeassistant-custom-component
```

## Run

```bash
# from repo root
pytest tests/ -v
```

## Layout

| File | Covers |
|---|---|
| `conftest.py` | Shared fixtures: `mock_config_entry`, `mock_oauth_token`, `mock_cloud_api_video_inputs` |
| `test_config_flow.py` | unique-config-entry, reauth, reconfigure, OAuth-create-entry |
| `test_diagnostics.py` | TO_REDACT covers all secret fields; nested FCM redaction |

## Adding tests

1. Use the `hass` fixture from `pytest_homeassistant_custom_component` — it provides a real HA core instance.
2. Use `MockConfigEntry` to attach a config entry to `hass`.
3. For cloud-API behaviour, patch `custom_components.bosch_shc_camera.shc.async_get_clientsession` (see `mock_cloud_api_video_inputs` in conftest).
4. Mark async tests with `async def` (auto-detected by pytest-asyncio).
