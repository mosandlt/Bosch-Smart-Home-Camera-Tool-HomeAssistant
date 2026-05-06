"""Tests for media_source.py pure helpers (404 LOC, 0% covered).

Pure helpers — no SMB, no HA platform fixtures:
  - `_safe_join` — path-traversal guard for media file paths
  - `_is_macos_junk` — filters AppleDouble + .DS_Store
  - `_parse_filename` — Bosch event filename pattern
  - `_format_event_title` — display string for the media browser
  - `_FILE_RE` regex contract
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ── _safe_join (path-traversal guard) ───────────────────────────────────


class TestSafeJoin:
    def test_normal_relative_path(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _safe_join
        result = _safe_join(tmp_path, "Terrasse/2026-05-05/snap.jpg")
        assert result is not None
        assert result.is_relative_to(tmp_path.resolve())

    def test_traversal_attempt_rejected(self, tmp_path):
        """`../etc/passwd` must NOT escape the base directory."""
        from custom_components.bosch_shc_camera.media_source import _safe_join
        # raise_if_invalid_path catches `..` directly
        result = _safe_join(tmp_path, "../etc/passwd")
        assert result is None

    def test_absolute_path_rejected(self, tmp_path):
        """Absolute path → traversal attempt → reject."""
        from custom_components.bosch_shc_camera.media_source import _safe_join
        result = _safe_join(tmp_path, "/etc/passwd")
        assert result is None

    def test_double_traversal_rejected(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _safe_join
        result = _safe_join(tmp_path, "../../etc/passwd")
        assert result is None


# ── _is_macos_junk ──────────────────────────────────────────────────────


class TestIsMacosJunk:
    @pytest.mark.parametrize("name,expected", [
        ("._snap.jpg", True),  # AppleDouble resource fork
        ("._video.mp4", True),
        (".DS_Store", True),
        ("snap.jpg", False),
        ("video.mp4", False),
        ("Terrasse_2026-05-05_10-00-00_MOVEMENT_ABC.jpg", False),
        ("", False),
    ])
    def test_classification(self, name, expected):
        from custom_components.bosch_shc_camera.media_source import _is_macos_junk
        assert _is_macos_junk(name) is expected


# ── _parse_filename ──────────────────────────────────────────────────────


class TestParseFilename:
    def test_jpeg_movement_event(self):
        from custom_components.bosch_shc_camera.media_source import _parse_filename
        result = _parse_filename("Terrasse_2026-05-05_10-00-00_MOVEMENT_DEADBEEF.jpg")
        assert result is not None
        assert result["camera"] == "Terrasse"
        assert result["date"] == "2026-05-05"
        assert result["time"] == "10-00-00"
        assert result["etype"] == "MOVEMENT"
        assert result["ext"].lower() == "jpg"

    def test_mp4_audio_alarm_event(self):
        from custom_components.bosch_shc_camera.media_source import _parse_filename
        result = _parse_filename(
            "Innenbereich_2026-05-05_14-23-45_AUDIO_ALARM_DEADBEEF12.mp4"
        )
        assert result is not None
        assert result["camera"] == "Innenbereich"
        assert result["etype"] == "AUDIO_ALARM"
        assert result["ext"].lower() == "mp4"

    def test_camera_name_with_special_chars(self):
        """Camera name can contain hyphens, dots, spaces."""
        from custom_components.bosch_shc_camera.media_source import _parse_filename
        result = _parse_filename(
            "My-Cam.Front_2026-05-05_10-00-00_PERSON_BEEF.jpg"
        )
        assert result is not None
        assert result["camera"] == "My-Cam.Front"

    def test_invalid_filename_returns_none(self):
        from custom_components.bosch_shc_camera.media_source import _parse_filename
        for bad in (
            "random.jpg",
            "Terrasse_no_date.jpg",
            "Terrasse_2026-05-05_no-time.jpg",
            "snap.txt",   # wrong extension
            ".DS_Store",
            "",
        ):
            assert _parse_filename(bad) is None, f"Should reject: {bad!r}"

    def test_uppercase_extension_works(self):
        """re.IGNORECASE — .JPG is accepted."""
        from custom_components.bosch_shc_camera.media_source import _parse_filename
        result = _parse_filename("Cam_2026-05-05_10-00-00_MOVEMENT_ABC.JPG")
        assert result is not None


# ── _enabled_sources (Media Browser source decision tree) ──────────────


class TestEnabledSources:
    """Reproduces the user-reported issue 'Media Browser bleibt leer nach v11.0.0'.

    The function decides per config-entry which backends (Local + SMB)
    appear under the Media Browser entry. Bug history:
      - v10.7.0 introduced the Media Browser provider
      - v10.7.1 fixed the empty-after-enable bug: enabling auto-download alone
        is now sufficient, no manual path entry needed
      - v11.0.0 migrated from `hass.data[DOMAIN]` to `entry.runtime_data` —
        the iteration changed to `async_loaded_entries(DOMAIN)`
    """

    def _build_hass(self, entries: list):
        """Stub `hass.config_entries.async_loaded_entries(DOMAIN)`."""
        from types import SimpleNamespace
        return SimpleNamespace(
            config_entries=SimpleNamespace(
                async_loaded_entries=lambda domain: entries,
            ),
        )

    def _entry(
        self,
        *,
        entry_id: str = "01ENTRY",
        runtime_data=...,
        options: dict | None = None,
    ):
        """Build a config-entry stub with `runtime_data` and `entry_id`."""
        from types import SimpleNamespace
        if runtime_data is ...:
            # Default coord stub
            runtime_data = SimpleNamespace(options=options or {})
        return SimpleNamespace(entry_id=entry_id, runtime_data=runtime_data)

    def test_no_options_returns_empty(self):
        """All defaults — auto_download off, no SMB → no Media Browser entry."""
        from custom_components.bosch_shc_camera.media_source import _enabled_sources
        hass = self._build_hass([self._entry(options={})])
        assert _enabled_sources(hass) == []

    def test_runtime_data_none_skipped(self):
        """An entry without runtime_data (not yet loaded) must be skipped, not crash."""
        from custom_components.bosch_shc_camera.media_source import _enabled_sources
        hass = self._build_hass([self._entry(runtime_data=None)])
        result = _enabled_sources(hass)
        assert result == []

    def test_no_loaded_entries_returns_empty(self):
        """No Bosch entries loaded → empty list, not crash."""
        from custom_components.bosch_shc_camera.media_source import _enabled_sources
        hass = self._build_hass([])
        assert _enabled_sources(hass) == []

    def test_download_path_set_adds_local_backend(self, tmp_path):
        """download_path set → Local backend appears (no extra checkbox needed since v11.0.8)."""
        from custom_components.bosch_shc_camera.media_source import _enabled_sources
        hass = self._build_hass([self._entry(options={
            "download_path": str(tmp_path),
            "media_browser_source": "auto",
        })])
        sources = _enabled_sources(hass)
        assert len(sources) == 1
        src, _ = sources[0]
        assert src.kind == "L"
        assert src.label == "Lokal"

    def test_empty_download_path_hides_local_backend(self, tmp_path):
        """v11.0.8: empty download_path → no local backend (disables FCM local save)."""
        from custom_components.bosch_shc_camera.media_source import _enabled_sources
        hass = self._build_hass([self._entry(options={
            "download_path": "",
            "media_browser_source": "auto",
        })])
        assert _enabled_sources(hass) == []

    def test_download_path_creates_missing_directory(self, tmp_path):
        """download_path pointing to non-existent dir → dir is created on first browse.

        Regression: before v11.0.1 the Media Browser stayed empty until the
        first event arrived because the directory had to pre-exist. Since v11.0.1
        _enabled_sources creates it on first call so the entry appears immediately.
        """
        from custom_components.bosch_shc_camera.media_source import _enabled_sources
        new_dir = tmp_path / "bosch_events_fresh"
        assert not new_dir.exists()
        hass = self._build_hass([self._entry(options={
            "download_path": str(new_dir),
            "media_browser_source": "auto",
        })])
        sources = _enabled_sources(hass)
        assert new_dir.is_dir(), "download_path must be created on first browse"
        assert len(sources) == 1
        assert sources[0][0].kind == "L"

    def test_download_path_creation_failure_skipped(self):
        """If the directory can't be created (perms, read-only fs), gracefully skip."""
        from custom_components.bosch_shc_camera.media_source import _enabled_sources
        hass = self._build_hass([self._entry(options={
            "download_path": "/proc/cannot_create_here_12345",
            "media_browser_source": "auto",
        })])
        result = _enabled_sources(hass)
        assert result == []

    def test_media_browser_source_none_hides_entry(self, tmp_path):
        """`media_browser_source=none` short-circuits — even with valid local data."""
        from custom_components.bosch_shc_camera.media_source import _enabled_sources
        hass = self._build_hass([self._entry(options={
            "download_path": str(tmp_path),
            "media_browser_source": "none",
        })])
        assert _enabled_sources(hass) == []

    def test_media_browser_source_local_filters_smb(self, tmp_path):
        """`media_browser_source=local` hides SMB even if SMB upload is enabled."""
        from custom_components.bosch_shc_camera.media_source import _enabled_sources
        hass = self._build_hass([self._entry(options={
            "download_path": str(tmp_path),
            "enable_smb_upload": True,
            "smb_server": "192.168.1.1",
            "smb_username": "user",
            "smb_password": "pass",
            "smb_share": "FRITZ.NAS",
            "media_browser_source": "local",
        })])
        sources = _enabled_sources(hass)
        assert len(sources) == 1
        assert sources[0][0].kind == "L"

    def test_media_browser_source_smb_filters_local(self, tmp_path):
        """`media_browser_source=smb` hides Local even if download_path is set."""
        from custom_components.bosch_shc_camera.media_source import _enabled_sources
        hass = self._build_hass([self._entry(options={
            "download_path": str(tmp_path),
            "media_browser_source": "smb",
        })])
        assert _enabled_sources(hass) == []

    def test_default_filter_is_auto(self, tmp_path):
        """Missing `media_browser_source` option → default 'auto' shows everything."""
        from custom_components.bosch_shc_camera.media_source import _enabled_sources
        hass = self._build_hass([self._entry(options={
            "download_path": str(tmp_path),
            # NO media_browser_source key
        })])
        sources = _enabled_sources(hass)
        assert len(sources) == 1


# ── _format_event_title ─────────────────────────────────────────────────


class TestFormatEventTitle:
    def test_replaces_time_dashes_with_colons(self):
        """Time `10-00-00` → display as `10:00:00`."""
        from custom_components.bosch_shc_camera.media_source import _format_event_title
        result = _format_event_title({
            "time": "14-23-45",
            "etype": "MOVEMENT",
            "camera": "Terrasse",
            "date": "2026-05-05",
        })
        assert "14:23:45" in result
        assert "MOVEMENT" in result
        assert "Terrasse" in result

    def test_format_includes_em_dash(self):
        from custom_components.bosch_shc_camera.media_source import _format_event_title
        result = _format_event_title({
            "time": "10-00-00",
            "etype": "PERSON",
            "camera": "Cam",
            "date": "2026-05-05",
        })
        assert "—" in result  # em-dash separator
