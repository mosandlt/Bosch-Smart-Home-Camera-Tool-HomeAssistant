"""Fresh-install regression tests.

Verifies that a brand-new installation (entry.options = {}, no prior config)
works end-to-end: correct defaults, local event save, Media Browser visibility.

Regression for bug reported by Andreas74 (simon42 forum, 2026-05-07):
- v11.0.10: _FILE_RE required camera prefix → old files invisible in Media Browser
- v11.0.11: async_send_alert returned early when no notify service configured →
  bosch_events/ stayed empty after fresh install
"""
from __future__ import annotations

import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
MODULE = "custom_components.bosch_shc_camera.fcm"
SMB_MODULE = "custom_components.bosch_shc_camera.smb"


# ── 1. DEFAULT_OPTIONS — all required keys present and sensible ───────────────

class TestDefaultOptions:
    """get_options with empty entry.options must return all required defaults."""

    def test_get_options_empty_entry_returns_defaults(self):
        from custom_components.bosch_shc_camera import get_options
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS

        entry = SimpleNamespace(options={})
        opts = get_options(entry)

        # All DEFAULT_OPTIONS keys must be present
        for key, default_val in DEFAULT_OPTIONS.items():
            assert key in opts, f"Missing key in get_options result: {key}"
            assert opts[key] == default_val, (
                f"Key {key!r}: expected {default_val!r}, got {opts[key]!r}"
            )

    def test_get_options_entry_values_override_defaults(self):
        from custom_components.bosch_shc_camera import get_options

        entry = SimpleNamespace(options={"alert_notify_service": "notify.signal"})
        opts = get_options(entry)

        assert opts["alert_notify_service"] == "notify.signal"
        # Other defaults still present
        assert opts["download_path"] == "/config/bosch_events"
        assert opts["enable_fcm_push"] is False

    def test_default_download_path_is_set(self):
        """Fresh install: download_path must default to /config/bosch_events."""
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS
        assert DEFAULT_OPTIONS.get("download_path") == "/config/bosch_events"

    def test_default_fcm_push_disabled(self):
        """Fresh install: FCM push is disabled by default — polling drives events."""
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS
        assert DEFAULT_OPTIONS.get("enable_fcm_push") is False

    def test_default_notify_service_empty(self):
        """Fresh install: no notification service configured by default."""
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS
        assert DEFAULT_OPTIONS.get("alert_notify_service") == ""


# ── 2. async_send_alert — fresh-install defaults produce local save ───────────

def _make_fresh_coord(**overrides):
    """Coordinator with default options (empty entry.options merged with defaults)."""
    from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS

    hass = MagicMock()
    hass.config.config_dir = "/tmp/test-ha"
    hass.async_add_executor_job = AsyncMock(return_value=None)
    hass.services.async_call = AsyncMock(return_value=None)

    coord = SimpleNamespace(
        token="tok-fresh",
        hass=hass,
        options=dict(DEFAULT_OPTIONS),   # fresh install = all defaults
        data={CAM_ID: {"info": {"title": "Aussenkamera"}, "events": []}},
        _last_event_ids={CAM_ID: "fresh-event-001"},
        _download_started_at=time.time() - 10,  # started 10s ago
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _resp_cm(status, body=b"", content_type="image/jpeg"):
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    resp.json = AsyncMock(return_value=[])
    resp.headers = {"Content-Type": content_type}
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


class TestFreshInstallAlertSave:
    """With default options (no notify service, default download_path), events must be saved."""

    @pytest.mark.asyncio
    async def test_local_save_fires_with_default_options(self):
        """Regression v11.0.11: sync_local_save must be called on default (fresh install) options."""
        coord = _make_fresh_coord()
        coord.hass.async_add_executor_job = AsyncMock(return_value=None)

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save") as mock_save:
                        from custom_components.bosch_shc_camera.fcm import async_send_alert
                        await async_send_alert(
                            coord, "Aussenkamera", "MOVEMENT",
                            "2026-05-07T12:00:00.000Z",
                            "", "", "",
                        )

        executor_calls = coord.hass.async_add_executor_job.call_args_list
        assert any(c.args[0] is mock_save for c in executor_calls), (
            "sync_local_save must be queued with default options (fresh install). "
            f"executor calls: {[getattr(c.args[0], '__name__', repr(c.args[0])) for c in executor_calls]}"
        )

    @pytest.mark.asyncio
    async def test_smb_not_called_on_default_options(self):
        """SMB upload must NOT fire on fresh install (enable_smb_upload=False by default)."""
        coord = _make_fresh_coord()
        coord.hass.async_add_executor_job = AsyncMock(return_value=None)

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload") as mock_smb:
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        from custom_components.bosch_shc_camera.fcm import async_send_alert
                        await async_send_alert(
                            coord, "Aussenkamera", "MOVEMENT",
                            "2026-05-07T12:00:00.000Z",
                            "", "", "",
                        )

        executor_calls = coord.hass.async_add_executor_job.call_args_list
        assert not any(c.args[0] is mock_smb for c in executor_calls), \
            "sync_smb_upload must NOT fire when enable_smb_upload=False (default)"


# ── 3. _FILE_RE — both old (no-prefix) and new (with-prefix) filenames match ──

class TestFilenameRegex:
    """_FILE_RE must handle both v10.x (no camera prefix) and v11+ (with prefix) filenames."""

    def _regex(self):
        from custom_components.bosch_shc_camera.media_source import _FILE_RE
        return _FILE_RE

    def test_old_format_no_prefix_matches(self):
        """v10.x files: 2026-05-06_21-57-07_MOVEMENT_118180F0.jpg"""
        m = self._regex().match("2026-05-06_21-57-07_MOVEMENT_118180F0.jpg")
        assert m is not None, "Old format (no camera prefix) must match _FILE_RE"
        assert m.group("camera") is None
        assert m.group("date") == "2026-05-06"
        assert m.group("etype") == "MOVEMENT"

    def test_new_format_with_prefix_matches(self):
        """v11+ files: Aussenkamera_2026-05-07_12-00-00_MOVEMENT_EF791764.jpg"""
        m = self._regex().match("Aussenkamera_2026-05-07_12-00-00_MOVEMENT_EF791764.jpg")
        assert m is not None, "New format (with camera prefix) must match _FILE_RE"
        assert m.group("camera") == "Aussenkamera"
        assert m.group("date") == "2026-05-07"

    def test_camera_name_with_spaces_converted_to_underscore(self):
        """Camera name with space is stored as underscore — Aussenkamera_Einfahrt."""
        m = self._regex().match("Aussenkamera_Einfahrt_2026-05-07_12-00-00_MOVEMENT_ABCD1234.mp4")
        assert m is not None
        assert m.group("camera") == "Aussenkamera_Einfahrt"
        assert m.group("ext") == "mp4"

    def test_non_matching_file_returns_none(self):
        for name in ["thumbs.db", ".DS_Store", "._hidden", "2026-bad.jpg", "random.txt"]:
            assert self._regex().match(name) is None, f"{name!r} must not match _FILE_RE"


# ── 4. sync_local_save — file naming uses camera prefix ──────────────────────

class TestLocalSaveFilenaming:
    """Files saved by sync_local_save must include camera prefix in filename."""

    def test_saved_filename_includes_camera_prefix(self, tmp_path):
        from custom_components.bosch_shc_camera.smb import _safe_name, sync_local_save

        coord = _make_fresh_coord()
        coord.options = dict(coord.options)
        coord.options["download_path"] = str(tmp_path)
        coord._download_started_at = 0.0  # disable "predates startup" guard

        cam_name = "Aussenkamera Einfahrt"
        cam_safe = _safe_name(cam_name)  # preserves spaces: "Aussenkamera Einfahrt"

        ev = {
            "timestamp": "2026-05-07T12:00:00.000Z",
            "eventType": "MOVEMENT",
            "id": "EF791764",            # valid hex ID
            "imageUrl": "https://api.bosch.com/image.jpg",
            "videoClipUrl": "",
            "videoClipUploadStatus": "",
        }

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "image/jpeg"}
        mock_resp.content = b"JFIF" + b"\x00" * 200
        mock_session.get.return_value = mock_resp

        with patch("requests.Session", return_value=mock_session):
            with patch("custom_components.bosch_shc_camera.smb._is_safe_bosch_url", return_value=True):
                sync_local_save(coord, ev, "tok", cam_name)

        cam_dir = tmp_path / cam_safe
        assert cam_dir.is_dir(), (
            f"Camera subfolder {cam_safe!r} must be created under download_path"
        )
        files = list(cam_dir.iterdir())
        assert files, "At least one file must be saved"
        for f in files:
            assert f.name.startswith(cam_safe + "_"), (
                f"Saved filename must start with camera prefix {cam_safe!r}; got: {f.name}"
            )
