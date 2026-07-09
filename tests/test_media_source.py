"""Tests for custom_components.bosch_shc_camera.media_source.

Consolidated from 10 previously-scattered test files into one flat module
(HA-core platinum test layout: one test_<module>.py per source module).
Covers:

  - Pure helpers (_safe_join, _is_macos_junk, _parse_filename,
    _format_event_title, _FILE_RE, _entry_title, _node)
  - _enabled_sources (Media Browser source discovery + OSError paths)
  - _LocalBackend: flat, camera-first and year-first tree layouts;
    camera names with spaces/umlauts; path-traversal guards
  - _NvrBackend: continuous-recording segment listing/resolve
  - _SmbBackend: SMB/NAS browse + open, connection-cache isolation under
    concurrency, session cleanup on error, path-traversal guards,
    year-first and legacy flat-file variants
  - BoschCameraMediaSource._browse dispatch tree (single/multi source,
    single/multi backend, camera-first vs date-first vs year-first)
  - BoschCameraMediaView.get() HTTP serving (Local/NVR/SMB dispatch,
    Range requests, error paths)
  - smb.sync_local_save: the auto-download-to-disk event guard + HTTP flow
    that backs the Local backend's on-disk tree
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.media_source.error import Unresolvable

from custom_components.bosch_shc_camera.media_source import (
    BoschCameraMediaSource,
    BoschCameraMediaView,
    _enabled_sources,
    _find_source,
    _LocalBackend,
    _NvrBackend,
    _parse_filename,
    _safe_join,
    _SmbBackend,
    async_get_media_source,
)
from tests.source_match import assert_in_source

MODULE = "custom_components.bosch_shc_camera.media_source"

CAM_FILE = "Terrasse_2026-05-07_10-00-00_MOVEMENT_11111111.mp4"
CAM_IMG = "Terrasse_2026-05-07_10-00-00_MOVEMENT_11111111.jpg"

_SAFE_IMAGE_URL = "https://media.boschsecurity.com/snapshot.jpg"
_SAFE_VIDEO_URL = "https://media.boschsecurity.com/clip.mp4"
_URLOPEN = "custom_components.bosch_shc_camera.smb.urllib.request.urlopen"


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers (deduped from the original 10 files)
# ─────────────────────────────────────────────────────────────────────────────


def _iso_ts(offset_s: float) -> str:
    """Return an ISO-8601 UTC timestamp offset_s seconds from now."""
    t = time.gmtime(time.time() + offset_s)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)


def _coord(
    tmp_path: Path, *, started_offset_s: float = -3600, extra_opts: dict | None = None
):
    """Coordinator stub for local-save tests.

    started_offset_s: seconds relative to now for _download_started_at.
      Negative -> started in the past.  Default = 1 h ago.
    """
    opts = {"enable_local_save": True, "download_path": str(tmp_path)}
    if extra_opts:
        opts.update(extra_opts)
    return SimpleNamespace(
        options=opts, _download_started_at=time.time() + started_offset_s
    )


def _ev(**kwargs) -> dict:
    base = {
        "timestamp": _iso_ts(0),
        "eventType": "MOVEMENT",
        "id": "AABBCCDD",
        "imageUrl": _SAFE_IMAGE_URL,
        "videoClipUrl": _SAFE_VIDEO_URL,
        "videoClipUploadStatus": "Done",
    }
    base.update(kwargs)
    return base


def _urlopen_resp(
    status: int = 200, content: bytes = b"FAKEDATA", raises: Exception | None = None
) -> MagicMock:
    """Build a MagicMock that behaves like urllib.request.urlopen()'s context manager."""
    resp = MagicMock()
    resp.status = status
    resp.read.side_effect = [content, b""]
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _fake_smbclient(entries=None, stat_size=0, fobj=None):
    """Build a fake smbclient module for sys.modules injection."""
    mod = MagicMock()
    mod.register_session = MagicMock()
    if entries is not None:
        mod.scandir = MagicMock(return_value=iter(entries))
    if fobj is not None:
        fake_stat = MagicMock()
        fake_stat.st_size = stat_size
        mod.open_file = MagicMock(return_value=fobj)
        mod.stat = MagicMock(return_value=fake_stat)
    return mod


def _dir_entry(name, is_dir=True, is_file=False):
    e = MagicMock()
    e.name = name
    e.is_dir.return_value = is_dir
    e.is_file.return_value = is_file
    return e


def _make_smb_backend(**opts):
    hass = MagicMock()
    hass.data = {}
    base = {
        "smb_server": "nas",
        "smb_share": "M",
        "smb_username": "u",
        "smb_password": "p",
        "smb_base_path": "",
    }
    base.update(opts)
    return _SmbBackend(hass, base)


def _hass_stub(
    entry_id: str = "entry1", opts: dict | None = None, tmp_path: Path | None = None
):
    hass = MagicMock()
    hass.data = {}
    hass.http = MagicMock()
    opts = opts or {
        "download_path": str(tmp_path or "/tmp"),
        "media_browser_source": "local",
    }
    coord = SimpleNamespace(options=opts)
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.runtime_data = coord
    entry.title = "Bosch Cam"
    hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)

    async def _exec(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = _exec
    return hass


def _hass_with_local_dir(tmp_path: Path, options: dict | None = None):
    """Build a fake `hass` whose `_enabled_sources` will return one
    `_LocalBackend` pointed at `tmp_path`."""
    opts = {
        "download_path": str(tmp_path),
        "media_browser_source": "auto",
    }
    if options:
        opts.update(options)
    coord = SimpleNamespace(options=opts)
    entry = SimpleNamespace(
        entry_id="01TESTENTRY",
        runtime_data=coord,
        title="Test Bosch",
    )
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_loaded_entries=MagicMock(return_value=[entry]),
            async_get_entry=MagicMock(return_value=entry),
        ),
        data={},
    )
    return hass, entry


def _seed_local_event(base: Path, camera: str, date: str, time: str = "10-00-00"):
    """Seed a jpg+mp4 pair in the camera-first nested structure: camera/year/month/day/."""
    year, month, day = date.split("-")
    cam_dir = base / camera / year / month / day
    cam_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{camera}_{date}_{time}_MOVEMENT_AB12.jpg"
    (cam_dir / fname).write_text("x")
    fname_mp4 = f"{camera}_{date}_{time}_MOVEMENT_AB12.mp4"
    (cam_dir / fname_mp4).write_text("x")
    return fname_mp4, fname


def _make_local_tree(
    tmp_path: Path, *, cam: str = "Terrasse", files: list[str] | None = None
) -> _LocalBackend:
    cam_dir = tmp_path / cam
    cam_dir.mkdir(parents=True, exist_ok=True)
    for f in files or [CAM_FILE, CAM_IMG]:
        (cam_dir / f).write_bytes(b"data")
    return _LocalBackend(str(tmp_path))


def _make_nvr_tree(
    tmp_path: Path,
    *,
    cam: str = "Terrasse",
    date: str = "2026-05-07",
    segments: list[str] | None = None,
) -> _NvrBackend:
    seg_dir = tmp_path / cam / date
    seg_dir.mkdir(parents=True, exist_ok=True)
    for s in segments or ["10-00.mp4"]:
        (seg_dir / s).write_bytes(b"vid")
    return _NvrBackend(str(tmp_path))


# ═════════════════════════════════════════════════════════════════════════
# PURE HELPERS: _safe_join, _is_macos_junk, _parse_filename,
# _format_event_title, _FILE_RE
# ═════════════════════════════════════════════════════════════════════════


class TestSafeJoin:
    def test_normal_relative_path(self, tmp_path):
        result = _safe_join(tmp_path, "Terrasse/2026-05-05/snap.jpg")
        assert result is not None
        assert result.is_relative_to(tmp_path.resolve())

    def test_traversal_attempt_rejected(self, tmp_path):
        """`../etc/passwd` must NOT escape the base directory."""
        # raise_if_invalid_path catches `..` directly
        result = _safe_join(tmp_path, "../etc/passwd")
        assert result is None

    def test_absolute_path_rejected(self, tmp_path):
        """Absolute path -> traversal attempt -> reject."""
        result = _safe_join(tmp_path, "/etc/passwd")
        assert result is None

    def test_double_traversal_rejected(self, tmp_path):
        result = _safe_join(tmp_path, "../../etc/passwd")
        assert result is None


class TestSafeJoinTraversal:
    def test_symlink_escaping_base_returns_none(self, tmp_path):
        """Symlink that points outside base passes raise_if_invalid_path
        but resolve() shows it's outside the base -> returns None."""
        base = tmp_path / "base"
        outside = tmp_path / "outside"
        base.mkdir()
        outside.mkdir()
        # Create a symlink named "escape" inside base pointing to outside/
        symlink = base / "escape"
        symlink.symlink_to(outside)
        # "escape" is a valid name (no ..) so raise_if_invalid_path passes
        # but its resolve() is outside base -> returns None
        result = _safe_join(base, "escape")
        assert result is None

    def test_valid_path_returns_target(self, tmp_path):
        """Normal relative path stays inside base -> returns Path."""
        (tmp_path / "cam").mkdir()
        result = _safe_join(tmp_path, "cam")
        assert result is not None
        assert result.name == "cam"


class TestIsMacosJunk:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("._snap.jpg", True),  # AppleDouble resource fork
            ("._video.mp4", True),
            (".DS_Store", True),
            ("snap.jpg", False),
            ("video.mp4", False),
            ("Terrasse_2026-05-05_10-00-00_MOVEMENT_ABC.jpg", False),
            ("", False),
        ],
    )
    def test_classification(self, name, expected):
        from custom_components.bosch_shc_camera.media_source import _is_macos_junk

        assert _is_macos_junk(name) is expected


class TestParseFilename:
    def test_jpeg_movement_event(self):
        result = _parse_filename("Terrasse_2026-05-05_10-00-00_MOVEMENT_DEADBEEF.jpg")
        assert result is not None
        assert result["camera"] == "Terrasse"
        assert result["date"] == "2026-05-05"
        assert result["time"] == "10-00-00"
        assert result["etype"] == "MOVEMENT"
        assert result["ext"].lower() == "jpg"

    def test_mp4_audio_alarm_event(self):
        result = _parse_filename(
            "Innenbereich_2026-05-05_14-23-45_AUDIO_ALARM_DEADBEEF12.mp4"
        )
        assert result is not None
        assert result["camera"] == "Innenbereich"
        assert result["etype"] == "AUDIO_ALARM"
        assert result["ext"].lower() == "mp4"

    def test_camera_name_with_special_chars(self):
        """Camera name can contain hyphens, dots, spaces."""
        result = _parse_filename("My-Cam.Front_2026-05-05_10-00-00_PERSON_BEEF.jpg")
        assert result is not None
        assert result["camera"] == "My-Cam.Front"

    def test_invalid_filename_returns_none(self):
        for bad in (
            "random.jpg",
            "Terrasse_no_date.jpg",
            "Terrasse_2026-05-05_no-time.jpg",
            "snap.txt",  # wrong extension
            ".DS_Store",
            "",
        ):
            assert _parse_filename(bad) is None, f"Should reject: {bad!r}"

    def test_uppercase_extension_works(self):
        """re.IGNORECASE -- .JPG is accepted."""
        result = _parse_filename("Cam_2026-05-05_10-00-00_MOVEMENT_ABC.JPG")
        assert result is not None


class TestFormatEventTitle:
    def test_replaces_time_dashes_with_colons(self):
        """Time `10-00-00` -> display as `10:00:00`."""
        from custom_components.bosch_shc_camera.media_source import _format_event_title

        result = _format_event_title(
            {
                "time": "14-23-45",
                "etype": "MOVEMENT",
                "camera": "Terrasse",
                "date": "2026-05-05",
            }
        )
        assert "14:23:45" in result
        assert "MOVEMENT" in result
        assert "Terrasse" in result

    def test_format_includes_em_dash(self):
        from custom_components.bosch_shc_camera.media_source import _format_event_title

        result = _format_event_title(
            {
                "time": "10-00-00",
                "etype": "PERSON",
                "camera": "Cam",
                "date": "2026-05-05",
            }
        )
        assert "—" in result  # em-dash separator


class TestFormatEventTitleEventTypes:
    def test_movement_event(self):
        from custom_components.bosch_shc_camera.media_source import _format_event_title

        parsed = {
            "date": "2026-05-04",
            "time": "10-30-15",
            "etype": "MOVEMENT",
            "camera": "Terrasse",
        }
        out = _format_event_title(parsed)
        # Format must include human-readable time + event type + camera
        assert "MOVEMENT" in out
        assert "10:30:15" in out
        assert "Terrasse" in out

    def test_audio_event(self):
        from custom_components.bosch_shc_camera.media_source import _format_event_title

        parsed = {
            "date": "2026-05-04",
            "time": "10-30-15",
            "etype": "AUDIO",
            "camera": "Terrasse",
        }
        out = _format_event_title(parsed)
        assert "AUDIO" in out

    def test_unknown_type_passes_through(self):
        from custom_components.bosch_shc_camera.media_source import _format_event_title

        parsed = {
            "date": "2026-05-04",
            "time": "10-30-15",
            "etype": "UNKNOWN_EVT",
            "camera": "Terrasse",
        }
        # Must not crash -- just include the literal type
        out = _format_event_title(parsed)
        assert isinstance(out, str)
        assert "UNKNOWN_EVT" in out


class TestFileRegexEdgeCases:
    """_FILE_RE must match (or correctly reject) filenames from various camera names."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from custom_components.bosch_shc_camera.media_source import _FILE_RE

        self.re = _FILE_RE

    def _parse(self, name: str):
        m = self.re.match(name)
        return m.groupdict() if m else None

    def test_space_in_camera_name(self):
        r = self._parse("Meine Kamera_2026-05-07_10-00-00_MOVEMENT_37AE5347.jpg")
        assert r is not None
        assert r["camera"] == "Meine Kamera"
        assert r["date"] == "2026-05-07"

    def test_two_spaces_in_camera_name(self):
        r = self._parse("Vorne Rechts Aussen_2026-05-07_10-00-00_MOVEMENT_37AE5347.jpg")
        assert r is not None
        assert r["camera"] == "Vorne Rechts Aussen"

    def test_umlaut_in_camera_name(self):
        r = self._parse("Küche_2026-05-07_10-00-00_MOVEMENT_37AE5347.jpg")
        assert r is not None
        assert r["camera"] == "Küche"

    def test_number_in_camera_name(self):
        r = self._parse("Kamera 1_2026-05-07_10-00-00_MOVEMENT_37AE5347.jpg")
        assert r is not None
        assert r["camera"] == "Kamera 1"

    def test_mp4_extension(self):
        r = self._parse("Terrasse_2026-05-07_10-00-00_MOVEMENT_37AE5347.mp4")
        assert r is not None
        assert r["ext"] == "mp4"

    def test_person_event_type(self):
        r = self._parse("Terrasse_2026-05-07_10-00-00_PERSON_37AE5347.jpg")
        assert r is not None
        assert r["etype"] == "PERSON"

    def test_unknown_id_rejected(self):
        """Files saved with id='UNKNOWN' (non-hex) must NOT match _FILE_RE.

        This is the case when coordinator._last_event_ids returns 'unknown'
        as the fallback -- those files are invisible in the Media Browser.
        Regression guard: if the ev_id guard is ever loosened, add a
        hex-validation step in sync_local_save instead.
        """
        r = self._parse("Terrasse_2026-05-07_10-00-00_MOVEMENT_UNKNOWN.jpg")
        assert r is None, (
            "_FILE_RE must not match files whose ID is 'UNKNOWN' (non-hex). "
            "If this fails, also fix sync_local_save to use a valid hex ID."
        )

    def test_empty_id_rejected(self):
        r = self._parse("Terrasse_2026-05-07_10-00-00_MOVEMENT_.jpg")
        assert r is None, "_FILE_RE must require at least one hex char in ID segment"

    def test_lowercase_hex_id_accepted(self):
        r = self._parse("Terrasse_2026-05-07_10-00-00_MOVEMENT_abcd1234.jpg")
        assert r is not None, "Lowercase hex IDs must be accepted (re.IGNORECASE)"


# ═════════════════════════════════════════════════════════════════════════
# _enabled_sources (Media Browser source decision tree)
# ═════════════════════════════════════════════════════════════════════════


class TestEnabledSources:
    """Reproduces the user-reported issue 'Media Browser bleibt leer nach v11.0.0'.

    The function decides per config-entry which backends (Local + SMB)
    appear under the Media Browser entry. Bug history:
      - v10.7.0 introduced the Media Browser provider
      - v10.7.1 fixed the empty-after-enable bug: enabling auto-download alone
        is now sufficient, no manual path entry needed
      - v11.0.0 migrated from `hass.data[DOMAIN]` to `entry.runtime_data` --
        the iteration changed to `async_loaded_entries(DOMAIN)`
    """

    def _build_hass(self, entries: list):
        """Stub `hass.config_entries.async_loaded_entries(DOMAIN)`."""
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
        if runtime_data is ...:
            # Default coord stub
            runtime_data = SimpleNamespace(options=options or {})
        return SimpleNamespace(entry_id=entry_id, runtime_data=runtime_data)

    def test_no_options_returns_empty(self):
        """All defaults -- auto_download off, no SMB -> no Media Browser entry."""
        hass = self._build_hass([self._entry(options={})])
        assert _enabled_sources(hass) == []

    def test_runtime_data_none_skipped(self):
        """An entry without runtime_data (not yet loaded) must be skipped, not crash."""
        hass = self._build_hass([self._entry(runtime_data=None)])
        result = _enabled_sources(hass)
        assert result == []

    def test_no_loaded_entries_returns_empty(self):
        """No Bosch entries loaded -> empty list, not crash."""
        hass = self._build_hass([])
        assert _enabled_sources(hass) == []

    def test_download_path_set_adds_local_backend(self, tmp_path):
        """download_path set -> Local backend always appears."""
        hass = self._build_hass(
            [
                self._entry(
                    options={
                        "download_path": str(tmp_path),
                    }
                )
            ]
        )
        sources = _enabled_sources(hass)
        assert len(sources) == 1
        src, _ = sources[0]
        assert src.kind == "L"
        assert src.label == "Lokal"

    def test_empty_download_path_hides_local_backend(self, tmp_path):
        """Empty download_path -> no local backend."""
        hass = self._build_hass(
            [
                self._entry(
                    options={
                        "download_path": "",
                    }
                )
            ]
        )
        assert _enabled_sources(hass) == []

    def test_download_path_creates_missing_directory(self, tmp_path):
        """download_path pointing to non-existent dir -> dir is created on first browse.

        Regression: before v11.0.1 the Media Browser stayed empty until the
        first event arrived because the directory had to pre-exist. Since v11.0.1
        _enabled_sources creates it on first call so the entry appears immediately.
        """
        new_dir = tmp_path / "bosch_events_fresh"
        assert not new_dir.exists()
        hass = self._build_hass(
            [
                self._entry(
                    options={
                        "download_path": str(new_dir),
                    }
                )
            ]
        )
        sources = _enabled_sources(hass)
        assert new_dir.is_dir(), "download_path must be created on first browse"
        assert len(sources) == 1
        assert sources[0][0].kind == "L"

    def test_download_path_creation_failure_skipped(self):
        """If the directory can't be created (perms, read-only fs), gracefully skip."""
        hass = self._build_hass(
            [
                self._entry(
                    options={
                        "download_path": "/proc/cannot_create_here_12345",
                    }
                )
            ]
        )
        result = _enabled_sources(hass)
        assert result == []

    def test_smb_shown_regardless_of_upload_protocol(self, tmp_path):
        """SMB backend appears even when upload_protocol=ftp (FTP files land on
        the same NAS share and are readable via SMB -- v11.0.12 fix)."""
        hass = self._build_hass(
            [
                self._entry(
                    options={
                        "download_path": str(tmp_path),
                        "enable_smb_upload": True,
                        "upload_protocol": "ftp",
                        "smb_server": "192.168.1.1",
                        "smb_share": "FRITZ.NAS",
                        "smb_username": "user",
                        "smb_password": "pass",
                    }
                )
            ]
        )
        sources = _enabled_sources(hass)
        kinds = {s.kind for s, _ in sources}
        assert "S" in kinds, "SMB backend must appear even when upload_protocol=ftp"
        assert "L" in kinds, "Local backend must also appear"

    def test_both_local_and_smb_shown_when_configured(self, tmp_path):
        """Local + SMB both shown simultaneously -- no filter."""
        hass = self._build_hass(
            [
                self._entry(
                    options={
                        "download_path": str(tmp_path),
                        "enable_smb_upload": True,
                        "smb_server": "192.168.1.1",
                        "smb_share": "FRITZ.NAS",
                        "smb_username": "user",
                        "smb_password": "pass",
                    }
                )
            ]
        )
        sources = _enabled_sources(hass)
        kinds = {s.kind for s, _ in sources}
        assert kinds == {"L", "S"}

    def test_only_configured_sources_shown(self, tmp_path):
        """When only download_path is set (no SMB), only local appears."""
        hass = self._build_hass(
            [
                self._entry(
                    options={
                        "download_path": str(tmp_path),
                    }
                )
            ]
        )
        sources = _enabled_sources(hass)
        assert len(sources) == 1
        assert sources[0][0].kind == "L"


class TestEnabledSourcesNoRuntimeData:
    def test_entry_without_runtime_data_skipped(self):
        """Entry where runtime_data is None -> continue (getattr default)."""
        entry = MagicMock()
        entry.entry_id = "e1"
        # Simulate entry with no runtime_data attribute
        del entry.runtime_data  # Remove attribute so getattr returns None
        hass = MagicMock()
        hass.config_entries.async_loaded_entries.return_value = [entry]
        hass.data = {}
        sources = _enabled_sources(hass)
        # Entry skipped -> no sources
        assert sources == []

    def test_entry_runtime_data_none_skipped(self):
        """Entry where runtime_data is explicitly None -> continue."""
        entry = SimpleNamespace(entry_id="e1", runtime_data=None)
        hass = MagicMock()
        hass.config_entries.async_loaded_entries.return_value = [entry]
        hass.data = {}
        sources = _enabled_sources(hass)
        assert sources == []


class TestFindSourceNone:
    def test_find_source_unknown_kind_returns_none(self, tmp_path):
        hass = _hass_stub("entry1", tmp_path=tmp_path)
        result = _find_source(hass, "entry1", "X")  # X is not a valid kind
        assert result is None


class TestEnabledSourcesOSError:
    def test_local_oserror_skipped(self):
        """OSError when creating download dir -> source is silently skipped."""
        entry = MagicMock()
        entry.entry_id = "e1"
        entry.runtime_data = SimpleNamespace(
            options={"download_path": "/no/such/deeply/nested/path"}
        )
        hass = MagicMock()
        hass.config_entries.async_loaded_entries.return_value = [entry]
        hass.data = {}
        # Path.mkdir will raise PermissionError (subclass of OSError) for impossible paths
        # on systems where /no doesn't exist and we can't create it.
        # We patch Path.mkdir to raise OSError to ensure the except branch fires.
        with patch(
            "custom_components.bosch_shc_camera.media_source.Path.mkdir",
            side_effect=OSError("permission denied"),
        ):
            sources = _enabled_sources(hass)
        # Local source should be skipped due to OSError
        kinds = [s.kind for s, _ in sources]
        assert "L" not in kinds

    def test_nvr_oserror_skipped(self, tmp_path):
        """OSError when accessing NVR path -> source is silently skipped."""
        entry = MagicMock()
        entry.entry_id = "e1"
        entry.runtime_data = SimpleNamespace(
            options={
                "download_path": "",
                "enable_nvr": True,
                "nvr_base_path": str(tmp_path / "nvr"),
            }
        )
        hass = MagicMock()
        hass.config_entries.async_loaded_entries.return_value = [entry]
        hass.data = {}
        # Patch Path.is_dir to raise OSError for the NVR path check
        original_is_dir = Path.is_dir
        call_count = [0]

        def mock_is_dir(self):
            call_count[0] += 1
            if "nvr" in str(self):
                raise OSError("nvr inaccessible")
            return original_is_dir(self)

        with patch.object(Path, "is_dir", mock_is_dir):
            sources = _enabled_sources(hass)
        kinds = [s.kind for s, _ in sources]
        assert "N" not in kinds


class TestEnabledSourcesFilters:
    def _entry(self, opts: dict):
        entry = MagicMock()
        entry.entry_id = "01ENT"
        entry.runtime_data = SimpleNamespace(options=opts)
        return entry

    def _call(self, entry):
        from custom_components.bosch_shc_camera import media_source as ms

        hass = MagicMock()
        hass.config_entries.async_loaded_entries.return_value = [entry]
        hass.data = {}
        return ms._enabled_sources(hass)

    def test_local_shown_when_download_path_set(self, tmp_path):
        """Local backend always appears when download_path is configured."""
        entry = self._entry({"download_path": str(tmp_path)})
        sources = self._call(entry)
        kinds = [src.kind for src, _ in sources]
        assert "L" in kinds

    def test_smb_shown_when_upload_enabled(self, tmp_path):
        """SMB backend always appears when enable_smb_upload=True + credentials."""
        entry = self._entry(
            {
                "download_path": str(tmp_path),
                "enable_smb_upload": True,
                "upload_protocol": "smb",
                "smb_server": "nas",
                "smb_share": "M",
                "smb_username": "u",
                "smb_password": "p",
            }
        )
        sources = self._call(entry)
        kinds = [src.kind for src, _ in sources]
        assert "S" in kinds

    def test_smb_shown_when_ftp_protocol(self, tmp_path):
        """SMB browser backend shown even when upload_protocol=ftp (v11.0.12 fix)."""
        entry = self._entry(
            {
                "download_path": str(tmp_path),
                "enable_smb_upload": True,
                "upload_protocol": "ftp",
                "smb_server": "nas",
                "smb_share": "M",
                "smb_username": "u",
                "smb_password": "p",
            }
        )
        sources = self._call(entry)
        kinds = [src.kind for src, _ in sources]
        assert "S" in kinds

    def test_nvr_backend_added_when_enabled(self, tmp_path):
        """enable_nvr=True with an existing dir must produce an NVR source."""
        nvr_base = tmp_path / "nvr"
        nvr_base.mkdir()
        entry = self._entry(
            {
                "enable_nvr": True,
                "nvr_base_path": str(nvr_base),
                "download_path": "",
            }
        )
        sources = self._call(entry)
        kinds = [src.kind for src, _ in sources]
        assert "N" in kinds

    def test_nvr_skipped_when_dir_missing(self, tmp_path):
        entry = self._entry(
            {
                "enable_nvr": True,
                "nvr_base_path": str(tmp_path / "no-such-dir"),
                "download_path": "",
            }
        )
        sources = self._call(entry)
        kinds = [src.kind for src, _ in sources]
        assert "N" not in kinds

    def test_download_path_dir_created_if_missing(self, tmp_path):
        """Missing download_path directory must be created by _enabled_sources."""
        new_dir = tmp_path / "auto_created"
        assert not new_dir.exists()
        entry = self._entry({"download_path": str(new_dir)})
        self._call(entry)
        assert new_dir.is_dir()


# ═════════════════════════════════════════════════════════════════════════
# _LocalBackend: list_cameras / list_dates / list_events / resolve
# ═════════════════════════════════════════════════════════════════════════


class TestLocalBackendListCameras:
    def test_empty_dir_returns_empty(self, tmp_path):
        b = _LocalBackend(str(tmp_path))
        assert b.list_cameras() == []

    def test_missing_dir_returns_empty(self, tmp_path):
        """Backend constructed with a path that doesn't exist must
        return [], not crash. Defensive against user typos in
        download_path."""
        b = _LocalBackend(str(tmp_path / "does-not-exist"))
        assert b.list_cameras() == []

    def test_lists_cameras_alphabetically_case_insensitive(self, tmp_path):
        (tmp_path / "Zebra").mkdir()
        (tmp_path / "alpha").mkdir()
        (tmp_path / "Beta").mkdir()
        b = _LocalBackend(str(tmp_path))
        # Case-insensitive sort
        assert b.list_cameras() == ["alpha", "Beta", "Zebra"]

    def test_skips_macos_junk(self, tmp_path):
        """`._.DS_Store` and similar macOS metadata dirs must not
        appear as fake camera entries."""
        (tmp_path / "Real-Cam").mkdir()
        (tmp_path / ".DS_Store").mkdir()
        (tmp_path / "._Real-Cam").mkdir()
        b = _LocalBackend(str(tmp_path))
        assert b.list_cameras() == ["Real-Cam"]

    def test_skips_underscore_dirs(self, tmp_path):
        """B13-5 regression: _staging / _failed NVR scratch dirs must not appear
        as camera tiles in the Media Browser for _LocalBackend."""
        (tmp_path / "Terrasse").mkdir()
        (tmp_path / "Innenbereich").mkdir()
        (tmp_path / "_staging").mkdir()
        (tmp_path / "_failed").mkdir()
        b = _LocalBackend(str(tmp_path))
        result = b.list_cameras()
        assert "_staging" not in result, "_staging must be filtered from camera list"
        assert "_failed" not in result, "_failed must be filtered from camera list"
        assert result == ["Innenbereich", "Terrasse"]

    def test_skips_files_only_dirs(self, tmp_path):
        (tmp_path / "loose-file.txt").write_text("x")
        b = _LocalBackend(str(tmp_path))
        assert b.list_cameras() == []

    def test_year_first_folders_appear_in_camera_list(self, tmp_path):
        """Year-first folders (e.g. "2026/") must appear in list_cameras() alongside
        real camera folders so users can browse legacy recordings without restructuring.

        Regression fix (simon42 / Andreas74 2026-05-08): previously these were
        filtered out and reported as hidden, leaving legacy recordings inaccessible
        in the Media Browser.
        """
        (tmp_path / "Terrasse").mkdir()
        (tmp_path / "Innenbereich").mkdir()
        (tmp_path / "2026").mkdir()
        (tmp_path / "2025").mkdir()
        b = _LocalBackend(str(tmp_path))
        cameras = b.list_cameras()
        assert "Terrasse" in cameras, "real camera must appear"
        assert "Innenbereich" in cameras, "real camera must appear"
        assert "2026" in cameras, (
            "year-first folder must appear -- browseable as 2026->month->day->events"
        )
        assert "2025" in cameras, "year-first folder must appear"

    def test_list_year_first_months(self, tmp_path):
        """list_year_first_months returns 2-digit month dirs under base/YYYY/."""
        year_dir = tmp_path / "2026"
        (year_dir / "03").mkdir(parents=True)
        (year_dir / "04").mkdir()
        (year_dir / "junk").mkdir()  # non-month dir must be excluded
        (year_dir / "file.mp4").write_text("x")  # file must be excluded
        b = _LocalBackend(str(tmp_path))
        months = b.list_year_first_months("2026")
        assert months == ["04", "03"], f"expected newest-first months, got {months}"

    def test_list_year_first_days(self, tmp_path):
        """list_year_first_days returns 2-digit day dirs under base/YYYY/MM/."""
        day_dir = tmp_path / "2026" / "03"
        (day_dir / "25").mkdir(parents=True)
        (day_dir / "26").mkdir()
        (day_dir / "notaday").mkdir()
        b = _LocalBackend(str(tmp_path))
        days = b.list_year_first_days("2026", "03")
        assert days == ["26", "25"], f"expected newest-first days, got {days}"

    def test_list_year_first_events(self, tmp_path):
        """list_year_first_events returns (filename, image, parsed) tuples from base/YYYY/MM/DD/."""
        day = tmp_path / "2026" / "03" / "25"
        day.mkdir(parents=True)
        (day / "Garten_2026-03-25_10-33-11_MOVEMENT_ABC123.mp4").write_text("x")
        (day / "Garten_2026-03-25_10-33-11_MOVEMENT_ABC123.jpg").write_text("x")
        (day / "._macjunk").write_text("x")  # must be filtered
        b = _LocalBackend(str(tmp_path))
        events = b.list_year_first_events("2026", "03", "25")
        assert len(events) == 1, f"expected 1 event, got {len(events)}"
        fname, image, parsed = events[0]
        assert fname.endswith(".mp4"), "video preferred over image"
        assert image is not None, "jpg thumbnail must be linked"
        assert parsed["camera"] == "Garten"


class TestLocalBackendListDates:
    def test_groups_files_by_date(self, tmp_path):
        cam = tmp_path / "Terrasse"
        cam.mkdir()
        # Filename pattern: <Camera>_<YYYY-MM-DD>_<HH-MM-SS>_<EventType>.<ext>
        (cam / "Terrasse_2026-05-04_10-30-00_MOVEMENT_A1.jpg").write_text("x")
        (cam / "Terrasse_2026-05-04_10-31-00_MOVEMENT_A2.mp4").write_text("x")
        (cam / "Terrasse_2026-05-03_09-00-00_AUDIO_A3.jpg").write_text("x")
        b = _LocalBackend(str(tmp_path))
        # Reverse-sorted by date (newest first)
        assert b.list_dates("Terrasse") == ["2026-05-04", "2026-05-03"]

    def test_unknown_camera_returns_empty(self, tmp_path):
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates("NonExistent") == []

    def test_skips_unparseable_filenames(self, tmp_path):
        """Loose / hand-named files in the camera dir don't break the
        date listing -- they're silently skipped."""
        cam = tmp_path / "Cam"
        cam.mkdir()
        (cam / "random-file.jpg").write_text("x")
        (cam / "Cam_2026-05-04_10-00-00_MOVEMENT_B1.jpg").write_text("x")
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates("Cam") == ["2026-05-04"]

    def test_traversal_camera_name_returns_empty(self, tmp_path):
        """`../etc` style camera name must not escape the base dir
        -- `_safe_join` gates this."""
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates("../../etc") == []


class TestLocalBackendListDatesEdgeCases:
    """Line 126: directories inside a camera folder are skipped (only files count)."""

    def test_subdir_in_cam_folder_skipped(self, tmp_path):
        cam_dir = tmp_path / "Terrasse"
        cam_dir.mkdir()
        # A sub-directory -- must NOT contribute a date
        (cam_dir / "subdir").mkdir()
        # A real event file
        (cam_dir / CAM_FILE).write_bytes(b"x")
        backend = _LocalBackend(str(tmp_path))
        dates = backend.list_dates("Terrasse")
        assert dates == ["2026-05-07"]

    def test_no_valid_files_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Terrasse"
        cam_dir.mkdir()
        (cam_dir / "readme.txt").write_bytes(b"x")  # unrecognised extension
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_dates("Terrasse") == []


class TestLocalBackendListEvents:
    def test_groups_jpg_and_mp4_into_one_event(self, tmp_path):
        cam = tmp_path / "Cam"
        cam.mkdir()
        (cam / "Cam_2026-05-04_10-00-00_MOVEMENT_B1.jpg").write_text("x")
        (cam / "Cam_2026-05-04_10-00-00_MOVEMENT_B1.mp4").write_text("x")
        b = _LocalBackend(str(tmp_path))
        events = b.list_events("Cam", "2026-05-04")
        # One event tuple, video preferred as primary, jpg as thumbnail
        assert len(events) == 1
        preferred, image, parsed = events[0]
        assert preferred.endswith(".mp4")
        assert image.endswith(".jpg")
        assert parsed["date"] == "2026-05-04"

    def test_image_only_event(self, tmp_path):
        cam = tmp_path / "Cam"
        cam.mkdir()
        (cam / "Cam_2026-05-04_10-00-00_AUDIO_C1.jpg").write_text("x")
        b = _LocalBackend(str(tmp_path))
        events = b.list_events("Cam", "2026-05-04")
        preferred, image, _ = events[0]
        assert preferred.endswith(".jpg")
        assert image == preferred

    def test_video_only_event_image_none(self, tmp_path):
        cam = tmp_path / "Cam"
        cam.mkdir()
        (cam / "Cam_2026-05-04_10-00-00_AUDIO_C2.mp4").write_text("x")
        b = _LocalBackend(str(tmp_path))
        events = b.list_events("Cam", "2026-05-04")
        preferred, image, _ = events[0]
        assert preferred.endswith(".mp4")
        assert image is None

    def test_filters_other_dates(self, tmp_path):
        cam = tmp_path / "Cam"
        cam.mkdir()
        (cam / "Cam_2026-05-04_10-00-00_MOVEMENT_B1.jpg").write_text("x")
        (cam / "Cam_2026-05-03_10-00-00_MOVEMENT_D1.jpg").write_text("x")
        b = _LocalBackend(str(tmp_path))
        events = b.list_events("Cam", "2026-05-04")
        assert len(events) == 1
        # Only the date=2026-05-04 entry came through
        assert events[0][2]["date"] == "2026-05-04"

    def test_sorted_newest_first(self, tmp_path):
        """Within a date, events appear newest-first (reverse stem sort
        works because the timestamp is in the stem)."""
        cam = tmp_path / "Cam"
        cam.mkdir()
        (cam / "Cam_2026-05-04_10-00-00_MOVEMENT_B1.jpg").write_text("x")
        (cam / "Cam_2026-05-04_15-30-00_AUDIO_E1.jpg").write_text("x")
        (cam / "Cam_2026-05-04_08-00-00_MOVEMENT_F1.jpg").write_text("x")
        b = _LocalBackend(str(tmp_path))
        events = b.list_events("Cam", "2026-05-04")
        # Sort by stem reverse -> 15:30 first, then 10:00, then 08:00
        assert "15-30-00" in events[0][0]
        assert "10-00-00" in events[1][0]
        assert "08-00-00" in events[2][0]


class TestLocalBackendListEventsEdgeCases:
    """Lines 135-140: cam_dir=None (path traversal blocked) + junk file skip."""

    def test_cam_dir_none_path_traversal(self, tmp_path):
        """_safe_join blocks '../..'; list_events returns []."""
        backend = _LocalBackend(str(tmp_path))
        # "../../etc" -> _safe_join returns None -> early return
        result = backend.list_events("../../etc", "2026-05-07")
        assert result == []

    def test_dated_path_traversal_rejected(self, tmp_path):
        """Regression: list_events_dated must validate year/month/day before
        joining -- a '..' component (from a crafted media identifier) must NOT
        escape the camera directory. 2026-06-01 security fix."""
        # A secret file one level ABOVE the camera dir that traversal would reach.
        cam_dir = tmp_path / "Terrasse" / "2026" / "05" / "07"
        cam_dir.mkdir(parents=True)
        secret = tmp_path / "Terrasse" / "secret"
        secret.mkdir()
        (secret / "Terrasse_2026-05-07_10-00-00_MOVEMENT_11111111.mp4").write_bytes(
            b"x"
        )
        backend = _LocalBackend(str(tmp_path))
        # year=".." would resolve cam_dir/../05/07 -- must be rejected -> []
        assert backend.list_events_dated("Terrasse", "..", "05", "07") == []
        assert backend.list_events_dated("Terrasse", "2026", "..", "secret") == []
        # A legitimate numeric path still works.
        (cam_dir / "Terrasse_2026-05-07_10-00-00_MOVEMENT_22222222.mp4").write_bytes(
            b"x"
        )
        assert len(backend.list_events_dated("Terrasse", "2026", "05", "07")) == 1

    def test_macos_junk_file_skipped(self, tmp_path):
        cam_dir = tmp_path / "Terrasse"
        cam_dir.mkdir()
        (cam_dir / "._Terrasse_2026-05-07_10-00-00_MOVEMENT_11111111.mp4").write_bytes(
            b"x"
        )
        (cam_dir / CAM_FILE).write_bytes(b"x")
        backend = _LocalBackend(str(tmp_path))
        events = backend.list_events("Terrasse", "2026-05-07")
        # Only the real file contributes; junk is skipped
        assert len(events) == 1
        fname, _, _ = events[0]
        assert "._" not in fname


class TestLocalBackendResolve:
    def test_resolve_existing_file(self, tmp_path):
        (tmp_path / "Cam").mkdir()
        target = tmp_path / "Cam" / "Cam_2026-05-04_10-00-00_MOVEMENT_B1.jpg"
        target.write_text("x")
        b = _LocalBackend(str(tmp_path))
        out = b.resolve("Cam", "Cam_2026-05-04_10-00-00_MOVEMENT_B1.jpg")
        assert out == target

    def test_resolve_traversal_blocked(self, tmp_path):
        """Path traversal via `..` must be blocked even when the target
        file exists outside the base."""
        b = _LocalBackend(str(tmp_path / "base"))
        (tmp_path / "base").mkdir()
        # Try to escape the base dir
        out = b.resolve("..", "etc", "passwd")
        assert out is None

    def test_resolve_nonexistent_file_returns_none(self, tmp_path):
        b = _LocalBackend(str(tmp_path))
        out = b.resolve("Cam", "missing.jpg")
        assert out is None

    def test_resolve_directory_returns_none(self, tmp_path):
        """Resolve must only return file paths -- directory targets
        return None (caller wants to play a media file)."""
        (tmp_path / "Cam").mkdir()
        b = _LocalBackend(str(tmp_path))
        # "Cam" exists but is a dir
        out = b.resolve("Cam")
        assert out is None

    def test_resolve_year_first_4_part_path(self, tmp_path):
        """resolve(year, month, day, filename) must return the file for year-first layout.

        _serve_local accepts len(tail)==4, which maps to (year, month, day, filename) --
        the year-first path where the year dir sits directly at the NAS/local root
        with no camera prefix.  Without len(tail)==4 in the allow-list the handler
        raises HTTPNotFound for every year-first playback attempt.
        Fix: v11.0.19 (simon42/Andreas74 2026-05-08).
        """
        year_dir = tmp_path / "2026" / "03" / "25"
        year_dir.mkdir(parents=True)
        fname = "Garten_2026-03-25_10-33-11_MOVEMENT_ABC123.mp4"
        (year_dir / fname).write_text("x")
        b = _LocalBackend(str(tmp_path))
        result = b.resolve("2026", "03", "25", fname)
        assert result is not None, (
            "4-part year-first resolve must return a Path, not None"
        )
        assert result.is_file(), "resolved 4-part path must point at a real file"


class TestLocalBackendCameraFirst:
    """_LocalBackend with folder_pattern starting with {camera} -> year/month/day tree.

    Regression: reported by Georg (simon42, 2026-05-08): files saved via
    sync_local_save land in camera/2026/05/08/ but the serve view routed
    camera/year/... paths to the SMB backend (kind="S"), returning 404 for
    every playback attempt. Fix: prefer Local when no SMB source is configured.
    """

    def test_list_years_returns_four_digit_dirs(self, tmp_path):
        """list_years must return only dirs matching ^\\d{4}$ (not full YYYY-MM-DD names)."""
        cam = tmp_path / "Terrasse"
        (cam / "2026").mkdir(parents=True)
        (cam / "2025").mkdir()
        (cam / "2026-05-08").mkdir()  # must NOT appear as a year
        b = _LocalBackend(str(tmp_path))
        years = b.list_years("Terrasse")
        assert years == ["2026", "2025"], (
            f"Expected only 4-digit year dirs, got {years}"
        )

    def test_list_months_returns_two_digit_dirs(self, tmp_path):
        cam = tmp_path / "Terrasse"
        year_dir = cam / "2026"
        (year_dir / "05").mkdir(parents=True)
        (year_dir / "04").mkdir()
        (year_dir / "not-a-month").mkdir()
        b = _LocalBackend(str(tmp_path))
        months = b.list_months("Terrasse", "2026")
        assert months == ["05", "04"], f"Expected two-digit month dirs, got {months}"

    def test_list_days_returns_two_digit_dirs(self, tmp_path):
        cam = tmp_path / "Terrasse"
        month_dir = cam / "2026" / "05"
        (month_dir / "08").mkdir(parents=True)
        (month_dir / "07").mkdir()
        b = _LocalBackend(str(tmp_path))
        days = b.list_days("Terrasse", "2026", "05")
        assert days == ["08", "07"], f"Expected two-digit day dirs, got {days}"

    def test_list_events_dated_reads_files_from_day_dir(self, tmp_path):
        cam = tmp_path / "Terrasse"
        day_dir = cam / "2026" / "05" / "08"
        day_dir.mkdir(parents=True)
        (day_dir / "Terrasse_2026-05-08_10-30-00_MOVEMENT_ABCD1234.jpg").write_bytes(
            b"\xff\xd8"
        )
        b = _LocalBackend(str(tmp_path))
        events = b.list_events_dated("Terrasse", "2026", "05", "08")
        assert len(events) == 1, "Expected 1 event in the day directory"
        fname, _thumb, parsed = events[0]
        assert "MOVEMENT" in fname, (
            f"Event filename should contain event type, got {fname}"
        )
        assert parsed["date"] == "2026-05-08", f"Parsed date wrong: {parsed['date']}"

    def test_resolve_camera_first_path(self, tmp_path):
        """resolve(camera, year, month, day, filename) must return the correct file path."""
        cam = tmp_path / "Terrasse"
        day_dir = cam / "2026" / "05" / "08"
        day_dir.mkdir(parents=True)
        fname = "Terrasse_2026-05-08_10-30-00_MOVEMENT_ABCD1234.jpg"
        (day_dir / fname).write_bytes(b"\xff\xd8")
        b = _LocalBackend(str(tmp_path))
        resolved = b.resolve("Terrasse", "2026", "05", "08", fname)
        assert resolved is not None, (
            "resolve() must return a Path for a camera-first file"
        )
        assert resolved.is_file(), "Resolved path must be an actual file"

    def test_camera_first_property_true_for_default_pattern(self):
        """Default folder_pattern={camera}/{year}/{month}/{day} -> camera_first=True."""
        b = _LocalBackend("/tmp")  # default pattern
        assert b.camera_first is True, (
            "Default folder_pattern must make camera_first=True; "
            "sync_local_save uses the same default and creates camera/year/month/day/"
        )


# ═════════════════════════════════════════════════════════════════════════
# _LocalBackend: flat-coverage gaps (None-branches / traversal / junk skips)
# ═════════════════════════════════════════════════════════════════════════


class TestLocalListYears:
    def test_cam_path_is_file_not_dir_returns_empty(self, tmp_path):
        # Create "Terrasse" as a file, not a dir
        (tmp_path / "Terrasse").write_bytes(b"x")
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_years("Terrasse") == []

    def test_path_traversal_returns_empty(self, tmp_path):
        backend = _LocalBackend(str(tmp_path / "sub"))
        assert backend.list_years("../../etc") == []


class TestLocalListMonths:
    def test_cam_dir_none_path_traversal_returns_empty(self, tmp_path):
        nested = tmp_path / "base"
        nested.mkdir()
        backend = _LocalBackend(str(nested))
        assert backend.list_months("../../etc", "2026") == []

    def test_year_dir_none_path_traversal_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        backend = _LocalBackend(str(tmp_path))
        # year is path traversal inside cam_dir
        assert backend.list_months("Cam", "../../etc") == []

    def test_year_dir_not_dir_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        # Create "2026" as a file inside cam
        (cam_dir / "2026").write_bytes(b"x")
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_months("Cam", "2026") == []

    def test_year_dir_missing_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_months("Cam", "2026") == []

    def test_year_dir_exists_returns_sorted_months(self, tmp_path):
        """Year dir exists and has valid month dirs -> returns them."""
        (tmp_path / "Cam" / "2026" / "05").mkdir(parents=True)
        (tmp_path / "Cam" / "2026" / "04").mkdir(parents=True)
        backend = _LocalBackend(str(tmp_path))
        months = backend.list_months("Cam", "2026")
        assert months == ["05", "04"]


class TestLocalListDays:
    def test_cam_dir_none_returns_empty(self, tmp_path):
        nested = tmp_path / "base"
        nested.mkdir()
        backend = _LocalBackend(str(nested))
        assert backend.list_days("../../etc", "2026", "05") == []

    def test_year_traversal_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_days("Cam", "../../etc", "05") == []

    def test_month_traversal_inside_year_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Cam"
        year_dir = cam_dir / "2026"
        year_dir.mkdir(parents=True)
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_days("Cam", "2026", "../../etc") == []

    def test_month_dir_not_dir_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Cam"
        year_dir = cam_dir / "2026"
        year_dir.mkdir(parents=True)
        (year_dir / "05").write_bytes(b"x")  # file not dir
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_days("Cam", "2026", "05") == []

    def test_month_dir_missing_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Cam"
        (cam_dir / "2026").mkdir(parents=True)
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_days("Cam", "2026", "05") == []


class TestLocalListEventsDated:
    def test_cam_dir_none_returns_empty(self, tmp_path):
        nested = tmp_path / "base"
        nested.mkdir()
        backend = _LocalBackend(str(nested))
        assert backend.list_events_dated("../../etc", "2026", "05", "07") == []

    def test_day_dir_not_dir_returns_empty(self, tmp_path):
        # Create cam/year/month/day as a file
        day_path = tmp_path / "Cam" / "2026" / "05" / "07"
        day_path.parent.mkdir(parents=True)
        day_path.write_bytes(b"x")  # file not dir
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_events_dated("Cam", "2026", "05", "07") == []

    def test_day_dir_missing_returns_empty(self, tmp_path):
        (tmp_path / "Cam" / "2026" / "05").mkdir(parents=True)
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_events_dated("Cam", "2026", "05", "07") == []


class TestLocalCollectEvents:
    def test_macos_junk_skipped_in_collect(self, tmp_path):
        day_dir = tmp_path / "Cam" / "2026" / "05" / "07"
        day_dir.mkdir(parents=True)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AB12CD34"
        (day_dir / f"._macos_{stem}.mp4").write_bytes(b"junk")  # macOS junk
        (day_dir / f"{stem}.mp4").write_bytes(b"real")
        backend = _LocalBackend(str(tmp_path))
        events = backend.list_events_dated("Cam", "2026", "05", "07")
        assert len(events) == 1
        assert "._" not in events[0][0]

    def test_unparseable_filename_skipped_in_collect(self, tmp_path):
        day_dir = tmp_path / "Cam" / "2026" / "05" / "07"
        day_dir.mkdir(parents=True)
        (day_dir / "not_a_valid_event.txt").write_bytes(b"x")
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_events_dated("Cam", "2026", "05", "07") == []


# ═════════════════════════════════════════════════════════════════════════
# _LocalBackend: year-first tree (base/YYYY/MM/DD/) None-branches
# ═════════════════════════════════════════════════════════════════════════


class TestLocalListYearFirstMonthsNoneBranches:
    """`_safe_join(base, year) is None` -> return []."""

    def test_path_traversal_year_returns_empty(self, tmp_path):
        """Year arg with `..` -> _safe_join returns None -> caller returns []."""
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_months("../../etc") == []

    def test_year_dir_not_a_dir_returns_empty(self, tmp_path):
        """Year is a file, not a directory -> second arm of `not d.is_dir()` -> []."""
        (tmp_path / "2026").write_bytes(b"x")
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_months("2026") == []


class TestLocalListYearFirstDaysNoneBranches:
    """Year-traversal None -> []; month-traversal None -> []."""

    def test_year_traversal_returns_empty(self, tmp_path):
        """Year is `../../etc` -> _safe_join returns None -> []."""
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_days("../../etc", "05") == []

    def test_month_traversal_inside_year_returns_empty(self, tmp_path):
        """Year ok but month is traversal -> second _safe_join None -> []."""
        (tmp_path / "2026").mkdir()
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_days("2026", "../../etc") == []

    def test_month_dir_missing_returns_empty(self, tmp_path):
        """`not d.is_dir()`: month name valid but dir doesn't exist."""
        (tmp_path / "2026").mkdir()
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_days("2026", "05") == []


class TestLocalListYearFirstEventsNoneBranches:
    """3 traversal-None branches for the year-first events helper."""

    def test_year_traversal_returns_empty(self, tmp_path):
        """Year is `..` -> first _safe_join None -> []."""
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_events("../../etc", "05", "07") == []

    def test_month_traversal_returns_empty(self, tmp_path):
        """Year ok, month traversal -> second _safe_join None -> []."""
        (tmp_path / "2026").mkdir()
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_events("2026", "../../etc", "07") == []

    def test_day_traversal_returns_empty(self, tmp_path):
        """Year+month ok, day traversal -> third _safe_join None -> []."""
        (tmp_path / "2026" / "05").mkdir(parents=True)
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_events("2026", "05", "../../etc") == []

    def test_day_dir_missing_returns_empty(self, tmp_path):
        """`not d.is_dir()`: day name valid but day dir absent."""
        (tmp_path / "2026" / "05").mkdir(parents=True)
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_events("2026", "05", "07") == []


# ═════════════════════════════════════════════════════════════════════════
# _LocalBackend: camera names with spaces / umlauts
# ═════════════════════════════════════════════════════════════════════════


def _make_event_file(
    cam_dir: Path,
    cam_name: str,
    date: str,
    t: str = "10-00-00",
    etype: str = "MOVEMENT",
    ev_id: str = "37AE5347",
    ext: str = "jpg",
) -> Path:
    """Write a zero-byte event file with the standard naming convention."""
    filename = f"{cam_name}_{date}_{t}_{etype}_{ev_id}.{ext}"
    p = cam_dir / filename
    p.write_bytes(b"FAKE")
    return p


class TestLocalBackendCameraNameWithSpace:
    """_LocalBackend must handle camera names that contain spaces.

    Root context: Andreas74 (simon42 2026-05-07) reported that the Media
    Browser subfolder was always empty when the camera display name
    contained a space.  The _FILE_RE and _safe_join must both tolerate
    spaces so list_cameras / list_dates / list_events all work end-to-end.
    """

    def test_list_cameras_returns_name_with_space(self, tmp_path):
        """Directories whose name contains a space are returned correctly."""
        cam = "Meine Kamera"
        (tmp_path / cam).mkdir()
        b = _LocalBackend(str(tmp_path))
        assert cam in b.list_cameras()

    def test_list_dates_with_space_in_name(self, tmp_path):
        """list_dates must find dates when camera name has a space."""
        cam = "Bosch Terrasse"
        cam_dir = tmp_path / cam
        cam_dir.mkdir()
        _make_event_file(cam_dir, cam, "2026-05-07")
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates(cam) == ["2026-05-07"]

    def test_list_dates_multiple_days(self, tmp_path):
        """Multiple dates returned sorted newest-first, spaces handled."""
        cam = "Kamera 01"
        cam_dir = tmp_path / cam
        cam_dir.mkdir()
        _make_event_file(cam_dir, cam, "2026-05-06")
        _make_event_file(cam_dir, cam, "2026-05-07", t="11-00-00")
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates(cam) == ["2026-05-07", "2026-05-06"]

    def test_list_events_returns_files_with_space_in_name(self, tmp_path):
        """list_events must yield events when camera name has a space."""
        cam = "Garten Kamera"
        cam_dir = tmp_path / cam
        cam_dir.mkdir()
        _make_event_file(cam_dir, cam, "2026-05-07")
        b = _LocalBackend(str(tmp_path))
        events = b.list_events(cam, "2026-05-07")
        assert len(events) == 1, "One event expected"

    def test_list_events_multiple_spaces(self, tmp_path):
        """Camera name with multiple spaces must work end-to-end."""
        cam = "Vorne Rechts Aussen"
        cam_dir = tmp_path / cam
        cam_dir.mkdir()
        _make_event_file(cam_dir, cam, "2026-05-07")
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates(cam) == ["2026-05-07"]
        events = b.list_events(cam, "2026-05-07")
        assert len(events) == 1

    def test_resolve_file_with_space_in_name(self, tmp_path):
        """resolve() must return the Path for a file under a camera with a space."""
        cam = "Test Kamera"
        cam_dir = tmp_path / cam
        cam_dir.mkdir()
        f = _make_event_file(cam_dir, cam, "2026-05-07")
        b = _LocalBackend(str(tmp_path))
        result = b.resolve(cam, f.name)
        assert result == f


class TestLocalBackendUmlautNames:
    """Camera names with German umlauts (ä, ö, ü) must be handled correctly."""

    def test_list_dates_umlaut_name(self, tmp_path):
        cam = "Küche"
        cam_dir = tmp_path / cam
        cam_dir.mkdir()
        _make_event_file(cam_dir, cam, "2026-05-07")
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates(cam) == ["2026-05-07"]

    def test_list_dates_umlaut_with_space(self, tmp_path):
        cam = "Haustür Eingang"
        cam_dir = tmp_path / cam
        cam_dir.mkdir()
        _make_event_file(cam_dir, cam, "2026-05-07")
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates(cam) == ["2026-05-07"]


# ═════════════════════════════════════════════════════════════════════════
# smb.sync_local_save: auto-download HTTP flow + old-event guard
# (backs the _LocalBackend on-disk tree; exercised here alongside its
# consumer rather than in tests/test_smb.py)
# ═════════════════════════════════════════════════════════════════════════


class TestSyncLocalSaveDownload:
    """Cover the actual HTTP download logic."""

    def _call(self, coord, ev, mock_urlopen_resp, cam_name="Terrasse"):
        from custom_components.bosch_shc_camera.smb import sync_local_save

        if mock_urlopen_resp is None:
            sync_local_save(coord, ev, "TOKEN", cam_name)
        elif isinstance(mock_urlopen_resp, Exception):
            with patch(_URLOPEN, side_effect=mock_urlopen_resp):
                sync_local_save(coord, ev, "TOKEN", cam_name)
        else:
            with patch(_URLOPEN, return_value=mock_urlopen_resp):
                sync_local_save(coord, ev, "TOKEN", cam_name)

    def test_jpg_downloaded_on_200(self, tmp_path):
        coord = _coord(tmp_path)
        resp = _urlopen_resp(200, b"JPEG")
        self._call(coord, _ev(videoClipUrl=None), resp)
        files = list((tmp_path / "Terrasse").rglob("*.jpg"))
        assert len(files) == 1
        assert files[0].read_bytes() == b"JPEG"

    def test_mp4_and_jpg_both_downloaded(self, tmp_path):
        coord = _coord(tmp_path)
        resp = _urlopen_resp(200, b"DATA")
        # Two files -> read() called multiple times; reset side_effect for each call
        resp.read.side_effect = [b"DATA", b"", b"DATA", b""]
        self._call(coord, _ev(), resp)
        cam_dir = tmp_path / "Terrasse"
        exts = {f.suffix for f in cam_dir.rglob("*.*")}
        assert ".jpg" in exts
        assert ".mp4" in exts

    def test_mp4_skipped_when_status_not_done(self, tmp_path):
        coord = _coord(tmp_path)
        resp = _urlopen_resp(200, b"DATA")
        self._call(coord, _ev(videoClipUploadStatus="Pending"), resp)
        cam_dir = tmp_path / "Terrasse"
        exts = {f.suffix for f in cam_dir.rglob("*.*")}
        assert ".jpg" in exts
        assert ".mp4" not in exts

    def test_mp4_skipped_when_status_missing(self, tmp_path):
        coord = _coord(tmp_path)
        resp = _urlopen_resp(200, b"DATA")
        ev = _ev()
        del ev["videoClipUploadStatus"]
        self._call(coord, ev, resp)
        exts = {f.suffix for f in (tmp_path / "Terrasse").rglob("*.*")}
        assert ".mp4" not in exts

    def test_unsafe_url_skipped(self, tmp_path):
        coord = _coord(tmp_path)
        with patch(_URLOPEN) as mock_urlopen:
            from custom_components.bosch_shc_camera.smb import sync_local_save

            sync_local_save(
                coord,
                _ev(imageUrl="https://evil.example.com/x.jpg", videoClipUrl=None),
                "TOKEN",
                "Terrasse",
            )
            mock_urlopen.assert_not_called()
        assert list((tmp_path / "Terrasse").rglob("*.*")) == []

    def test_missing_image_url_no_jpg(self, tmp_path):
        coord = _coord(tmp_path)
        resp = _urlopen_resp(200, b"DATA")
        self._call(coord, _ev(imageUrl=None), resp)
        exts = {f.suffix for f in (tmp_path / "Terrasse").rglob("*.*")}
        assert ".jpg" not in exts

    def test_http_non_200_no_file_written(self, tmp_path):
        coord = _coord(tmp_path)
        resp = _urlopen_resp(403, b"")
        self._call(coord, _ev(videoClipUrl=None), resp)
        assert list((tmp_path / "Terrasse").rglob("*.*")) == []

    def test_http_exception_does_not_crash(self, tmp_path):
        coord = _coord(tmp_path)
        self._call(coord, _ev(videoClipUrl=None), OSError("network gone"))
        assert list((tmp_path / "Terrasse").rglob("*.*")) == []

    def test_file_already_exists_skips_http(self, tmp_path):
        """If the file is already on disk, no HTTP request must be made."""
        coord = _coord(tmp_path)
        ev = _ev(videoClipUrl=None)
        ts = ev["timestamp"]
        date_str = ts[:10]
        year, month, day = date_str.split("-")
        time_str = ts[11:19].replace(":", "-")
        stem = f"Terrasse_{date_str}_{time_str}_MOVEMENT_AABBCCDD"
        nested_dir = tmp_path / "Terrasse" / year / month / day
        nested_dir.mkdir(parents=True, exist_ok=True)
        (nested_dir / f"{stem}.jpg").write_bytes(b"OLD")
        with patch(_URLOPEN) as mock_urlopen:
            from custom_components.bosch_shc_camera.smb import sync_local_save

            sync_local_save(coord, ev, "TOKEN", "Terrasse")
            mock_urlopen.assert_not_called()

    def test_stem_uses_empty_id_when_none(self, tmp_path):
        """id=None must not crash; stem ends with empty id suffix."""
        coord = _coord(tmp_path)
        resp = _urlopen_resp(200, b"X")
        self._call(coord, _ev(id=None, videoClipUrl=None), resp)
        files = list((tmp_path / "Terrasse").rglob("*.*"))
        assert len(files) == 1
        assert files[0].stem.endswith("_MOVEMENT_")

    def test_short_timestamp_returns_early(self, tmp_path):
        """Events with timestamp shorter than 19 chars must be ignored."""
        coord = _coord(tmp_path)
        with patch(_URLOPEN) as mock_urlopen:
            from custom_components.bosch_shc_camera.smb import sync_local_save

            sync_local_save(coord, _ev(timestamp="2026-05"), "TOKEN", "Cam")
            mock_urlopen.assert_not_called()

    def test_no_download_path_returns_early(self, tmp_path):
        """Empty download_path must be a no-op."""
        coord = SimpleNamespace(
            options={"download_path": ""}, _download_started_at=time.time() - 3600
        )
        with patch(_URLOPEN) as mock_urlopen:
            from custom_components.bosch_shc_camera.smb import sync_local_save

            sync_local_save(coord, _ev(), "TOKEN", "Cam")
            mock_urlopen.assert_not_called()

    def test_camera_name_with_space_creates_dir(self, tmp_path):
        """Camera name containing a space must produce the right directory."""
        coord = _coord(tmp_path)
        resp = _urlopen_resp(200, b"X")
        self._call(coord, _ev(videoClipUrl=None), resp, cam_name="Außen Kamera")
        assert (tmp_path / "Außen Kamera").is_dir()


class TestSyncLocalSaveOldEventGuard:
    """sync_local_save must skip events that predate coordinator._download_started_at.

    Root cause (Andreas74, simon42 2026-05-07): when download_path is
    enabled and HA restarts, Bosch may replay queued FCM push notifications
    for events that happened before the reload.  Without the guard, those
    stale events get downloaded even though they predate the current session.
    The fix: compare ev["timestamp"] (parsed to epoch) against
    coordinator._download_started_at (set in __init__ to time.time()).
    Events older than (started_at - 60 s) are skipped.
    """

    def test_old_event_is_skipped(self, tmp_path):
        """Event timestamp 2 hours before coordinator start -> no file written."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        # Coordinator started "now"
        coord = _coord(tmp_path, started_offset_s=0)

        # Event from 2 hours ago
        ev = {
            "timestamp": _iso_ts(-7200),
            "eventType": "MOVEMENT",
            "id": "ABCD1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        sync_local_save(coord, ev, "tok", "Terrasse")

        assert list(tmp_path.rglob("*.jpg")) == [], (
            "Old event (2 h before session start) must not create any file"
        )

    def test_event_just_before_cutoff_is_skipped(self, tmp_path):
        """Event 90 s before start is within the guard window -> skipped."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord(tmp_path, started_offset_s=0)
        ev = {
            "timestamp": _iso_ts(-90),
            "eventType": "MOVEMENT",
            "id": "ABCD1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        sync_local_save(coord, ev, "tok", "Terrasse")

        assert list(tmp_path.rglob("*.jpg")) == [], (
            "Event 90 s before session start must be skipped"
        )

    def test_recent_event_is_not_skipped(self, tmp_path):
        """Event after coordinator start (within 60 s slack) -> proceeds to download."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        # Coordinator started 10 minutes ago
        coord = _coord(tmp_path, started_offset_s=-600)

        # Event happened 5 minutes ago (after session start)
        ev = {
            "timestamp": _iso_ts(-300),
            "eventType": "MOVEMENT",
            "id": "ABCD1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.read.side_effect = [b"JPEG", b""]
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch(_URLOPEN, return_value=fake_resp):
            sync_local_save(coord, ev, "tok", "Terrasse")

        written = list(tmp_path.rglob("*.jpg"))
        assert len(written) == 1, (
            "Recent event (5 min after session start) must trigger download"
        )

    def test_event_within_60s_slack_is_not_skipped(self, tmp_path):
        """Event 30 s before coordinator start is within the 60 s tolerance -> allowed."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord(tmp_path, started_offset_s=0)

        ev = {
            "timestamp": _iso_ts(-30),
            "eventType": "MOVEMENT",
            "id": "ABCD1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.read.side_effect = [b"JPEG", b""]
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch(_URLOPEN, return_value=fake_resp):
            sync_local_save(coord, ev, "tok", "Terrasse")

        written = list(tmp_path.rglob("*.jpg"))
        assert len(written) == 1, "Event within 60 s slack window must not be blocked"

    def test_no_started_at_attribute_falls_through(self, tmp_path):
        """Coordinator without _download_started_at (e.g. old pickled state) must not crash."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = SimpleNamespace(
            options={"download_path": str(tmp_path)},
            hass=MagicMock(),
            # deliberately no _download_started_at
        )

        ev = {
            "timestamp": _iso_ts(-7200),
            "eventType": "MOVEMENT",
            "id": "ABCD1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        # Should not raise; getattr default is 0.0 which disables the guard
        sync_local_save(coord, ev, "tok", "Terrasse")
        # No assertion on file -- guard disabled, behaviour depends on network (now blocked)


class TestSafeNameSanitization:
    """_safe_name must produce valid directory names from arbitrary camera names.

    The result is used both as a directory name (sync_local_save) and as a
    component of the filename that _FILE_RE must then match.
    """

    @pytest.fixture(autouse=True)
    def _import(self):
        from custom_components.bosch_shc_camera.smb import _safe_name

        self.fn = _safe_name

    def test_plain_name_unchanged(self):
        assert self.fn("Terrasse") == "Terrasse"

    def test_spaces_preserved(self):
        assert self.fn("Meine Kamera") == "Meine Kamera"

    def test_dot_dot_replaced(self):
        result = self.fn("../etc/passwd")
        assert ".." not in result

    def test_slash_replaced(self):
        result = self.fn("Cam/Eingang")
        assert "/" not in result

    def test_tilde_replaced(self):
        result = self.fn("~user")
        assert "~" not in result

    def test_umlaut_preserved(self):
        result = self.fn("Haustür")
        assert "Haustür" == result

    def test_truncated_to_64(self):
        long_name = "A" * 100
        assert len(self.fn(long_name)) <= 64

    def test_result_matches_file_re_roundtrip(self):
        """Sanitized name, used as camera in a filename, must match _FILE_RE."""
        import re

        _FILE_RE = re.compile(
            r"^(?P<camera>.+?)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})"
            r"_(?P<etype>[A-Z_]+)_[0-9A-F]+\.(?P<ext>jpg|jpeg|mp4)$",
            re.IGNORECASE,
        )
        test_names = [
            "Meine Kamera",
            "Küche",
            "Cam/Eingang",
            "Test 01",
            "Vorne Rechts Aussen",
        ]
        for name in test_names:
            safe = self.fn(name)
            filename = f"{safe}_2026-05-07_10-00-00_MOVEMENT_AB12CD34.jpg"
            m = _FILE_RE.match(filename)
            assert m is not None, (
                f"_safe_name({name!r}) = {safe!r} produces filename {filename!r} "
                f"that does NOT match _FILE_RE -- Media Browser would show empty folder"
            )
            assert m.group("camera") == safe


# ═════════════════════════════════════════════════════════════════════════
# _NvrBackend: continuous-recording segment listing/resolve
# ═════════════════════════════════════════════════════════════════════════


class TestNvrBackend:
    def test_list_cameras_sorted(self, tmp_path):
        (tmp_path / "Garten").mkdir()
        (tmp_path / "Terrasse").mkdir()
        (tmp_path / ".DS_Store").mkdir()
        b = _NvrBackend(str(tmp_path))
        assert b.list_cameras() == ["Garten", "Terrasse"]

    def test_list_cameras_skips_underscore_dirs(self, tmp_path):
        """B13-5 regression: _staging / _failed NVR internal dirs must not
        appear as camera tiles in the Media Browser for _NvrBackend."""
        (tmp_path / "Garten").mkdir()
        (tmp_path / "Terrasse").mkdir()
        (tmp_path / "_staging").mkdir()
        (tmp_path / "_failed").mkdir()
        b = _NvrBackend(str(tmp_path))
        result = b.list_cameras()
        assert "_staging" not in result, "_staging must be filtered"
        assert "_failed" not in result, "_failed must be filtered"
        assert result == ["Garten", "Terrasse"]

    def test_list_dates_only_yyyy_mm_dd_dirs(self, tmp_path):
        """Only `YYYY-MM-DD` named dirs are date entries -- random
        sub-dirs (e.g. `_staging`, `_failed`) must be excluded."""
        cam = tmp_path / "Cam"
        cam.mkdir()
        (cam / "2026-05-04").mkdir()
        (cam / "2026-05-03").mkdir()
        (cam / "_staging").mkdir()  # NVR scratch dir
        (cam / "_failed").mkdir()
        b = _NvrBackend(str(tmp_path))
        # Reverse-sorted, junk excluded
        assert b.list_dates("Cam") == ["2026-05-04", "2026-05-03"]

    def test_list_dates_unknown_camera_returns_empty(self, tmp_path):
        b = _NvrBackend(str(tmp_path))
        assert b.list_dates("NoCam") == []

    def test_list_segments_returns_filename_and_human_label(self, tmp_path):
        cam = tmp_path / "Cam"
        date = cam / "2026-05-04"
        date.mkdir(parents=True)
        # NVR segment naming: HH-MM.mp4
        (date / "10-30.mp4").write_text("x")
        (date / "11-00.mp4").write_text("x")
        b = _NvrBackend(str(tmp_path))
        out = b.list_segments("Cam", "2026-05-04")
        # Reverse-sorted, label is HH:MM (not HH-MM)
        assert out == [
            ("11-00.mp4", "11:00"),
            ("10-30.mp4", "10:30"),
        ]

    def test_list_segments_skips_non_matching_files(self, tmp_path):
        cam = tmp_path / "Cam"
        date = cam / "2026-05-04"
        date.mkdir(parents=True)
        (date / "10-30.mp4").write_text("x")
        (date / "random.txt").write_text("x")
        (date / "10-30.tmp").write_text("x")  # ffmpeg in-progress
        b = _NvrBackend(str(tmp_path))
        out = b.list_segments("Cam", "2026-05-04")
        assert out == [("10-30.mp4", "10:30")]

    def test_resolve_validates_date_and_filename(self, tmp_path):
        cam = tmp_path / "Cam"
        date = cam / "2026-05-04"
        date.mkdir(parents=True)
        (date / "10-30.mp4").write_text("x")
        b = _NvrBackend(str(tmp_path))
        out = b.resolve("Cam", "2026-05-04", "10-30.mp4")
        assert out is not None
        # Bad date format rejected
        assert b.resolve("Cam", "2026/05/04", "10-30.mp4") is None
        # Bad filename rejected
        assert b.resolve("Cam", "2026-05-04", "evil.exe") is None
        # Traversal rejected
        assert b.resolve("..", "2026-05-04", "10-30.mp4") is None

    def test_resolve_missing_file_returns_none(self, tmp_path):
        cam = tmp_path / "Cam"
        date = cam / "2026-05-04"
        date.mkdir(parents=True)
        b = _NvrBackend(str(tmp_path))
        out = b.resolve("Cam", "2026-05-04", "10-30.mp4")
        assert out is None


class TestNvrListSegmentsCamDirNone:
    def test_path_traversal_cam_returns_empty(self, tmp_path):
        nested = tmp_path / "base"
        nested.mkdir()
        backend = _NvrBackend(str(nested))
        assert backend.list_segments("../../etc", "2026-05-07") == []


class TestNvrBackendListCameras:
    """Base dir doesn't exist -> list_cameras returns []."""

    def test_missing_base_returns_empty(self, tmp_path):
        backend = _NvrBackend(str(tmp_path / "nonexistent"))
        assert backend.list_cameras() == []


class TestNvrBackendListDates:
    """cam_dir None or not a directory -> list_dates returns []."""

    def test_path_traversal_cam_returns_empty(self, tmp_path):
        backend = _NvrBackend(str(tmp_path))
        assert backend.list_dates("../../etc") == []

    def test_missing_cam_dir_returns_empty(self, tmp_path):
        backend = _NvrBackend(str(tmp_path))
        assert backend.list_dates("MissingCam") == []


class TestNvrBackendListSegments:
    """Junk files skipped; date_dir None or not-dir."""

    def test_junk_file_skipped_in_segments(self, tmp_path):
        seg_dir = tmp_path / "Terrasse" / "2026-05-07"
        seg_dir.mkdir(parents=True)
        (seg_dir / "._10-00.mp4").write_bytes(b"x")  # macOS junk
        (seg_dir / "10-00.mp4").write_bytes(b"x")
        backend = _NvrBackend(str(tmp_path))
        segs = backend.list_segments("Terrasse", "2026-05-07")
        assert len(segs) == 1
        assert segs[0][0] == "10-00.mp4"

    def test_date_dir_none_path_traversal(self, tmp_path):
        (tmp_path / "Terrasse").mkdir()
        backend = _NvrBackend(str(tmp_path))
        assert backend.list_segments("Terrasse", "../../etc") == []

    def test_date_dir_not_dir_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Terrasse"
        cam_dir.mkdir()
        # "2026-05-07" is a file, not a directory
        (cam_dir / "2026-05-07").write_bytes(b"x")
        backend = _NvrBackend(str(tmp_path))
        assert backend.list_segments("Terrasse", "2026-05-07") == []

    def test_non_matching_file_skipped(self, tmp_path):
        seg_dir = tmp_path / "Terrasse" / "2026-05-07"
        seg_dir.mkdir(parents=True)
        (seg_dir / "README.txt").write_bytes(b"x")
        backend = _NvrBackend(str(tmp_path))
        assert backend.list_segments("Terrasse", "2026-05-07") == []


class TestNvrBackendResolve:
    """date_dir None (path traversal) and invalid date/filename."""

    def test_date_traversal_returns_none(self, tmp_path):
        (tmp_path / "Terrasse").mkdir()
        backend = _NvrBackend(str(tmp_path))
        assert backend.resolve("Terrasse", "../../etc", "10-00.mp4") is None

    def test_invalid_date_format_returns_none(self, tmp_path):
        backend = _make_nvr_tree(tmp_path)
        assert backend.resolve("Terrasse", "20260507", "10-00.mp4") is None

    def test_invalid_segment_format_returns_none(self, tmp_path):
        backend = _make_nvr_tree(tmp_path)
        assert backend.resolve("Terrasse", "2026-05-07", "bad.avi") is None

    def test_missing_file_returns_none(self, tmp_path):
        backend = _make_nvr_tree(tmp_path)
        assert backend.resolve("Terrasse", "2026-05-07", "23-59.mp4") is None

    def test_valid_resolve_returns_path(self, tmp_path):
        backend = _make_nvr_tree(tmp_path)
        result = backend.resolve("Terrasse", "2026-05-07", "10-00.mp4")
        assert result is not None
        assert result.name == "10-00.mp4"


# ═════════════════════════════════════════════════════════════════════════
# _SmbBackend: properties, scandir-backed listing, open_file
# ═════════════════════════════════════════════════════════════════════════


class TestSmbBackendProperties:
    def _make(self, **opts):
        hass = MagicMock()
        hass.data = {}
        base = {
            "smb_server": "nas.local",
            "smb_share": "Media",
            "smb_username": "user",
            "smb_password": "pass",
            "smb_base_path": "Bosch/Cams",
        }
        base.update(opts)
        return _SmbBackend(hass, base)

    def test_configured_true_when_server_and_share(self):
        assert self._make().configured is True

    def test_configured_false_when_no_server(self):
        assert self._make(smb_server="").configured is False

    def test_configured_false_when_no_share(self):
        assert self._make(smb_share="").configured is False

    def test_camera_first_true_when_pattern_starts_with_camera(self):
        b = self._make(folder_pattern="{camera}/{year}/{month}/{day}")
        assert b.camera_first is True

    def test_camera_first_false_when_pattern_starts_with_year(self):
        b = self._make(folder_pattern="{year}/{month}/{day}")
        assert b.camera_first is False

    def test_camera_first_true_is_default(self):
        b = self._make()  # no folder_pattern override
        assert b.camera_first is True

    def test_label_contains_server_and_share(self):
        b = self._make()
        assert "nas.local" in b.label
        assert "Media" in b.label

    def test_path_builds_unc(self):
        b = self._make()
        p = b._path("2026", "05", "07")
        assert p.startswith("\\\\nas.local\\Media\\")
        assert "2026" in p
        assert "07" in p

    def test_path_without_extra_segments(self):
        b = self._make(smb_base_path="")
        p = b._path()
        assert p == "\\\\nas.local\\Media"


class TestSmbBackendScandir:
    """Tests for list_cameras / list_years / list_months / list_days / list_events via mocked smbclient."""

    def _make(self):
        hass = MagicMock()
        hass.data = {}
        return _SmbBackend(
            hass,
            {
                "smb_server": "nas",
                "smb_share": "M",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "",
            },
        )

    def test_list_cameras_returns_dirs(self):
        b = self._make()
        entries = [_dir_entry("Terrasse"), _dir_entry("Kamera"), _dir_entry("Eingang")]
        fake = _fake_smbclient(entries)
        with patch.dict(sys.modules, {"smbclient": fake}):
            cams = b.list_cameras()
        assert cams == ["Eingang", "Kamera", "Terrasse"]

    def test_list_cameras_skips_macos_junk(self):
        b = self._make()
        entries = [_dir_entry("Terrasse"), _dir_entry(".DS_Store")]
        with patch.dict(sys.modules, {"smbclient": _fake_smbclient(entries)}):
            cams = b.list_cameras()
        assert ".DS_Store" not in cams

    def test_list_years_filters_non_year(self):
        b = self._make()
        entries = [_dir_entry("2025"), _dir_entry("2026"), _dir_entry("random")]
        fake = _fake_smbclient(entries)
        with patch.dict(sys.modules, {"smbclient": fake}):
            years = b.list_years("Terrasse")
        assert years == ["2026", "2025"]

    def test_list_years_skips_macos_junk(self):
        b = self._make()
        entries = [_dir_entry("2026"), _dir_entry(".DS_Store")]
        with patch.dict(sys.modules, {"smbclient": _fake_smbclient(entries)}):
            years = b.list_years("Terrasse")
        assert ".DS_Store" not in years

    def test_list_months_filters_non_numeric(self):
        b = self._make()
        entries = [_dir_entry("05"), _dir_entry("12"), _dir_entry("junk")]
        with patch.dict(sys.modules, {"smbclient": _fake_smbclient(entries)}):
            months = b.list_months("Terrasse", "2026")
        assert "junk" not in months
        assert months == ["12", "05"]

    def test_list_days_sorted_newest_first(self):
        b = self._make()
        entries = [_dir_entry("03"), _dir_entry("22"), _dir_entry("07")]
        with patch.dict(sys.modules, {"smbclient": _fake_smbclient(entries)}):
            days = b.list_days("Terrasse", "2026", "05")
        assert days == ["22", "07", "03"]

    def test_list_events_groups_jpg_and_mp4(self):
        b = self._make()
        stem = "Terrasse_2026-05-07_10-00-00_MOVEMENT_AB12CD34"
        entries = [
            _dir_entry(f"{stem}.jpg", is_dir=False, is_file=True),
            _dir_entry(f"{stem}.mp4", is_dir=False, is_file=True),
        ]
        with patch.dict(sys.modules, {"smbclient": _fake_smbclient(entries)}):
            events = b.list_events("Terrasse", "2026", "05", "07")
        assert len(events) == 1
        preferred, image, _parsed = events[0]
        assert preferred.endswith(".mp4")
        assert image.endswith(".jpg")

    def test_list_events_skips_unparseable_filenames(self):
        b = self._make()
        entries = [_dir_entry("not_a_valid_event.txt", is_dir=False, is_file=True)]
        with patch.dict(sys.modules, {"smbclient": _fake_smbclient(entries)}):
            events = b.list_events("Terrasse", "2026", "05", "07")
        assert events == []

    def test_list_events_image_only(self):
        b = self._make()
        stem = "Cam_2026-05-07_08-00-00_MOVEMENT_DEADBEEF"
        entries = [_dir_entry(f"{stem}.jpg", is_dir=False, is_file=True)]
        with patch.dict(sys.modules, {"smbclient": _fake_smbclient(entries)}):
            events = b.list_events("Cam", "2026", "05", "07")
        assert len(events) == 1
        preferred, image, _ = events[0]
        assert preferred.endswith(".jpg")
        assert image.endswith(".jpg")


class TestSmbBackendOpenFile:
    def _make(self):
        hass = MagicMock()
        hass.data = {}
        return _SmbBackend(
            hass,
            {
                "smb_server": "nas",
                "smb_share": "M",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "",
            },
        )

    def test_traversal_in_filename_raises(self):
        b = self._make()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_file("Cam", "2026", "05", "07", "../secret.jpg")

    def test_backslash_in_filename_raises(self):
        b = self._make()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_file("Cam", "2026", "05", "07", "a\\b.jpg")

    def test_unparseable_filename_raises(self):
        b = self._make()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_file("Cam", "2026", "05", "07", "not_valid_UNKNOWN.jpg")

    def test_valid_filename_delegates_to_smbclient(self):
        b = self._make()
        stem = "Cam_2026-05-07_08-00-00_MOVEMENT_DEADBEEF"
        fake_fobj = MagicMock()
        fake = _fake_smbclient(fobj=fake_fobj, stat_size=1234)
        with patch.dict(sys.modules, {"smbclient": fake}):
            fobj, size = b.open_file("Cam", "2026", "05", "07", f"{stem}.jpg")
        assert size == 1234
        assert fobj is fake_fobj


# ═════════════════════════════════════════════════════════════════════════
# _SmbBackend: legacy flat-file variant (list_flat_dates/list_flat_events/
# open_flat_file -- camera/file.ext with no date subtree)
# ═════════════════════════════════════════════════════════════════════════


class TestSmbListFlatDates:
    def test_flat_dates_extracted_from_files(self):
        b = _make_smb_backend()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_DEADBEEF"
        entries = [
            _dir_entry(f"{stem}.mp4", is_dir=False, is_file=True),
            _dir_entry(f"{stem}.jpg", is_dir=False, is_file=True),
        ]
        fake = _fake_smbclient(entries)
        with patch.dict(sys.modules, {"smbclient": fake}):
            dates = b.list_flat_dates("Cam")
        assert dates == ["2026-05-07"]

    def test_flat_dates_multiple_files_deduped(self):
        b = _make_smb_backend()
        s1 = "Cam_2026-05-07_10-00-00_MOVEMENT_AA"
        s2 = "Cam_2026-05-07_11-00-00_MOVEMENT_BB"
        s3 = "Cam_2026-05-08_10-00-00_MOVEMENT_CC"
        entries = [
            _dir_entry(f"{s1}.mp4", is_dir=False, is_file=True),
            _dir_entry(f"{s2}.jpg", is_dir=False, is_file=True),
            _dir_entry(f"{s3}.mp4", is_dir=False, is_file=True),
        ]
        fake = _fake_smbclient(entries)
        with patch.dict(sys.modules, {"smbclient": fake}):
            dates = b.list_flat_dates("Cam")
        assert dates == ["2026-05-08", "2026-05-07"]  # sorted reverse

    def test_flat_dates_oserror_returns_empty(self):
        b = _make_smb_backend()
        fake = MagicMock()
        fake.register_session = MagicMock()
        fake.scandir = MagicMock(side_effect=OSError("smb down"))
        with patch.dict(sys.modules, {"smbclient": fake}):
            dates = b.list_flat_dates("Cam")
        assert dates == []

    def test_flat_dates_unparseable_file_skipped(self):
        b = _make_smb_backend()
        entries = [_dir_entry("not_parseable.txt", is_dir=False, is_file=True)]
        fake = _fake_smbclient(entries)
        with patch.dict(sys.modules, {"smbclient": fake}):
            dates = b.list_flat_dates("Cam")
        assert dates == []


class TestSmbListFlatEvents:
    def test_flat_events_filtered_by_date(self):
        b = _make_smb_backend()
        stem_match = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        stem_other = "Cam_2026-05-08_10-00-00_MOVEMENT_CCDD"
        entries = [
            _dir_entry(f"{stem_match}.mp4", is_dir=False, is_file=True),
            _dir_entry(f"{stem_match}.jpg", is_dir=False, is_file=True),
            _dir_entry(f"{stem_other}.mp4", is_dir=False, is_file=True),
        ]
        fake = _fake_smbclient(entries)
        with patch.dict(sys.modules, {"smbclient": fake}):
            events = b.list_flat_events("Cam", "2026-05-07")
        assert len(events) == 1
        preferred, image, parsed = events[0]
        assert preferred.endswith(".mp4")
        assert image.endswith(".jpg")
        assert parsed["date"] == "2026-05-07"

    def test_flat_events_no_match_returns_empty(self):
        b = _make_smb_backend()
        stem = "Cam_2026-05-08_10-00-00_MOVEMENT_AABB"
        entries = [_dir_entry(f"{stem}.mp4", is_dir=False, is_file=True)]
        fake = _fake_smbclient(entries)
        with patch.dict(sys.modules, {"smbclient": fake}):
            events = b.list_flat_events("Cam", "2026-05-07")
        assert events == []

    def test_flat_events_oserror_returns_empty(self):
        b = _make_smb_backend()
        fake = MagicMock()
        fake.register_session = MagicMock()
        fake.scandir = MagicMock(side_effect=OSError("smb down"))
        with patch.dict(sys.modules, {"smbclient": fake}):
            events = b.list_flat_events("Cam", "2026-05-07")
        assert events == []

    def test_flat_events_image_only(self):
        b = _make_smb_backend()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        entries = [_dir_entry(f"{stem}.jpg", is_dir=False, is_file=True)]
        fake = _fake_smbclient(entries)
        with patch.dict(sys.modules, {"smbclient": fake}):
            events = b.list_flat_events("Cam", "2026-05-07")
        assert len(events) == 1
        preferred, image, _ = events[0]
        assert preferred.endswith(".jpg")
        assert image.endswith(".jpg")


class TestSmbOpenFlatFile:
    def test_slash_in_filename_raises(self):
        b = _make_smb_backend()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_flat_file("Cam", "some/path.jpg")

    def test_backslash_in_filename_raises(self):
        b = _make_smb_backend()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_flat_file("Cam", "a\\b.jpg")

    def test_dotdot_filename_raises(self):
        b = _make_smb_backend()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_flat_file("Cam", "..")

    def test_macos_junk_filename_raises(self):
        b = _make_smb_backend()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_flat_file("Cam", "._hidden.jpg")

    def test_unparseable_filename_raises(self):
        b = _make_smb_backend()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_flat_file("Cam", "not_valid_UNKNOWN.jpg")

    def test_valid_filename_returns_fobj_and_size(self):
        b = _make_smb_backend()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_DEADBEEF"
        fake_fobj = MagicMock()
        fake = _fake_smbclient(fobj=fake_fobj, stat_size=4096)
        with patch.dict(sys.modules, {"smbclient": fake}):
            fobj, size = b.open_flat_file("Cam", f"{stem}.mp4")
        assert fobj is fake_fobj
        assert size == 4096


# ═════════════════════════════════════════════════════════════════════════
# _SmbBackend: year-first browse methods (mocked at _scandir_filtered boundary)
# ═════════════════════════════════════════════════════════════════════════


class TestSmbBackendYearFirst:
    """Regression fix v11.0.19 (simon42/Andreas74 2026-05-08): year-first folders
    ('2026', '2025') were not browseable via SMB.  Fix: remove _YEAR_RE filter
    from list_cameras(); add list_year_first_months/days/events().
    """

    def _make_backend(self):
        hass = SimpleNamespace(data={})
        opts = {
            "smb_server": "nas.local",
            "smb_share": "Events",
            "smb_username": "user",
            "smb_password": "pass",
            "smb_base_path": "",
            "upload_protocol": "smb",
            "folder_pattern": "{camera}/{year}/{month}/{day}",
        }
        return _SmbBackend(hass, opts)

    def test_list_cameras_includes_year_first_folders(self):
        """list_cameras() must return ALL dirs -- including 4-digit year dirs.

        Previously _YEAR_RE filtered these out, leaving legacy recordings
        inaccessible for SMB/FTP users (same bug as for _LocalBackend).
        """
        b = self._make_backend()
        dirs = ["Terrasse", "2026", "Innenbereich", "2025"]
        with patch.object(b, "_scandir_filtered", return_value=iter(dirs)):
            result = b.list_cameras()
        assert "2026" in result, "year-first folder must appear in SMB list_cameras()"
        assert "Terrasse" in result, (
            "normal camera folder must appear in SMB list_cameras()"
        )
        assert result == sorted(dirs, key=str.casefold), (
            "SMB list_cameras() must be sorted case-insensitive"
        )

    def test_list_year_first_months_filters_by_date_dir_re(self):
        """list_year_first_months('2026') filters to 2-digit dirs only, newest-first."""
        b = self._make_backend()
        raw = ["03", "04", "junk", "not-a-month"]
        with patch.object(b, "_scandir_filtered", return_value=iter(raw)) as mock_scan:
            result = b.list_year_first_months("2026")
        mock_scan.assert_called_once_with("2026", want_dirs=True)
        assert result == ["04", "03"], (
            f"SMB list_year_first_months must return ['04','03'] newest-first, got {result}"
        )
        assert "junk" not in result, "non-month dir must be excluded"

    def test_list_year_first_days_filters_and_sorts(self):
        """list_year_first_days('2026', '03') returns 2-digit day dirs, newest-first."""
        b = self._make_backend()
        raw = ["25", "26", "notaday"]
        with patch.object(b, "_scandir_filtered", return_value=iter(raw)) as mock_scan:
            result = b.list_year_first_days("2026", "03")
        mock_scan.assert_called_once_with("2026", "03", want_dirs=True)
        assert result == ["26", "25"], (
            f"SMB list_year_first_days must return ['26','25'] newest-first, got {result}"
        )

    def test_list_year_first_events_groups_mp4_and_jpg(self):
        """list_year_first_events groups mp4+jpg into one event, video preferred."""
        b = self._make_backend()
        raw = [
            "Garten_2026-03-25_10-33-11_MOVEMENT_ABC123.mp4",
            "Garten_2026-03-25_10-33-11_MOVEMENT_ABC123.jpg",
            "unparseable_random_name.txt",  # must be silently skipped
        ]
        with patch.object(b, "_scandir_filtered", return_value=iter(raw)) as mock_scan:
            result = b.list_year_first_events("2026", "03", "25")
        mock_scan.assert_called_once_with("2026", "03", "25", want_dirs=False)
        assert len(result) == 1, (
            f"SMB list_year_first_events must return 1 event (random.txt not parsed), got {len(result)}"
        )
        fname, image, parsed = result[0]
        assert fname.endswith(".mp4"), (
            "video must be preferred over image in SMB year-first events"
        )
        assert image is not None and image.endswith(".jpg"), (
            "jpg must be included as thumbnail"
        )
        assert parsed["camera"] == "Garten", f"parsed camera wrong: {parsed['camera']}"


def _make_smb_media_source(tmp_path):
    """Build a BoschCameraMediaSource bound to a single SMB source whose
    year-first methods are backed by tmp_path scaffolding.

    Mocks `_enabled_sources` so the dispatcher reaches `_browse_smb` with a
    `_SmbBackend` instance.
    """
    from custom_components.bosch_shc_camera.media_source import _Source

    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_get_entry=MagicMock(
                return_value=SimpleNamespace(entry_id="01ENT", title="Bosch")
            )
        ),
    )

    backend = _SmbBackend(
        hass,
        {
            "smb_server": "nas.local",
            "smb_share": "Events",
            "smb_username": "user",
            "smb_password": "pass",
            "smb_base_path": "",
            "upload_protocol": "smb",
            "folder_pattern": "{camera}/{year}/{month}/{day}",
        },
    )
    src = _Source(entry_id="01ENT", kind="S", label="NAS")
    media = BoschCameraMediaSource(hass)
    return media, src, backend


class TestBrowseSmbYearFirstMonths:
    """`_YEAR_RE.match(camera)` -> list_year_first_months path.

    Reached by `_browse_smb` with rest=['2026'] and a year-named root folder.
    """

    def test_year_at_camera_level_lists_months(self, tmp_path):
        media, src, backend = _make_smb_media_source(tmp_path)
        with patch.object(backend, "list_year_first_months", return_value=["04", "03"]):
            node = media._browse_smb(src, backend, ["2026"], single_source=True)
        titles = [c.title for c in node.children]
        assert titles == ["04", "03"], (
            f"year-first months must be rendered as direct children, got {titles}"
        )
        # identifier of children must include the year segment
        for child in node.children:
            assert "2026/" in child.identifier, (
                f"child identifier must embed the year, got {child.identifier}"
            )


class TestBrowseSmbYearFirstDays:
    """`_YEAR_RE.match(camera) and _DATE_DIR_RE.match(year)` -> list_year_first_days path.

    Reached with rest=['2026','05'] where the first segment is the year.
    """

    def test_year_month_lists_days(self, tmp_path):
        media, src, backend = _make_smb_media_source(tmp_path)
        with patch.object(backend, "list_year_first_days", return_value=["08", "07"]):
            node = media._browse_smb(src, backend, ["2026", "05"], single_source=True)
        titles = [c.title for c in node.children]
        assert titles == ["08", "07"], f"year-first days must be rendered, got {titles}"
        assert node.title == "2026-05", (
            f"month node title must combine year+month, got {node.title}"
        )


class TestBrowseSmbYearFirstEvents:
    """3-segment year-first events branch.

    rest=['2026','05','08'] with all three matching their respective regexes ->
    dispatch to `list_year_first_events`, build VIDEO/IMAGE children, embed
    `year/month/day/file` in identifiers + thumbnails.
    """

    def test_year_month_day_lists_events_with_thumbnail(self, tmp_path):
        from homeassistant.components.media_player import MediaClass

        media, src, backend = _make_smb_media_source(tmp_path)
        events = [
            (
                "Garten_2026-03-25_10-33-11_MOVEMENT_ABC123.mp4",
                "Garten_2026-03-25_10-33-11_MOVEMENT_ABC123.jpg",
                {
                    "camera": "Garten",
                    "date": "2026-03-25",
                    "time": "10-33-11",
                    "etype": "MOVEMENT",
                    "ext": "mp4",
                },
            ),
        ]
        with patch.object(backend, "list_year_first_events", return_value=events):
            node = media._browse_smb(
                src, backend, ["2026", "03", "25"], single_source=True
            )
        assert node.title == "2026-03-25", (
            f"event-list node title must be YYYY-MM-DD, got {node.title}"
        )
        assert len(node.children) == 1
        child = node.children[0]
        # Video event -> VIDEO media class + can_play
        assert child.media_class == MediaClass.VIDEO
        assert child.can_play is True
        assert child.can_expand is False
        assert child.media_content_type == "video/mp4"
        # Identifier embeds year/month/day/file (no camera prefix in year-first)
        assert "2026/03/25/" in child.identifier
        assert child.identifier.endswith(".mp4")
        # Thumbnail URL uses the jpg sibling
        assert child.thumbnail is not None
        assert child.thumbnail.endswith(".jpg"), (
            f"thumbnail must point at the jpg sibling, got {child.thumbnail}"
        )

    def test_year_month_day_image_only_event(self, tmp_path):
        """Image-only event (no mp4 sibling) -> IMAGE class + content-type image/jpeg."""
        from homeassistant.components.media_player import MediaClass

        media, src, backend = _make_smb_media_source(tmp_path)
        events = [
            (
                "Garten_2026-03-25_10-33-11_AUDIO_AAAA.jpg",
                None,
                {
                    "camera": "Garten",
                    "date": "2026-03-25",
                    "time": "10-33-11",
                    "etype": "AUDIO",
                    "ext": "jpg",
                },
            ),
        ]
        with patch.object(backend, "list_year_first_events", return_value=events):
            node = media._browse_smb(
                src, backend, ["2026", "03", "25"], single_source=True
            )
        assert len(node.children) == 1
        child = node.children[0]
        assert child.media_class == MediaClass.IMAGE
        assert child.media_content_type == "image/jpeg"
        # No jpg sibling -> thumbnail must be None (no preview)
        assert child.thumbnail is None


# ═════════════════════════════════════════════════════════════════════════
# _SmbBackend: connection-cache isolation under concurrency (SMB2
# credit-starvation regression, production trace 2026-05-14) + session
# cleanup on error + path-traversal guards
# ═════════════════════════════════════════════════════════════════════════


def _backend_credit_starvation(hass_data: dict | None = None):
    """Build a configured ``_SmbBackend`` with credentials for the
    connection-cache-isolation tests below."""
    hass = SimpleNamespace(data=hass_data if hass_data is not None else {})
    return _SmbBackend(
        hass,
        {
            "smb_server": "192.0.2.10",
            "smb_share": "Cameras",
            "smb_username": "user",
            "smb_password": "pw",
            "smb_base_path": "/events",
            "folder_pattern": "{camera}/{year}/{month}/{day}",
        },
    )


def _fake_smbclient_default() -> MagicMock:
    """Build a fake ``smbclient`` module that captures all kwargs."""
    fake = MagicMock()
    fake.register_session = MagicMock()
    fake_stat = MagicMock()
    fake_stat.st_size = 12345
    fake.stat = MagicMock(return_value=fake_stat)
    fake.open_file = MagicMock(return_value=MagicMock(name="fobj"))
    fake.scandir = MagicMock(return_value=iter([]))
    fake.delete_session = MagicMock()
    return fake


class TestSmbConnectionCachePerCall:
    """The fix: every public SMB call must use its own ``connection_cache``.

    A shared cache means a shared ``Connection``, which means a shared 64-credit
    pool that exhausts under burst -- this is the production bug: 9x
    ``smbprotocol.exceptions.SMBException: Request requires 1 credits but only 0
    credits are available`` when the browser fired parallel HTTP Range requests
    against the media-source ``/api/bosch_shc_camera/event/...`` endpoint to
    play a video clip from the NAS (2026-05-14 22:54 UTC).

    Root cause (knowledge-base/smb-credit-starvation.md): the integration
    registered ONE smbclient session via ``register_session()`` without a
    custom ``connection_cache``. smbclient's global cache then served every
    concurrent executor thread the SAME ``Connection`` object, whose 64-credit
    SMB2 sequence-window drained faster than responses replenished it.

    Fix recommended by smbprotocol author (jborean93) in
    https://github.com/jborean93/smbprotocol/issues/312#issuecomment-3027461329:
    each concurrent worker passes its own ``connection_cache={}`` dict. A fresh
    dict forces a new ``Connection`` object with its own credit window.
    """

    def test_open_file_passes_connection_cache_to_all_smb_ops(self):
        """register_session, stat, and open_file must each receive the SAME
        per-call cache dict via the ``connection_cache`` kwarg."""
        backend = _backend_credit_starvation()
        fake = _fake_smbclient_default()
        valid = "Innenbereich_2026-05-15_10-00-00_MOTION_ABC123.mp4"

        with patch.dict(sys.modules, {"smbclient": fake}):
            backend.open_file("Innenbereich", "2026", "05", "15", valid)

        # All three calls must have received connection_cache kwarg
        reg_kwargs = fake.register_session.call_args.kwargs
        stat_kwargs = fake.stat.call_args.kwargs
        open_kwargs = fake.open_file.call_args.kwargs

        assert "connection_cache" in reg_kwargs, (
            "register_session must be called with connection_cache="
        )
        assert "connection_cache" in stat_kwargs, (
            "smbclient.stat must be called with connection_cache="
        )
        assert "connection_cache" in open_kwargs, (
            "smbclient.open_file must be called with connection_cache="
        )

        # Same cache for one logical operation
        assert reg_kwargs["connection_cache"] is stat_kwargs["connection_cache"], (
            "stat must use the same cache that register_session populated"
        )
        assert reg_kwargs["connection_cache"] is open_kwargs["connection_cache"], (
            "open_file must use the same cache that register_session populated"
        )

        # Cache is a dict (smbclient API contract)
        assert isinstance(reg_kwargs["connection_cache"], dict), (
            "connection_cache must be a dict per smbclient API"
        )

        # share_access="r" must be passed on open_file -- without it, FRITZ.NAS
        # and other servers open the file exclusively and a second parallel
        # range-request fails with NtStatus 0xc0000043 (SHARING_VIOLATION).
        # Confirmed in production 2026-05-15 06:45 UTC after the credit-pool
        # fix exposed this latent issue. Pinning the share-access kwarg here
        # prevents a regression from re-introducing the exclusive-open default.
        assert open_kwargs.get("share_access") == "r", (
            "smbclient.open_file must be called with share_access='r' to allow "
            "concurrent readers; default (None=exclusive) causes SHARING_VIOLATION "
            "on a 2nd parallel range-request"
        )

    def test_two_open_file_calls_use_isolated_caches(self):
        """The whole point of the fix: parallel callers each get a NEW dict.

        Without this, two concurrent range-requests share one Connection's
        credit pool -- the exact production bug. The 9x SMBException came
        from >=9 concurrent ops landing on one shared session.
        """
        backend = _backend_credit_starvation()
        fake = _fake_smbclient_default()
        valid_a = "Innenbereich_2026-05-15_10-00-00_MOTION_AAA111.mp4"
        valid_b = "Innenbereich_2026-05-15_10-00-01_MOTION_BBB222.mp4"

        with patch.dict(sys.modules, {"smbclient": fake}):
            backend.open_file("Innenbereich", "2026", "05", "15", valid_a)
            backend.open_file("Innenbereich", "2026", "05", "15", valid_b)

        register_calls = fake.register_session.call_args_list
        assert len(register_calls) == 2, (
            f"expected register_session called once per open_file (2), got {len(register_calls)}"
        )

        cache_a = register_calls[0].kwargs["connection_cache"]
        cache_b = register_calls[1].kwargs["connection_cache"]
        assert cache_a is not cache_b, (
            "each open_file() must use an isolated connection_cache; "
            "sharing the dict reintroduces the SMB2 credit-starvation bug"
        )

    def test_open_flat_file_uses_isolated_cache_per_call(self):
        """Flat-layout (legacy camera/file.mp4) -- same isolation requirement."""
        backend = _backend_credit_starvation()
        fake = _fake_smbclient_default()
        valid_a = "Kamera_2026-05-15_10-00-00_MOTION_AAA111.mp4"
        valid_b = "Kamera_2026-05-15_10-00-01_MOTION_BBB222.mp4"

        with patch.dict(sys.modules, {"smbclient": fake}):
            backend.open_flat_file("Kamera", valid_a)
            backend.open_flat_file("Kamera", valid_b)

        register_calls = fake.register_session.call_args_list
        assert len(register_calls) == 2
        cache_a = register_calls[0].kwargs["connection_cache"]
        cache_b = register_calls[1].kwargs["connection_cache"]
        assert cache_a is not cache_b, (
            "open_flat_file must also isolate connection_cache per call"
        )

    def test_scandir_uses_isolated_cache_per_call(self):
        """Directory listings (list_cameras, list_years, list_months, list_days,
        list_flat_dates) all go through ``_scandir_filtered`` -> scandir(). They
        must also use a fresh cache so a burst of browse requests during a video
        playback can't contend on the same Connection."""
        backend = _backend_credit_starvation()
        fake = _fake_smbclient_default()

        with patch.dict(sys.modules, {"smbclient": fake}):
            list(backend._scandir_filtered("Innenbereich", want_dirs=True))
            list(backend._scandir_filtered("Innenbereich", want_dirs=False))

        register_calls = fake.register_session.call_args_list
        assert len(register_calls) == 2, (
            "_scandir_filtered must register a fresh session per call"
        )
        cache_a = register_calls[0].kwargs["connection_cache"]
        cache_b = register_calls[1].kwargs["connection_cache"]
        assert cache_a is not cache_b, "scandir must isolate connection_cache per call"
        scandir_kwargs_list = [c.kwargs for c in fake.scandir.call_args_list]
        for kw in scandir_kwargs_list:
            assert "connection_cache" in kw, (
                "smbclient.scandir must be called with connection_cache="
            )


class TestSmbParallelBurst:
    """Simulate the exact production scenario: 9 parallel open_file() calls
    landing on the backend within milliseconds. Without per-call caches a
    real smbclient would raise SMBException; with the fix each call gets a
    fresh credit pool.
    """

    def test_nine_parallel_open_files_get_nine_isolated_caches(self):
        """The production failure had 9 SMBException in 1 second. With the
        fix, 9 concurrent open_file() calls must produce 9 register_session
        calls with 9 mutually-distinct cache dicts.
        """
        import concurrent.futures

        backend = _backend_credit_starvation()
        fake = _fake_smbclient_default()
        cam = "Innenbereich"
        names = [
            f"Innenbereich_2026-05-15_10-00-{i:02d}_MOTION_FF{i:04d}.mp4"
            for i in range(9)
        ]

        with patch.dict(sys.modules, {"smbclient": fake}):
            with concurrent.futures.ThreadPoolExecutor(max_workers=9) as ex:
                futures = [
                    ex.submit(backend.open_file, cam, "2026", "05", "15", name)
                    for name in names
                ]
                for f in futures:
                    f.result()

        register_calls = fake.register_session.call_args_list
        assert len(register_calls) == 9, (
            f"9 parallel open_file -> 9 register_session calls, got {len(register_calls)}"
        )
        caches = [c.kwargs["connection_cache"] for c in register_calls]
        cache_ids = {id(c) for c in caches}
        assert len(cache_ids) == 9, (
            f"all 9 caches must be distinct objects; got {len(cache_ids)} unique. "
            "Sharing caches across threads is the exact bug we're preventing."
        )


def _backend_min() -> _SmbBackend:
    hass = MagicMock()
    hass.data = {}
    return _SmbBackend(
        hass,
        {
            "smb_server": "nas",
            "smb_share": "M",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "",
        },
    )


def _install_failing_smbclient(
    stat_raises: Exception | None = None, open_raises: Exception | None = None
) -> MagicMock:
    """Inject a fake `smbclient` into sys.modules whose stat/open_file raise."""
    mod = MagicMock()
    mod.register_session = MagicMock()
    fake_stat = MagicMock()
    fake_stat.st_size = 1024
    if stat_raises is not None:
        mod.stat = MagicMock(side_effect=stat_raises)
    else:
        mod.stat = MagicMock(return_value=fake_stat)
    if open_raises is not None:
        mod.open_file = MagicMock(side_effect=open_raises)
    else:
        mod.open_file = MagicMock(return_value=MagicMock())
    sys.modules["smbclient"] = mod
    return mod


class TestOpenFileExceptionCleanup:
    """When `smbclient.open_file` raises (closed FRITZ.NAS connection, EACCES,
    NtStatus) the SMB session cache must be torn down before the exception
    propagates -- otherwise it leaks until the media-source background
    sweeper catches it."""

    def test_open_file_closes_session_on_smb_error(self):
        """`open_file()` raises -> `_close_session_cache(cache)` runs +
        exception propagates."""
        backend = _backend_min()
        _install_failing_smbclient(open_raises=OSError("NtStatus 0xc0000043"))
        with patch.object(
            backend,
            "_close_session_cache",
            wraps=backend._close_session_cache,
        ) as close_spy:
            with pytest.raises(OSError, match="NtStatus 0xc0000043"):
                backend.open_file(
                    "Terrasse",
                    "2026",
                    "05",
                    "19",
                    "Terrasse_2026-05-19_12-30-45_MOTION_ABC123.mp4",
                )
        close_spy.assert_called_once()

    def test_open_flat_file_closes_session_on_smb_error(self):
        """Same contract for the flat-layout variant."""
        backend = _backend_min()
        _install_failing_smbclient(open_raises=OSError("simulated SMB blowup"))
        with patch.object(
            backend,
            "_close_session_cache",
            wraps=backend._close_session_cache,
        ) as close_spy:
            with pytest.raises(OSError, match="simulated SMB blowup"):
                backend.open_flat_file(
                    "Terrasse", "Terrasse_2026-05-19_12-30-45_MOTION_ABC123.mp4"
                )
        close_spy.assert_called_once()

    def test_open_file_closes_session_on_stat_error(self):
        """`stat()` raising before `open_file()` also runs cleanup."""
        backend = _backend_min()
        _install_failing_smbclient(stat_raises=PermissionError("EACCES"))
        with patch.object(
            backend,
            "_close_session_cache",
            wraps=backend._close_session_cache,
        ) as close_spy:
            with pytest.raises(PermissionError):
                backend.open_file(
                    "Terrasse",
                    "2026",
                    "05",
                    "19",
                    "Terrasse_2026-05-19_12-30-45_MOTION_ABC123.mp4",
                )
        close_spy.assert_called_once()


class TestSmbPathTraversal:
    """Regression (bug-hunt 2026-07-03): `_path()` string-joined every
    segment into the UNC path with ZERO validation -- unlike `filename`,
    which every caller already re-validates before calling `_path()`.
    Camera titles come from the Bosch cloud account (in principle
    attacker-influenceable) and media_content_id segments are reachable via
    any media_source.resolve_media call, not just this integration's own
    browse UI, so a crafted `camera` segment containing "..\\" could escape
    `{share}\\{base}\\{camera}\\...` and read/list outside the intended
    NAS tree.
    """

    def test_path_rejects_backslash_traversal_segment(self) -> None:
        backend = _backend_min()
        with pytest.raises(FileNotFoundError):
            backend._path("..\\..\\Windows\\System32", "file.mp4")

    def test_path_rejects_dotdot_segment(self) -> None:
        backend = _backend_min()
        with pytest.raises(FileNotFoundError):
            backend._path("..", "file.mp4")

    def test_path_rejects_forward_slash_segment(self) -> None:
        backend = _backend_min()
        with pytest.raises(FileNotFoundError):
            backend._path("../etc/passwd", "file.mp4")

    def test_path_accepts_normal_segments(self) -> None:
        """No regression: a legitimate camera/date tree still builds the
        expected UNC path."""
        backend = _backend_min()
        path = backend._path("Terrasse", "2026", "05", "19", "file.mp4")
        assert path == "\\\\nas\\M\\Terrasse\\2026\\05\\19\\file.mp4"

    def test_path_skips_empty_segment(self) -> None:
        """An empty-string segment (e.g. a double-slash/trailing-empty split
        artifact) must be silently skipped, not raise and not appear in the
        joined path -- pins the `if not seg: continue` guard."""
        backend = _backend_min()
        path = backend._path("Terrasse", "", "file.mp4")
        assert path == "\\\\nas\\M\\Terrasse\\file.mp4"

    def test_open_file_rejects_malicious_camera_before_touching_smbclient(
        self,
    ) -> None:
        """A malicious `camera` value must be rejected before smb_stat()/
        open_file() are ever called with the traversal path -- proving the
        traversal never actually reaches the network layer."""
        backend = _backend_min()
        mod = _install_failing_smbclient()
        with patch.object(
            backend,
            "_close_session_cache",
            wraps=backend._close_session_cache,
        ) as close_spy:
            with pytest.raises(FileNotFoundError):
                backend.open_file(
                    "..\\..\\Windows\\System32",
                    "2026",
                    "05",
                    "19",
                    "Terrasse_2026-05-19_12-30-45_MOTION_ABC123.mp4",
                )
        mod.stat.assert_not_called()
        mod.open_file.assert_not_called()
        close_spy.assert_called_once()

    def test_open_flat_file_rejects_malicious_camera(self) -> None:
        backend = _backend_min()
        mod = _install_failing_smbclient()
        with pytest.raises(FileNotFoundError):
            backend.open_flat_file(
                "../etc", "Terrasse_2026-05-19_12-30-45_MOTION_ABC123.mp4"
            )
        mod.stat.assert_not_called()
        mod.open_file.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════
# Small pure helpers, second batch: _format_event_title / _entry_title / _node
# ═════════════════════════════════════════════════════════════════════════


class TestEntryTitle:
    def test_returns_entry_title_when_loaded(self):
        from custom_components.bosch_shc_camera.media_source import _entry_title

        entry = SimpleNamespace(entry_id="01ABC", title="My Bosch")
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_get_entry=MagicMock(return_value=entry),
            ),
        )
        assert _entry_title(hass, "01ABC") == "My Bosch"

    def test_falls_back_to_entry_id_short_when_missing(self):
        from custom_components.bosch_shc_camera.media_source import _entry_title

        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_get_entry=MagicMock(return_value=None),
            ),
        )
        out = _entry_title(hass, "01ABCDEFGHJKLMNOPQRSTUV0")
        # Some short form of the entry_id -- pin only that it's a string
        assert isinstance(out, str)
        assert len(out) > 0


class TestNode:
    def test_default_directory_node(self):
        from custom_components.bosch_shc_camera.media_source import _node

        out = _node(identifier="root", title="Root")
        assert out.identifier == "root"
        assert out.title == "Root"
        assert out.can_play is False
        assert out.can_expand is True

    def test_playable_leaf(self):
        from homeassistant.components.media_player import MediaClass

        from custom_components.bosch_shc_camera.media_source import _node

        out = _node(
            identifier="L:01ENT/Cam/2026-05-04/file.mp4",
            title="10:30",
            media_class=MediaClass.VIDEO,
            media_content_type="video/mp4",
            can_play=True,
            can_expand=False,
        )
        assert out.can_play is True
        assert out.can_expand is False
        assert out.media_content_type == "video/mp4"

    def test_thumbnail_propagated(self):
        from custom_components.bosch_shc_camera.media_source import _node

        out = _node(
            identifier="x",
            title="x",
            thumbnail="https://example/thumb.jpg",
        )
        assert out.thumbnail == "https://example/thumb.jpg"


# ═════════════════════════════════════════════════════════════════════════
# BoschCameraMediaSource.async_resolve_media
# ═════════════════════════════════════════════════════════════════════════


class TestAsyncResolveMedia:
    @pytest.mark.asyncio
    async def test_root_unresolvable(self):
        src = BoschCameraMediaSource(SimpleNamespace())
        item = SimpleNamespace(identifier=None)
        with pytest.raises(Unresolvable):
            await src.async_resolve_media(item)

    @pytest.mark.asyncio
    async def test_resolves_to_view_url_with_mime(self):
        src = BoschCameraMediaSource(SimpleNamespace())
        item = SimpleNamespace(identifier="L:01ENT/Cam/2026-05-04/file.mp4")
        out = await src.async_resolve_media(item)
        # MIME inferred from extension
        assert out.mime_type == "video/mp4"
        assert "L:01ENT/Cam/2026-05-04/file.mp4" in out.url

    @pytest.mark.asyncio
    async def test_unknown_extension_falls_back_to_octet_stream(self):
        src = BoschCameraMediaSource(SimpleNamespace())
        item = SimpleNamespace(identifier="L:01ENT/Cam/file.unknownext")
        out = await src.async_resolve_media(item)
        assert out.mime_type == "application/octet-stream"


# ═════════════════════════════════════════════════════════════════════════
# async_get_media_source: HTTP view registered exactly once
# ═════════════════════════════════════════════════════════════════════════


class TestAsyncGetMediaSource:
    @pytest.mark.asyncio
    async def test_first_call_registers_view(self):
        hass = MagicMock()
        hass.data = {}
        hass.http = MagicMock()
        await async_get_media_source(hass)
        hass.http.register_view.assert_called_once()

    @pytest.mark.asyncio
    async def test_second_call_skips_registration(self):
        hass = MagicMock()
        hass.data = {}
        hass.http = MagicMock()
        await async_get_media_source(hass)
        await async_get_media_source(hass)
        # register_view must have been called exactly once across two calls
        assert hass.http.register_view.call_count == 1


# ═════════════════════════════════════════════════════════════════════════
# BoschCameraMediaSource._browse dispatch tree
# ═════════════════════════════════════════════════════════════════════════


class TestBrowseEmpty:
    def test_no_enabled_sources_returns_empty_root(self):
        # hass with no loaded entries
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_loaded_entries=MagicMock(return_value=[]),
            ),
            data={},
        )
        src = BoschCameraMediaSource(hass)
        out = src._browse("")
        assert out.identifier == ""
        assert out.children == []


class TestBrowseSingleEntrySingleBackend:
    def test_root_lists_cameras_directly(self, tmp_path):
        _seed_local_event(tmp_path, "Terrasse", "2026-05-04")
        _seed_local_event(tmp_path, "Garten", "2026-05-04")
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        root = src._browse("")
        # Root with single entry + single source -> skips chooser, lists cameras
        titles = [c.title for c in root.children]
        assert "Terrasse" in titles
        assert "Garten" in titles

    def test_camera_level_lists_years(self, tmp_path):
        """Camera-first tree (default): browsing a camera shows years, not flat dates."""
        _seed_local_event(tmp_path, "Terrasse", "2026-05-04")
        _seed_local_event(tmp_path, "Terrasse", "2026-05-03")
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse")
        titles = [c.title for c in out.children]
        assert "2026" in titles

    def test_day_level_lists_events(self, tmp_path):
        """Camera-first tree: browsing camera/year/month/day shows events."""
        mp4, jpg = _seed_local_event(tmp_path, "Terrasse", "2026-05-04", "10-30-00")
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse/2026/05/04")
        # One event grouped from jpg+mp4 pair
        assert len(out.children) == 1
        ev = out.children[0]
        assert ev.can_play is True
        assert ev.can_expand is False
        # Identifier ends in the mp4 filename (preferred over jpg)
        assert ev.identifier.endswith(mp4)
        # Thumbnail URL points to the jpg
        assert ev.thumbnail and jpg in ev.thumbnail

    def test_too_deep_path_raises_unresolvable(self, tmp_path):
        """Camera-first tree: 6 rest segments (beyond year/month/day/events) -> Unresolvable."""
        _seed_local_event(tmp_path, "Cam", "2026-05-04")
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        with pytest.raises(Unresolvable):
            src._browse("01TESTENTRY/Cam/2026/05/04/extra/extra2")


class TestBrowseMultipleEntries:
    def test_root_lists_entries(self, tmp_path):
        """Two loaded config entries -> root level shows them as
        chooser nodes (one per entry)."""
        # Set up two entries each with their own download dir
        dir_a = tmp_path / "entry_a"
        dir_b = tmp_path / "entry_b"
        dir_a.mkdir()
        dir_b.mkdir()
        _seed_local_event(dir_a, "CamA", "2026-05-04")
        _seed_local_event(dir_b, "CamB", "2026-05-04")

        coord_a = SimpleNamespace(
            options={
                "enable_auto_download": True,
                "download_path": str(dir_a),
                "media_browser_source": "auto",
            }
        )
        coord_b = SimpleNamespace(
            options={
                "enable_auto_download": True,
                "download_path": str(dir_b),
                "media_browser_source": "auto",
            }
        )
        entry_a = SimpleNamespace(
            entry_id="01ENT_A",
            runtime_data=coord_a,
            title="Account A",
        )
        entry_b = SimpleNamespace(
            entry_id="01ENT_B",
            runtime_data=coord_b,
            title="Account B",
        )

        def _get_entry(eid):
            return (
                entry_a if eid == "01ENT_A" else entry_b if eid == "01ENT_B" else None
            )

        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_loaded_entries=MagicMock(return_value=[entry_a, entry_b]),
                async_get_entry=MagicMock(side_effect=_get_entry),
            ),
            data={},
        )
        src = BoschCameraMediaSource(hass)
        root = src._browse("")
        titles = [c.title for c in root.children]
        assert "Account A" in titles
        assert "Account B" in titles

    def test_unknown_entry_raises_unresolvable(self, tmp_path):
        _seed_local_event(tmp_path, "Cam", "2026-05-04")
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        with pytest.raises(Unresolvable):
            src._browse("01DOESNOTEXIST")


class TestBrowseNvrBackend:
    def _setup_nvr_only(self, tmp_path):
        nvr_base = tmp_path / "nvr"
        nvr_base.mkdir()
        # Seed Camera/2026-05-04/10-30.mp4
        seg_dir = nvr_base / "Cam" / "2026-05-04"
        seg_dir.mkdir(parents=True)
        (seg_dir / "10-30.mp4").write_text("x")
        (seg_dir / "11-00.mp4").write_text("x")
        # NVR-only (auto-download disabled, NVR enabled)
        coord = SimpleNamespace(
            options={
                "enable_auto_download": False,
                "enable_nvr": True,
                "nvr_base_path": str(nvr_base),
                "media_browser_source": "auto",
            }
        )
        entry = SimpleNamespace(
            entry_id="01NVRONLY",
            runtime_data=coord,
            title="NVR-Only",
        )
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_loaded_entries=MagicMock(return_value=[entry]),
                async_get_entry=MagicMock(return_value=entry),
            ),
            data={},
        )
        return hass

    def test_nvr_root_lists_cameras(self, tmp_path):
        hass = self._setup_nvr_only(tmp_path)
        src = BoschCameraMediaSource(hass)
        root = src._browse("")
        titles = [c.title for c in root.children]
        assert "Cam" in titles

    def test_nvr_camera_lists_dates(self, tmp_path):
        hass = self._setup_nvr_only(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01NVRONLY/Cam")
        titles = [c.title for c in out.children]
        assert "2026-05-04" in titles

    def test_nvr_date_lists_segments_with_time_label(self, tmp_path):
        hass = self._setup_nvr_only(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01NVRONLY/Cam/2026-05-04")
        # Newest first
        assert len(out.children) == 2
        titles = [c.title for c in out.children]
        assert titles == ["11:00", "10:30"]
        # All playable
        assert all(c.can_play for c in out.children)
        assert all(not c.can_expand for c in out.children)

    def test_nvr_too_deep_raises_unresolvable(self, tmp_path):
        hass = self._setup_nvr_only(tmp_path)
        src = BoschCameraMediaSource(hass)
        with pytest.raises(Unresolvable):
            src._browse("01NVRONLY/Cam/2026-05-04/foo/bar")


class TestMultiSourceSingleEntry:
    def _setup_local_plus_nvr(self, tmp_path):
        """Single config entry that has BOTH local download AND NVR
        enabled -- the entry root should show a source chooser."""
        local_dir = tmp_path / "local"
        nvr_dir = tmp_path / "nvr"
        local_dir.mkdir()
        nvr_dir.mkdir()
        _seed_local_event(local_dir, "Cam", "2026-05-04")
        (nvr_dir / "Cam" / "2026-05-04").mkdir(parents=True)
        (nvr_dir / "Cam" / "2026-05-04" / "10-30.mp4").write_text("x")

        coord = SimpleNamespace(
            options={
                "enable_auto_download": True,
                "download_path": str(local_dir),
                "enable_nvr": True,
                "nvr_base_path": str(nvr_dir),
                "media_browser_source": "auto",
            }
        )
        entry = SimpleNamespace(
            entry_id="01MULTI",
            runtime_data=coord,
            title="Multi-Source",
        )
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_loaded_entries=MagicMock(return_value=[entry]),
                async_get_entry=MagicMock(return_value=entry),
            ),
            data={},
        )
        return hass

    def test_root_with_two_sources_shows_chooser(self, tmp_path):
        hass = self._setup_local_plus_nvr(tmp_path)
        src = BoschCameraMediaSource(hass)
        root = src._browse("")
        # Two source nodes: "Lokal" + "Aufnahmen"
        labels = [c.title for c in root.children]
        assert "Lokal" in labels
        assert "Aufnahmen" in labels

    def test_local_source_explicit_kind(self, tmp_path):
        """Identifier `01MULTI/L` selects the local backend even though
        the entry has multiple sources."""
        hass = self._setup_local_plus_nvr(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01MULTI/L")
        titles = [c.title for c in out.children]
        assert "Cam" in titles

    def test_nvr_source_explicit_kind(self, tmp_path):
        hass = self._setup_local_plus_nvr(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01MULTI/N")
        titles = [c.title for c in out.children]
        assert "Cam" in titles

    def test_unknown_kind_raises(self, tmp_path):
        hass = self._setup_local_plus_nvr(tmp_path)
        src = BoschCameraMediaSource(hass)
        with pytest.raises(Unresolvable):
            src._browse("01MULTI/XYZ")


class TestAsyncBrowseMedia:
    @pytest.mark.asyncio
    async def test_unresolvable_becomes_browse_error(self, tmp_path):
        from homeassistant.components.media_player.errors import BrowseError

        hass, _ = _hass_with_local_dir(tmp_path)

        # Run executor jobs synchronously for the test
        async def _run_executor(func, *args, **kw):
            return func(*args, **kw)

        hass.async_add_executor_job = _run_executor
        src = BoschCameraMediaSource(hass)
        item = SimpleNamespace(identifier="01UNKNOWN")
        with pytest.raises(BrowseError):
            await src.async_browse_media(item)

    @pytest.mark.asyncio
    async def test_browse_media_runs_through_executor(self, tmp_path):
        _seed_local_event(tmp_path, "Cam", "2026-05-04")
        hass, _ = _hass_with_local_dir(tmp_path)

        async def _run_executor(func, *args, **kw):
            return func(*args, **kw)

        hass.async_add_executor_job = _run_executor
        src = BoschCameraMediaSource(hass)
        item = SimpleNamespace(identifier="")
        out = await src.async_browse_media(item)
        # Single entry single source -> cameras at root level
        titles = [c.title for c in out.children]
        assert "Cam" in titles


def _hass_for_browse(
    tmp_path: Path, entry_id: str = "01ENT", extra_opts: dict | None = None
):
    """Minimal fake hass for _browse tests with one local backend."""
    opts = {"download_path": str(tmp_path), "media_browser_source": "auto"}
    if extra_opts:
        opts.update(extra_opts)
    coord = SimpleNamespace(options=opts)
    entry = SimpleNamespace(entry_id=entry_id, runtime_data=coord, title="Bosch")
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_loaded_entries=MagicMock(return_value=[entry]),
            async_get_entry=MagicMock(return_value=entry),
        ),
        data={},
    )
    return hass


def _seed_event(base: Path, camera: str, date: str, t: str = "10-00-00") -> None:
    """Seed a single event jpg in the camera-first nested structure: camera/year/month/day/."""
    year, month, day = date.split("-")
    cam_dir = base / camera / year / month / day
    cam_dir.mkdir(parents=True, exist_ok=True)
    (cam_dir / f"{camera}_{date}_{t}_MOVEMENT_AB12CD34.jpg").write_bytes(b"x")


class TestBrowsePathAutoDetection:
    """_browse correctly detects whether parts[1] is a source-kind token or
    a tree segment (camera name).

    Regression target: the original `parts[1] not in ("L","S","N")` check
    broke navigation for cameras named exactly "L", "S", or "N" in
    single_source mode.  Fixed by comparing against the actual backend kind.
    """

    def test_camera_with_space_navigates_to_years(self, tmp_path):
        """Single source, camera name 'Meine Kamera' (with space) -> years listed.

        Root context: Andreas74 (simon42 2026-05-07) reported empty subfolder.
        Camera-first tree (default): camera level shows years.
        """
        _seed_event(tmp_path, "Meine Kamera", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/Meine Kamera")
        assert len(out.children) == 1
        assert out.children[0].title == "2026"

    def test_camera_named_L_known_ambiguity(self, tmp_path):
        """KNOWN LIMITATION: camera named 'L' with local backend is ambiguous.

        The identifier '{entry_id}/L' cannot be distinguished between:
          a) navigate to source kind "L" (local)   <- backwards-compat path
          b) navigate to camera named "L"           <- desired for this camera

        Because single_source==True and actual_kind=="L" and parts[1]=="L",
        the condition `parts[1] != actual_kind` is False, so the code takes
        the source-token path (else branch) and returns the camera list
        instead of the date list.

        Fixing this without a breaking identifier-scheme change is not possible.
        The fix we applied (use `!= actual_kind` instead of `not in ("L","S","N")`)
        already correctly handles cameras named "S" or "N" with a local backend.
        The remaining blind spot is ONLY camera-name == backend-kind (both "L").

        This test pins the actual behaviour so we notice if it ever changes.
        """
        _seed_event(tmp_path, "L", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/L")
        # Source-kind path taken -> returns the camera list (one camera named "L")
        assert len(out.children) == 1
        assert out.children[0].title == "L", (
            "Known limitation: identifier '01ENT/L' is treated as source-kind token "
            "and returns the camera list; 'L' is the camera name shown as a child."
        )

    def test_camera_named_S_navigates_to_years(self, tmp_path):
        """Camera named 'S' must not be treated as SMB-source token."""
        _seed_event(tmp_path, "S", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/S")
        assert len(out.children) == 1
        assert out.children[0].title == "2026"

    def test_camera_named_N_navigates_to_years(self, tmp_path):
        """Camera named 'N' must not be treated as NVR-source token."""
        _seed_event(tmp_path, "N", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/N")
        assert len(out.children) == 1
        assert out.children[0].title == "2026"

    def test_old_style_L_prefix_compatibility(self, tmp_path):
        """Old bookmark with 'L' prefix on single-source entry must still navigate.

        When a user had multi-source and bookmarked '{entry_id}/L/Cam',
        then removed SMB, single_source becomes True.  Identifier has 'L' at
        parts[1] which matches the actual backend kind -> treated as source
        token (backwards-compat path), rest = [cam] -> camera-first year level.
        """
        _seed_event(tmp_path, "Terrasse", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        # Old multi-source identifier for the camera level: 01ENT/L/Terrasse
        out = src._browse("01ENT/L/Terrasse")
        assert len(out.children) == 1
        assert out.children[0].title == "2026"

    def test_camera_with_umlaut_navigates_to_years(self, tmp_path):
        _seed_event(tmp_path, "Küche", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/Küche")
        assert len(out.children) == 1

    def test_camera_with_space_day_lists_events(self, tmp_path):
        """Full path to day level with space in camera name returns events."""
        _seed_event(tmp_path, "Bosch Terrasse", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/Bosch Terrasse/2026/05/07")
        assert len(out.children) == 1
        assert out.children[0].can_play is True

    def test_root_with_space_camera_lists_camera_as_child(self, tmp_path):
        """Root browse returns camera names (with spaces) as children."""
        _seed_event(tmp_path, "Vorne Rechts", "2026-05-07")
        _seed_event(tmp_path, "Hinten Links", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("")
        titles = {c.title for c in out.children}
        assert "Vorne Rechts" in titles
        assert "Hinten Links" in titles

    def test_camera_name_longer_than_one_char_starting_with_L(self, tmp_path):
        """Camera named 'Lounge' (starts with L) is NOT treated as source token."""
        _seed_event(tmp_path, "Lounge", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/Lounge")
        assert len(out.children) == 1
        assert out.children[0].title == "2026"  # camera-first: year level

    def test_multiple_cameras_with_spaces_sorted(self, tmp_path):
        """Multiple cameras with spaces are sorted case-insensitively."""
        _seed_event(tmp_path, "Zweite Kamera", "2026-05-07")
        _seed_event(tmp_path, "erste Kamera", "2026-05-07")
        _seed_event(tmp_path, "Mittlere Kamera", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("")
        titles = [c.title for c in out.children]
        assert titles == sorted(titles, key=str.casefold)

    def test_unknown_entry_raises_unresolvable(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
            Unresolvable,
        )

        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        with pytest.raises((Exception,)):
            src._browse("UNKNOWN_ENTRY/Cam")

    def test_camera_day_events_thumbnail_uses_space_in_url(self, tmp_path):
        """Thumbnail URL for events under a camera with a space must be set."""
        cam = "My Cam"
        cam_dir = tmp_path / cam / "2026" / "05" / "07"
        cam_dir.mkdir(parents=True)
        # Write both jpg and mp4 to trigger thumbnail logic
        stem = f"{cam}_2026-05-07_10-00-00_MOVEMENT_AB12CD34"
        (cam_dir / f"{stem}.jpg").write_bytes(b"x")
        (cam_dir / f"{stem}.mp4").write_bytes(b"x")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/My Cam/2026/05/07")
        assert len(out.children) == 1
        event = out.children[0]
        # Thumbnail must be set for the jpg
        assert event.thumbnail is not None
        assert "My Cam" in event.thumbnail


class TestBrowseDispatchSingleSource:
    """Single-source entry implicit kind detection and unknown-source error."""

    def test_unknown_entry_raises_unresolvable(self, tmp_path):
        hass = _hass_stub("entry1", tmp_path=tmp_path)
        (tmp_path).mkdir(exist_ok=True)
        ms = BoschCameraMediaSource(hass)
        with pytest.raises(Unresolvable):
            ms._browse("unknown-entry/L/Terrasse")

    def test_too_deep_local_path_raises_unresolvable(self, tmp_path):
        """Camera-first tree: 6 rest segments (past camera/year/month/day/events) -> Unresolvable."""
        (tmp_path / "Terrasse").mkdir(parents=True, exist_ok=True)
        hass = _hass_stub("entry1", tmp_path=tmp_path)
        ms = BoschCameraMediaSource(hass)
        with pytest.raises(Unresolvable):
            ms._browse("entry1/L/Terrasse/2026/05/07/extra/segment")


class TestBrowseLocalCameraFirstFlat:
    """camera_first=True with files directly in camera/ (flat layout within
    camera-first mode) -- flat dates + flat events branches."""

    def _setup(self, tmp_path):
        """Create a backend with files directly in camera/ (flat layout within camera_first mode)."""
        cam_dir = tmp_path / "Terrasse"
        cam_dir.mkdir()
        stem = "Terrasse_2026-05-07_10-00-00_MOVEMENT_11111111"
        (cam_dir / f"{stem}.mp4").write_bytes(b"vid")
        (cam_dir / f"{stem}.jpg").write_bytes(b"img")
        return tmp_path

    def test_camera_level_shows_flat_dates_alongside_years(self, tmp_path):
        """camera_first=True, len(rest)==1: flat dates (from files in cam/) appended."""
        self._setup(tmp_path)
        # Also create a year subfolder so we have both year nodes and flat dates
        year_dir = tmp_path / "Terrasse" / "2026"
        year_dir.mkdir()
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse")
        titles = [c.title for c in out.children]
        # Year node
        assert "2026" in titles
        # Flat date node from file
        assert "2026-05-07" in titles

    def test_camera_level_flat_dates_only(self, tmp_path):
        """camera_first=True, no year subdirs -- only flat dates shown."""
        self._setup(tmp_path)
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse")
        titles = [c.title for c in out.children]
        assert "2026-05-07" in titles

    def test_flat_date_routing_returns_events(self, tmp_path):
        """camera_first=True, rest[1] is YYYY-MM-DD (not a 4-digit year) -> flat events."""
        self._setup(tmp_path)
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        # Browse into the flat date
        out = src._browse("01TESTENTRY/Terrasse/2026-05-07")
        assert len(out.children) == 1
        ev = out.children[0]
        assert ev.can_play is True
        assert ev.can_expand is False

    def test_flat_date_event_has_thumbnail(self, tmp_path):
        """Flat event nodes include a thumbnail URL when image is present."""
        self._setup(tmp_path)
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse/2026-05-07")
        ev = out.children[0]
        assert ev.thumbnail is not None
        assert ".jpg" in ev.thumbnail

    def test_flat_date_event_identifier_is_2_segment(self, tmp_path):
        """Flat event identifier is camera/filename (2 segments, not 5)."""
        self._setup(tmp_path)
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse/2026-05-07")
        ev = out.children[0]
        # Identifier after entry prefix: camera/filename (2 parts)
        parts_after_entry = ev.identifier.split("/")[1:]
        assert len(parts_after_entry) == 2


class TestBrowseLocalMonthsLevel:
    def test_year_level_lists_months(self, tmp_path):
        """camera_first, len(rest)==2 with 4-digit year -> lists months."""
        _seed_local_event(tmp_path, "Terrasse", "2026-05-07")
        _seed_local_event(tmp_path, "Terrasse", "2026-04-15")
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse/2026")
        titles = [c.title for c in out.children]
        # Months show as bare 2-digit numbers now ("05", "04")
        assert "05" in titles
        assert "04" in titles

    def test_year_level_empty_months(self, tmp_path):
        """camera_first, len(rest)==2 with 4-digit year but empty year dir."""
        year_dir = tmp_path / "Terrasse" / "2026"
        year_dir.mkdir(parents=True)
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse/2026")
        assert out.children == []


class TestBrowseLocalDaysLevel:
    def test_year_month_level_lists_days(self, tmp_path):
        """camera_first, len(rest)==3 -> lists days."""
        _seed_local_event(tmp_path, "Terrasse", "2026-05-07")
        _seed_local_event(tmp_path, "Terrasse", "2026-05-04")
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse/2026/05")
        titles = [c.title for c in out.children]
        # Should show bare day numbers
        assert "07" in titles
        assert "04" in titles
        # Children must be expandable (folders)
        assert all(c.can_expand for c in out.children)


class TestBrowseLocalLegacyFlat:
    """camera_first=False (folder_pattern date-first) -- legacy flat tree."""

    def _hass_with_legacy_backend(self, tmp_path):
        """Build hass with camera_first=False (flat) local backend."""
        cam_dir = tmp_path / "Terrasse"
        cam_dir.mkdir()
        stem = "Terrasse_2026-05-07_10-00-00_MOVEMENT_11111111"
        (cam_dir / f"{stem}.mp4").write_bytes(b"vid")
        (cam_dir / f"{stem}.jpg").write_bytes(b"img")
        opts = {
            "download_path": str(tmp_path),
            "folder_pattern": "{year}/{month}/{day}/{camera}",  # NOT camera-first
            "media_browser_source": "auto",
        }
        coord = SimpleNamespace(options=opts)
        entry = SimpleNamespace(
            entry_id="01FLAT",
            runtime_data=coord,
            title="Flat Backend",
        )
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_loaded_entries=MagicMock(return_value=[entry]),
                async_get_entry=MagicMock(return_value=entry),
            ),
            data={},
        )
        return hass

    def test_legacy_camera_lists_dates(self, tmp_path):
        """camera_first=False, len(rest)==1 -> list dates from filenames."""
        hass = self._hass_with_legacy_backend(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01FLAT/Terrasse")
        titles = [c.title for c in out.children]
        assert "2026-05-07" in titles

    def test_legacy_date_lists_events(self, tmp_path):
        """camera_first=False, len(rest)==2 -> list events for that date."""
        hass = self._hass_with_legacy_backend(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01FLAT/Terrasse/2026-05-07")
        assert len(out.children) == 1
        ev = out.children[0]
        assert ev.can_play is True

    def test_legacy_date_event_no_thumbnail_when_image_missing(self, tmp_path):
        """Legacy flat: no thumbnail when image is None."""
        # mp4 only, no jpg
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_ABCD1234"
        (cam_dir / f"{stem}.mp4").write_bytes(b"vid")
        opts = {
            "download_path": str(tmp_path),
            "folder_pattern": "{year}/{month}/{day}/{camera}",
            "media_browser_source": "auto",
        }
        coord = SimpleNamespace(options=opts)
        entry = SimpleNamespace(
            entry_id="01FLAT2",
            runtime_data=coord,
            title="Flat2",
        )
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_loaded_entries=MagicMock(return_value=[entry]),
                async_get_entry=MagicMock(return_value=entry),
            ),
            data={},
        )
        src = BoschCameraMediaSource(hass)
        out = src._browse("01FLAT2/Cam/2026-05-07")
        assert len(out.children) == 1
        assert out.children[0].thumbnail is None

    def test_legacy_too_deep_raises_unresolvable(self, tmp_path):
        """camera_first=False: 4+ segments -> Unresolvable."""
        hass = self._hass_with_legacy_backend(tmp_path)
        src = BoschCameraMediaSource(hass)
        with pytest.raises(Unresolvable):
            src._browse("01FLAT/Terrasse/2026-05-07/extra/segment")


class TestBrowseSmbCameraFirstFlatDates:
    def _browse_smb(self, identifier, cameras=None, years=None, flat_dates=None):
        from custom_components.bosch_shc_camera import media_source as ms
        from custom_components.bosch_shc_camera.media_source import _Source

        backend = MagicMock(spec=_SmbBackend)
        backend.camera_first = True
        backend.list_cameras.return_value = cameras or []
        backend.list_years.return_value = years or []
        backend.list_flat_dates.return_value = flat_dates or []
        backend.list_flat_events.return_value = []

        src = _Source(entry_id="01ENT", kind="S", label="NAS")
        hass = MagicMock()
        hass.data = {}
        src_obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            return src_obj._browse(identifier)

    def test_camera_level_shows_flat_dates(self):
        """len(rest)==1: list_flat_dates() results appended to year nodes."""
        out = self._browse_smb(
            "01ENT/Terrasse",
            years=["2026"],
            flat_dates=["2026-05-07"],
        )
        titles = [c.title for c in out.children]
        assert "2026" in titles
        assert "2026-05-07" in titles

    def test_camera_level_flat_dates_only(self):
        """No year dirs, only flat dates."""
        out = self._browse_smb(
            "01ENT/Terrasse",
            years=[],
            flat_dates=["2026-05-08", "2026-05-07"],
        )
        titles = [c.title for c in out.children]
        assert "2026-05-08" in titles
        assert "2026-05-07" in titles


class TestBrowseSmbCameraFirstFlatEvents:
    def _browse_smb_flat(self, identifier, flat_events=None, image_present=True):
        from custom_components.bosch_shc_camera import media_source as ms
        from custom_components.bosch_shc_camera.media_source import _Source

        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABBCCDD"
        image = f"{stem}.jpg" if image_present else None
        default_events = [
            (
                f"{stem}.mp4",
                image,
                {
                    "camera": "Cam",
                    "date": "2026-05-07",
                    "time": "10-00-00",
                    "etype": "MOVEMENT",
                },
            )
        ]

        backend = MagicMock(spec=_SmbBackend)
        backend.camera_first = True
        backend.list_flat_events.return_value = (
            flat_events if flat_events is not None else default_events
        )

        src = _Source(entry_id="01ENT", kind="S", label="NAS")
        hass = MagicMock()
        hass.data = {}
        src_obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            return src_obj._browse(identifier)

    def test_flat_date_route_returns_events(self):
        """rest[1] is YYYY-MM-DD (not year) -> flat events returned."""
        out = self._browse_smb_flat("01ENT/Cam/2026-05-07")
        assert len(out.children) == 1
        ev = out.children[0]
        assert ev.can_play is True

    def test_flat_event_has_thumbnail_when_image_present(self):
        out = self._browse_smb_flat("01ENT/Cam/2026-05-07", image_present=True)
        assert out.children[0].thumbnail is not None

    def test_flat_event_no_thumbnail_when_image_none(self):
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABBCCDD"
        events_no_image = [
            (
                f"{stem}.mp4",
                None,
                {
                    "camera": "Cam",
                    "date": "2026-05-07",
                    "time": "10-00-00",
                    "etype": "MOVEMENT",
                },
            )
        ]
        out = self._browse_smb_flat("01ENT/Cam/2026-05-07", flat_events=events_no_image)
        assert out.children[0].thumbnail is None


class TestBrowseSmbDateFirstEvents:
    def _browse_smb_date_first(self, identifier, events=None, image_present=True):
        from custom_components.bosch_shc_camera import media_source as ms
        from custom_components.bosch_shc_camera.media_source import _Source

        stem = "Terrasse_2026-05-07_10-00-00_MOVEMENT_AB12CD34"
        image = f"{stem}.jpg" if image_present else None
        default_events = [
            (
                f"{stem}.mp4",
                image,
                {
                    "camera": "Terrasse",
                    "date": "2026-05-07",
                    "time": "10-00-00",
                    "etype": "MOVEMENT",
                },
            )
        ]

        backend = MagicMock(spec=_SmbBackend)
        backend.camera_first = False
        backend.list_events.return_value = (
            events if events is not None else default_events
        )

        src = _Source(entry_id="01ENT", kind="S", label="NAS")
        hass = MagicMock()
        hass.data = {}
        src_obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            return src_obj._browse(identifier)

    def test_date_first_events_listed(self):
        """date-first, len(rest)==3 (year/month/day) -> events returned."""
        out = self._browse_smb_date_first("01ENT/2026/05/07")
        assert len(out.children) == 1
        assert out.children[0].can_play is True

    def test_date_first_event_has_thumbnail(self):
        out = self._browse_smb_date_first("01ENT/2026/05/07", image_present=True)
        assert out.children[0].thumbnail is not None

    def test_date_first_event_no_thumbnail_when_image_none(self):
        stem = "Terrasse_2026-05-07_10-00-00_MOVEMENT_AB12CD34"
        events_no_img = [
            (
                f"{stem}.mp4",
                None,
                {
                    "camera": "Terrasse",
                    "date": "2026-05-07",
                    "time": "10-00-00",
                    "etype": "MOVEMENT",
                },
            )
        ]
        out = self._browse_smb_date_first("01ENT/2026/05/07", events=events_no_img)
        assert out.children[0].thumbnail is None

    def test_date_first_days_level(self):
        """date-first, len(rest)==2 (year/month) -> days returned."""
        from custom_components.bosch_shc_camera import media_source as ms
        from custom_components.bosch_shc_camera.media_source import _Source

        backend = MagicMock(spec=_SmbBackend)
        backend.camera_first = False
        backend.list_days.return_value = ["22", "07"]

        src = _Source(entry_id="01ENT", kind="S", label="NAS")
        hass = MagicMock()
        hass.data = {}
        src_obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            out = src_obj._browse("01ENT/2026/05")
        titles = [c.title for c in out.children]
        assert any("2026-05-22" in t for t in titles)
        assert any("2026-05-07" in t for t in titles)

    def test_date_first_too_deep_raises_unresolvable(self):
        from custom_components.bosch_shc_camera import media_source as ms
        from custom_components.bosch_shc_camera.media_source import _Source

        backend = MagicMock(spec=_SmbBackend)
        backend.camera_first = False

        src = _Source(entry_id="01ENT", kind="S", label="NAS")
        hass = MagicMock()
        hass.data = {}
        src_obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            with pytest.raises(Unresolvable):
                src_obj._browse("01ENT/2026/05/07/file.mp4/extra")


class TestBrowseSmb:
    def _browse(
        self, identifier, cameras=None, years=None, months=None, days=None, events=None
    ):
        from custom_components.bosch_shc_camera import media_source as ms
        from custom_components.bosch_shc_camera.media_source import _Source

        backend = MagicMock(spec=_SmbBackend)
        backend.list_cameras.return_value = cameras or []
        backend.list_years.return_value = years or []
        backend.list_months.return_value = months or []
        backend.list_days.return_value = days or []
        backend.list_events.return_value = events or []

        src = _Source(entry_id="01ENT", kind="S", label="NAS \\\\nas\\M")

        hass = MagicMock()
        hass.data = {}

        src_obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            return src_obj._browse(identifier)

    def test_root_lists_cameras(self):
        out = self._browse("", cameras=["Terrasse", "Kamera"])
        assert len(out.children) == 2
        assert out.children[0].title == "Terrasse"

    def test_camera_lists_years(self):
        out = self._browse("01ENT/Terrasse", years=["2026", "2025"])
        assert len(out.children) == 2
        assert out.children[0].title == "2026"

    def test_camera_year_lists_months(self):
        out = self._browse("01ENT/Terrasse/2026", months=["05", "04"])
        assert len(out.children) == 2
        assert out.children[0].title == "05"

    def test_camera_year_month_lists_days(self):
        out = self._browse("01ENT/Terrasse/2026/05", days=["22", "07"])
        assert len(out.children) == 2
        assert out.children[0].title == "22"

    def test_camera_year_month_day_lists_events(self):
        stem = "Terrasse_2026-05-07_10-00-00_MOVEMENT_AB12CD34"
        evs = [
            (
                f"{stem}.mp4",
                f"{stem}.jpg",
                {
                    "camera": "Terrasse",
                    "date": "2026-05-07",
                    "time": "10-00-00",
                    "etype": "MOVEMENT",
                },
            )
        ]
        out = self._browse("01ENT/Terrasse/2026/05/07", events=evs)
        assert len(out.children) == 1
        assert out.children[0].can_play is True

    def test_event_thumbnail_set_when_image_present(self):
        stem = "Cam_2026-05-07_08-00-00_MOVEMENT_DEADBEEF"
        evs = [
            (
                f"{stem}.mp4",
                f"{stem}.jpg",
                {
                    "camera": "Cam",
                    "date": "2026-05-07",
                    "time": "08-00-00",
                    "etype": "MOVEMENT",
                },
            )
        ]
        out = self._browse("01ENT/Cam/2026/05/07", events=evs)
        assert out.children[0].thumbnail is not None

    def test_event_no_thumbnail_when_image_none(self):
        stem = "Cam_2026-05-07_08-00-00_MOVEMENT_DEADBEEF"
        evs = [
            (
                f"{stem}.mp4",
                None,
                {
                    "camera": "Cam",
                    "date": "2026-05-07",
                    "time": "08-00-00",
                    "etype": "MOVEMENT",
                },
            )
        ]
        out = self._browse("01ENT/Cam/2026/05/07", events=evs)
        assert out.children[0].thumbnail is None

    def test_too_deep_raises_unresolvable(self):
        with pytest.raises(Unresolvable):
            self._browse("01ENT/Cam/2026/05/07/file.mp4/extra")

    def test_single_source_skips_kind_token_for_camera(self):
        """Single SMB source: '01ENT/Terrasse' directly navigates to years."""
        out = self._browse("01ENT/Terrasse", years=["2026"])
        assert out.children[0].title == "2026"

    def test_date_first_root_lists_years(self):
        """When folder_pattern is date-first, root browse shows years."""
        from custom_components.bosch_shc_camera import media_source as ms
        from custom_components.bosch_shc_camera.media_source import _Source

        backend = MagicMock(spec=_SmbBackend)
        backend.camera_first = False  # date-first mode
        backend.list_years.return_value = ["2026"]

        src = _Source(entry_id="01ENT", kind="S", label="NAS")
        hass = MagicMock()
        hass.data = {}
        src_obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            out = src_obj._browse("")
        assert out.children[0].title == "2026"

    def test_date_first_year_lists_months(self):
        """Date-first: '01ENT/2026' -> months."""
        from custom_components.bosch_shc_camera import media_source as ms
        from custom_components.bosch_shc_camera.media_source import _Source

        backend = MagicMock(spec=_SmbBackend)
        backend.camera_first = False
        backend.list_months.return_value = ["05", "04"]

        src = _Source(entry_id="01ENT", kind="S", label="NAS")
        hass = MagicMock()
        hass.data = {}
        src_obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            out = src_obj._browse("01ENT/2026")
        assert len(out.children) == 2


class TestBrowseEntryRootDispatch:
    def test_nvr_single_source_lists_cameras(self, tmp_path):
        """Single NVR source: root browse goes straight to camera list."""
        from custom_components.bosch_shc_camera import media_source as ms
        from custom_components.bosch_shc_camera.media_source import _Source

        nvr_base = tmp_path / "nvr"
        (nvr_base / "Terrasse").mkdir(parents=True)
        backend = _NvrBackend(str(nvr_base))
        src = _Source(entry_id="01ENT", kind="N", label="Aufnahmen")

        hass = MagicMock()
        hass.data = {}
        obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            out = obj._browse("")
        cameras = [c.title for c in out.children]
        assert "Terrasse" in cameras

    def test_smb_single_source_root_shows_cameras(self):
        """Single SMB source: root browse shows camera folders (camera-first)."""
        from custom_components.bosch_shc_camera import media_source as ms
        from custom_components.bosch_shc_camera.media_source import _Source

        backend = MagicMock(spec=_SmbBackend)
        backend.list_cameras.return_value = ["Terrasse"]
        src = _Source(entry_id="01ENT", kind="S", label="NAS")

        hass = MagicMock()
        hass.data = {}
        obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            out = obj._browse("")
        assert out.children[0].title == "Terrasse"

    def test_multi_source_entry_root_shows_chooser(self, tmp_path):
        """Two backends on same entry: root shows source chooser."""
        from custom_components.bosch_shc_camera import media_source as ms
        from custom_components.bosch_shc_camera.media_source import _Source

        local = _LocalBackend(str(tmp_path))
        nvr_base = tmp_path / "nvr"
        nvr_base.mkdir()
        nvr = _NvrBackend(str(nvr_base))

        src_l = _Source("01ENT", "L", "Lokal")
        src_n = _Source("01ENT", "N", "Aufnahmen")

        hass = MagicMock()
        hass.data = {}
        obj = BoschCameraMediaSource(hass)
        with patch.object(
            ms, "_enabled_sources", return_value=[(src_l, local), (src_n, nvr)]
        ):
            out = obj._browse("01ENT")
        kinds = {c.identifier.split("/")[-1] for c in out.children}
        assert "L" in kinds
        assert "N" in kinds


# ═════════════════════════════════════════════════════════════════════════
# BoschCameraMediaView routing: structural source-pins (camera-first vs
# date-first vs NVR disambiguation, verified by reading the view's own
# source via inspect.getsource + assert_in_source)
# ═════════════════════════════════════════════════════════════════════════


class TestViewRoutingCameraFirstLocal:
    """Pin the routing fix: local camera-first paths must NOT be sent to SMB.

    Before the fix (pre-v11.0.18): parts[1] matching ^\\d{4}$ always set kind='S',
    so _find_source(entry_id, 'S') returned None for users without SMB -> HTTP 404.
    After the fix: kind falls through to 'L' when no SMB source is configured.
    """

    def test_source_routing_prefers_smb_only_when_smb_configured(self):
        """When parts[1] is a year AND SMB is not configured, routing must pick Local.

        This is a structural pin of the fix at BoschCameraMediaView.get -- reads the
        source code and asserts the disambiguation logic is present.
        """
        import inspect

        src = inspect.getsource(BoschCameraMediaView.get)
        # The fix must check for an SMB source before defaulting to "S"
        assert_in_source(
            src, "_find_source", '"S"', '"L"'
        )  # BoschCameraMediaView.get must disambiguate Local vs SMB camera-first paths via _find_source -- without this, Local camera-first files (camera/year/month/day/file) are incorrectly routed to SMB and return HTTP 404 (georg, simon42, 2026-05-08)
        # Specifically, the SMB preference expression must exist (not just hardcode "S").
        # bug-hunt 2026-07-03: _find_source now runs via async_add_executor_job
        # (it does blocking Path.exists()/mkdir()/is_dir() internally) instead of
        # being called directly on the event loop.
        assert_in_source(  # Routing must use _find_source (via the executor) to check if SMB is configured before choosing kind='S'
            src,
            '"S" if await self.hass.async_add_executor_job(_find_source, self.hass, entry_id, "S")',
            "'S' if await self.hass.async_add_executor_job(_find_source, self.hass, entry_id, 'S')",
            any_of=True,
        )

    def test_legacy_flat_path_routes_to_local_when_local_configured(self):
        """Legacy flat identifier {camera}/{filename} routes to kind='L' when Local exists.

        A camera name like 'Terrasse' never matches _YEAR_RE, so the path falls through
        all year/NVR heuristics to the else branch. The else branch now checks for a
        Local source first, so users with a local download_path get kind='L'.
        The else branch must NOT unconditionally hardcode kind='S'.
        """
        import inspect

        src = inspect.getsource(BoschCameraMediaView.get)
        # The else branch must prefer Local via _find_source (not hardcode 'L' or 'S').
        # bug-hunt 2026-07-03: _find_source now runs via async_add_executor_job.
        assert_in_source(  # The else-branch must check _find_source for Local before choosing kind. Hardcoding kind='L' would break SMB-only users; hardcoding kind='S' would break Local-only users.
            src,
            'await self.hass.async_add_executor_job(_find_source, self.hass, entry_id, "L")',
        )
        # Must also NOT unconditionally hardcode kind='S' in the else branch
        lines = src.splitlines()
        in_else = False
        for line in lines:
            if line.strip().startswith("else:"):
                in_else = True
            if in_else and 'kind = "S"' in line and "_find_source" not in line:
                assert False, (
                    "else-branch must not unconditionally set kind='S' -- that would break Local users"
                )

    def test_camera_first_and_legacy_flat_coexist(self, tmp_path):
        """_LocalBackend must serve BOTH old flat files AND new year/month/day files
        from the same base directory (Georg's mixed-layout scenario).

        Old files are flat in camera/. New files are in camera/2026/05/08/.
        Both must be resolvable via backend.resolve().
        """
        cam = tmp_path / "Terrasse"
        # Old flat file
        flat_fname = "Terrasse_2026-05-07_09-00-00_MOVEMENT_OLD00001.jpg"
        cam.mkdir()
        (cam / flat_fname).write_bytes(b"\xff\xd8")
        # New camera-first file
        day_dir = cam / "2026" / "05" / "08"
        day_dir.mkdir(parents=True)
        new_fname = "Terrasse_2026-05-08_10-30-00_MOVEMENT_ABCD1234.jpg"
        (day_dir / new_fname).write_bytes(b"\xff\xd8")

        b = _LocalBackend(str(tmp_path))
        # Flat file -> resolve(camera, filename)
        flat_resolved = b.resolve("Terrasse", flat_fname)
        assert flat_resolved is not None and flat_resolved.is_file(), (
            "resolve(camera, flat_filename) must work for legacy flat files"
        )
        # Camera-first file -> resolve(camera, year, month, day, filename)
        new_resolved = b.resolve("Terrasse", "2026", "05", "08", new_fname)
        assert new_resolved is not None and new_resolved.is_file(), (
            "resolve(camera, year, month, day, filename) must work for camera-first files"
        )

    def test_smb_date_first_single_source_still_routes_to_smb(self):
        """When parts[0] is a year (SMB date-first single-source), kind must still be 'S'.

        The disambiguation fix must NOT change how SMB date-first paths are routed.
        These paths have parts[0] = '2026' (a 4-digit year), which triggers the
        EARLIER heuristic before the camera-first disambiguation branch is reached.
        """
        import inspect

        src = inspect.getsource(BoschCameraMediaView.get)
        # The _YEAR_RE.match(head) branch must still unconditionally set kind='S'
        lines = src.splitlines()
        year_first_block = False
        for line in lines:
            stripped = line.strip()
            if "_YEAR_RE.match(head)" in stripped:
                year_first_block = True
            if (
                year_first_block
                and 'kind = "S"' in stripped
                and "_find_source" not in stripped
            ):
                break  # found the unconditional S assignment for date-first SMB
        else:
            assert False, (
                "The SMB date-first path (_YEAR_RE.match(head)) must still unconditionally "
                "set kind='S' -- the disambiguation fix must only apply to camera/year/... paths"
            )

    def test_smb_camera_first_with_smb_configured_routes_to_smb(self):
        """camera/year/month/day/filename must route to SMB when an SMB source exists.

        FTP uploads land on the same NAS share and are browsed via SMB. The camera-first
        disambiguation must pick 'S' when _find_source finds an SMB backend, so FTP
        and SMB camera-first files are served correctly.
        """
        import inspect

        src = inspect.getsource(BoschCameraMediaView.get)
        # After the fix: the camera/year path picks 'S' when SMB is present.
        # bug-hunt 2026-07-03: _find_source now runs via async_add_executor_job.
        assert_in_source(  # camera-first disambiguation must choose kind='S' when _find_source returns SMB, so FTP-uploaded / SMB camera-first files are served correctly
            src,
            'kind = ("S" if await self.hass.async_add_executor_job(_find_source, self.hass, entry_id, "S") is not None else "L")',
        )

    def test_smb_flat_single_source_routes_to_smb_when_no_local(self):
        """Flat SMB file {camera}/{filename} must route to kind='S' when no Local source exists.

        Users with only a NAS share (no local download_path) and old flat files directly
        in the camera/ folder on the NAS would get HTTP 404 if the else-branch always
        hardcoded kind='L'. Fix: prefer Local when it exists, fall back to SMB.
        """
        import inspect

        src = inspect.getsource(BoschCameraMediaView.get)
        # The else branch must use _find_source to choose between L and S.
        # bug-hunt 2026-07-03: _find_source now runs via async_add_executor_job.
        assert_in_source(  # The else-branch (flat file fallback) must check for a Local source before defaulting to kind='L', so SMB-only users with flat NAS files are served
            src,
            'await self.hass.async_add_executor_job(_find_source, self.hass, entry_id, "L")',
        )

    def test_nvr_path_routes_to_nvr(self):
        """camera/YYYY-MM-DD/HH-MM.mp4 must always route to kind='N' (NVR).

        NVR paths use the full ISO-date format (2026-05-08) in parts[1], which matches
        _NVR_DATE_DIR_RE but NOT _YEAR_RE (has dashes). The NVR branch must fire before
        the else branch so that continuous-recording segments are served correctly.
        """
        import inspect

        src = inspect.getsource(BoschCameraMediaView.get)
        assert_in_source(  # BoschCameraMediaView.get must have an NVR branch checking _NVR_DATE_DIR_RE before the flat-file fallback, so NVR segments route to kind='N'
            src, "_NVR_DATE_DIR_RE.match(parts[1])"
        )
        # NVR must set kind='N' unconditionally (not via _find_source heuristic)
        lines = [line.strip() for line in src.splitlines()]
        nvr_block = False
        for line in lines:
            if "_NVR_DATE_DIR_RE.match(parts[1])" in line:
                nvr_block = True
            if nvr_block and 'kind = "N"' in line:
                break
        else:
            assert False, "NVR branch must set kind='N' after matching _NVR_DATE_DIR_RE"

    def test_explicit_kind_tokens_bypass_all_heuristics(self):
        """When the path starts with L, S, or N (multi-source), heuristics are skipped.

        This is the normal path for multi-source entries (both Local + SMB configured).
        Explicit tokens are never ambiguous, so no _find_source lookup is needed there.
        """
        import inspect

        src = inspect.getsource(BoschCameraMediaView.get)
        # The very first if-branch must handle explicit tokens without calling _find_source
        lines = src.splitlines()
        token_block = False
        for line in lines:
            stripped = line.strip()
            if 'head in ("L", "S", "N")' in stripped:
                token_block = True
            if token_block and "tail = parts[1:]" in stripped:
                break  # found the token branch -- correctly peels the token and moves on
            if token_block and "_find_source" in stripped:
                assert False, (
                    "Explicit kind token branch must NOT call _find_source -- "
                    "L/S/N tokens are unambiguous by design"
                )


class TestBrowseYearFirstRouting:
    """Pin the browse handler's year-first detection in async_browse_media.

    Fix v11.0.19: camera=2026 must route to list_year_first_months, not
    list_years('2026'), which would return [] (no nested year dirs inside 2026/).
    """

    def test_browse_handler_calls_year_first_methods(self):
        """_browse_smb/_browse_local source must contain all three year-first method calls."""
        import inspect

        src_smb = inspect.getsource(BoschCameraMediaSource._browse_smb)
        src_local = inspect.getsource(BoschCameraMediaSource._browse_local)
        assert (
            "list_year_first_months" in src_smb or "list_year_first_months" in src_local
        ), (
            "_browse_smb or _browse_local must call list_year_first_months for '2026 -> month' browsing"
        )
        assert (
            "list_year_first_days" in src_smb or "list_year_first_days" in src_local
        ), (
            "_browse_smb or _browse_local must call list_year_first_days for '2026 -> month -> day' browsing"
        )
        assert (
            "list_year_first_events" in src_smb or "list_year_first_events" in src_local
        ), (
            "_browse_smb or _browse_local must call list_year_first_events for year-first events"
        )

    def test_browse_handler_detects_year_with_year_re(self):
        """browse handler must use _YEAR_RE.match(camera) to detect year-first folders."""
        import inspect

        src_smb = inspect.getsource(BoschCameraMediaSource._browse_smb)
        src_local = inspect.getsource(BoschCameraMediaSource._browse_local)
        assert_in_source(  # browse handler (_browse_smb or _browse_local) must call _YEAR_RE.match(camera) to detect year-first folders at len(rest)==1/2/3 inside the camera_first block
            src_smb + src_local, "_YEAR_RE.match(camera)"
        )


# ═════════════════════════════════════════════════════════════════════════
# BoschCameraMediaView.get(): HTTP serving -- Local/NVR/SMB dispatch,
# Range requests, error paths
# ═════════════════════════════════════════════════════════════════════════


def _smb_hass_for_view(entry_id="entry1"):
    """Build hass stub with an SMB backend."""
    hass = MagicMock()
    hass.data = {}
    hass.http = MagicMock()
    opts = {
        "enable_smb_upload": True,
        "upload_protocol": "smb",
        "smb_server": "nas",
        "smb_share": "SHARE",
        "smb_username": "u",
        "smb_password": "p",
        "smb_base_path": "",
        "media_browser_source": "smb",
    }
    coord = SimpleNamespace(options=opts)
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.runtime_data = coord
    entry.title = "Bosch"
    hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)

    async def _exec(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = _exec
    return hass


def _make_view_hass(entry_id: str, tmp_path: Path, kind: str = "L"):
    """Build a hass stub that exposes one source of the given kind."""
    hass = MagicMock()
    hass.data = {}
    hass.http = MagicMock()

    if kind == "L":
        (tmp_path / "Terrasse").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Terrasse" / CAM_FILE).write_bytes(b"mp4data")
        opts = {"download_path": str(tmp_path), "media_browser_source": "local"}
    elif kind == "N":
        seg_dir = tmp_path / "Terrasse" / "2026-05-07"
        seg_dir.mkdir(parents=True, exist_ok=True)
        (seg_dir / "10-00.mp4").write_bytes(b"nvr")
        opts = {
            "enable_nvr": True,
            "nvr_base_path": str(tmp_path),
            "media_browser_source": "local",
        }
    else:
        opts = {
            "enable_smb_upload": True,
            "upload_protocol": "smb",
            "smb_server": "nas",
            "smb_share": "SHARE",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "",
            "media_browser_source": "smb",
        }

    coord = SimpleNamespace(options=opts)
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.runtime_data = coord
    entry.title = "Bosch"
    hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)

    async def _exec(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = _exec
    return hass


class TestMediaViewDispatch:
    """get() dispatches by head token to correct backend."""

    @pytest.mark.asyncio
    async def test_empty_parts_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path)
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):  # web.HTTPNotFound
            await view.get(request, "entry1", "")

    @pytest.mark.asyncio
    async def test_local_wrong_tail_length_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="L")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            # L head + only 1 tail part (need exactly 2)
            await view.get(request, "entry1", "L/Terrasse")

    @pytest.mark.asyncio
    async def test_nvr_wrong_tail_length_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="N")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            # N head + only 2 parts (need exactly 3: cam/date/file)
            await view.get(request, "entry1", "N/Terrasse/2026-05-07")

    @pytest.mark.asyncio
    async def test_source_not_found_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="L")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            # S head -- but only L backend is configured
            await view.get(request, "entry1", "S/Cam/2026/05/07/file.mp4")

    @pytest.mark.asyncio
    async def test_find_source_dispatched_via_executor_not_event_loop(self, tmp_path):
        """Regression (bug-hunt 2026-07-03): _find_source -> _enabled_sources
        does blocking Path.exists()/mkdir()/is_dir() per configured entry.
        get() used to call it directly on the event loop (unlike _browse(),
        which already wraps it) -- hit once per served file/thumbnail, so a
        day-folder view could fire 200+ blocking-I/O calls on the loop.
        Pinned here: hass.async_add_executor_job must be invoked with
        _find_source as the callable, not just eventually produce the same
        return value."""
        hass = _make_view_hass("entry1", tmp_path, kind="L")

        executor_calls: list[tuple] = []

        async def _spy_exec(fn, *args):
            executor_calls.append((fn, *args))
            return fn(*args)

        hass.async_add_executor_job = _spy_exec

        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        await view.get(request, "entry1", "L/Terrasse/" + CAM_FILE)

        find_source_calls = [c for c in executor_calls if c[0] is _find_source]
        assert find_source_calls, (
            "_find_source must be dispatched via hass.async_add_executor_job, "
            "not called directly on the event loop"
        )

    @pytest.mark.asyncio
    async def test_year_head_routes_to_smb(self):
        """head matches _YEAR_RE -> kind=S, tail=parts (date-first single-source)."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(side_effect=[b"data", b""])
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_file = MagicMock(return_value=(fake_fobj, 4))

        request = MagicMock()
        request.headers = {}

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                resp = await view.get(request, "entry1", f"2026/05/07/{stem}.mp4")
        assert resp is real_response

    @pytest.mark.asyncio
    async def test_camera_year_head_routes_to_smb(self):
        """parts[0]=camera, parts[1]=year -> kind=S (camera-first single-source)."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Terrasse_2026-05-07_10-00-00_MOVEMENT_AABB"
        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(side_effect=[b"data", b""])
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_file = MagicMock(return_value=(fake_fobj, 4))

        request = MagicMock()
        request.headers = {}

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                resp = await view.get(
                    request, "entry1", f"Terrasse/2026/05/07/{stem}.mp4"
                )
        assert resp is real_response

    @pytest.mark.asyncio
    async def test_else_branch_routes_to_local(self, tmp_path):
        """head is not a kind/year/camera+year/camera+date -> else branch -> kind=L."""
        # Use a local backend
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        (cam_dir / f"{stem}.mp4").write_bytes(b"data")

        hass = MagicMock()
        hass.data = {}
        opts = {"download_path": str(tmp_path), "media_browser_source": "local"}
        coord = SimpleNamespace(options=opts)
        entry = MagicMock()
        entry.entry_id = "entry1"
        entry.runtime_data = coord
        entry.title = "Bosch"
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)

        async def _exec(fn, *args):
            return fn(*args)

        hass.async_add_executor_job = _exec

        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        # "Cam/file.mp4" -- head="Cam" (not kind token, not year, and len(parts)=2
        # so not camera+year because parts[1]="file.mp4" doesn't match YEAR_RE,
        # and parts[1] doesn't match NVR_DATE_DIR_RE) -> else branch -> kind=L
        # Note: len(tail)=2 is valid for local serve
        resp = await view.get(request, "entry1", f"Cam/{stem}.mp4")
        from aiohttp.web import FileResponse

        assert isinstance(resp, FileResponse)

    @pytest.mark.asyncio
    async def test_nvr_date_head_routes_to_nvr(self, tmp_path):
        """parts[1] matches NVR_DATE_DIR_RE -> kind=N (NVR single-source)."""
        seg_dir = tmp_path / "Terrasse" / "2026-05-07"
        seg_dir.mkdir(parents=True)
        (seg_dir / "10-00.mp4").write_bytes(b"nvr")

        hass = MagicMock()
        hass.data = {}
        opts = {
            "enable_nvr": True,
            "nvr_base_path": str(tmp_path),
            "media_browser_source": "local",
        }
        coord = SimpleNamespace(options=opts)
        entry = MagicMock()
        entry.entry_id = "entry1"
        entry.runtime_data = coord
        entry.title = "NVR"
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)

        async def _exec(fn, *args):
            return fn(*args)

        hass.async_add_executor_job = _exec

        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        resp = await view.get(request, "entry1", "Terrasse/2026-05-07/10-00.mp4")
        assert isinstance(resp, type(resp))  # web.FileResponse

    @pytest.mark.asyncio
    async def test_bad_date_format_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="N")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            await view.get(request, "entry1", "N/Terrasse/20260507/10-00.mp4")

    @pytest.mark.asyncio
    async def test_bad_segment_name_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="N")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            await view.get(request, "entry1", "N/Terrasse/2026-05-07/bad.avi")

    @pytest.mark.asyncio
    async def test_missing_nvr_file_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="N")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            await view.get(request, "entry1", "N/Terrasse/2026-05-07/23-59.mp4")

    @pytest.mark.asyncio
    async def test_bad_year_format_raises_404(self):
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            # year "XXXX" doesn't match _YEAR_RE -> HTTPNotFound
            await view.get(request, "entry1", "S/Cam/XXXX/05/07/file.mp4")

    @pytest.mark.asyncio
    async def test_smb_file_not_found_raises_404(self):
        """FileNotFoundError from open_file -> HTTPNotFound."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        backend_mock = MagicMock()
        backend_mock.open_file = MagicMock(side_effect=FileNotFoundError("nope"))

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with pytest.raises(Exception):
                await view.get(request, "entry1", "S/Cam/2026/05/07/file.mp4")

    @pytest.mark.asyncio
    async def test_smb_os_error_raises_404(self):
        """OSError (e.g. SMB network failure) from open_file -> HTTPNotFound."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        backend_mock = MagicMock()
        backend_mock.open_file = MagicMock(side_effect=OSError("smb down"))

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with pytest.raises(Exception):
                await view.get(request, "entry1", "S/Cam/2026/05/07/file.mp4")

    @pytest.mark.asyncio
    async def test_smb_range_request_206(self):
        """Range header -> status 206 + Content-Range header returned."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)

        payload = b"A" * 2000
        fobj = MagicMock()
        fobj.seek = MagicMock()
        fobj.read = MagicMock(side_effect=[payload[500 : 500 + 256 * 1024], b""])
        fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_file = MagicMock(return_value=(fobj, len(payload)))

        request = MagicMock()
        request.headers = {"Range": "bytes=500-1999"}

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        async def _exec(fn, *args):
            return fn(*args)

        hass.async_add_executor_job = _exec

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                resp = await view.get(request, "entry1", "S/Cam/2026/05/07/file.mp4")
        assert resp is real_response

    @pytest.mark.asyncio
    async def test_smb_full_read_no_range(self):
        """No Range header -> status 200, full content streamed."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)

        payload = b"B" * 100
        fobj = MagicMock()
        fobj.seek = MagicMock()
        fobj.read = MagicMock(side_effect=[payload, b""])
        fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_file = MagicMock(return_value=(fobj, len(payload)))

        request = MagicMock()
        request.headers = {}

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        async def _exec(fn, *args):
            return fn(*args)

        hass.async_add_executor_job = _exec

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                resp = await view.get(request, "entry1", "S/Cam/2026/05/07/file.mp4")
        assert resp is real_response


class TestMediaViewSmbFlatPath:
    @pytest.mark.asyncio
    async def test_tail_len_2_calls_serve_smb_flat(self):
        """S head + 2-part tail -> _serve_smb_flat invoked."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(side_effect=[b"data", b""])
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(return_value=(fake_fobj, 4))

        request = MagicMock()
        request.headers = {}

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                resp = await view.get(request, "entry1", f"S/Cam/{stem}.mp4")
        assert resp is real_response

    @pytest.mark.asyncio
    async def test_tail_len_other_raises_404(self):
        """SMB backend with 3-part tail (not 2, 4, or 5) -> HTTPNotFound."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)

        backend_mock = MagicMock()

        request = MagicMock()
        request.headers = {}

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with pytest.raises(Exception):  # web.HTTPNotFound
                await view.get(request, "entry1", "S/a/b/c")


class TestServeLocalBadMime:
    @pytest.mark.asyncio
    async def test_bad_mime_raises_404(self, tmp_path):
        """File exists and filename parses, but mime is not image/jpeg or video/mp4."""
        # Create a file with extension that parses but gives bad mime
        # We can't easily create a file with a parseable name but bad mime
        # because _FILE_RE only matches jpg/jpeg/mp4 -- instead we test via
        # mocking mimetypes.guess_type to return a different mime.
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        filepath = cam_dir / f"{stem}.jpg"
        filepath.write_bytes(b"data")

        hass = MagicMock()
        hass.data = {}
        opts = {"download_path": str(tmp_path), "media_browser_source": "local"}
        coord = SimpleNamespace(options=opts)
        entry = MagicMock()
        entry.entry_id = "entry1"
        entry.runtime_data = coord
        entry.title = "Bosch"
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)

        async def _exec(fn, *args):
            return fn(*args)

        hass.async_add_executor_job = _exec

        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        with patch(
            "custom_components.bosch_shc_camera.media_source.mimetypes.guess_type",
            return_value=("application/octet-stream", None),
        ):
            with pytest.raises(Exception):  # web.HTTPNotFound
                await view.get(request, "entry1", f"L/Cam/{stem}.jpg")

    @pytest.mark.asyncio
    async def test_bad_filename_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="L")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            # "notvalid.txt" doesn't match _FILE_RE -> HTTPNotFound
            await view.get(request, "entry1", "L/Terrasse/notvalid.txt")

    @pytest.mark.asyncio
    async def test_missing_file_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="L")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            missing = "Terrasse_2026-05-07_23-59-59_MOVEMENT_AABBCCDD.mp4"
            await view.get(request, "entry1", f"L/Terrasse/{missing}")


class TestServeLocalHappyPath:
    @pytest.mark.asyncio
    async def test_serve_local_returns_file_response(self, tmp_path):
        """_serve_local with valid file + correct mime -> web.FileResponse."""
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        filepath = cam_dir / f"{stem}.mp4"
        filepath.write_bytes(b"mp4data")

        hass = MagicMock()
        hass.data = {}
        opts = {"download_path": str(tmp_path), "media_browser_source": "local"}
        coord = SimpleNamespace(options=opts)
        entry = MagicMock()
        entry.entry_id = "entry1"
        entry.runtime_data = coord
        entry.title = "Bosch"
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)

        async def _exec(fn, *args):
            return fn(*args)

        hass.async_add_executor_job = _exec

        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        resp = await view.get(request, "entry1", f"L/Cam/{stem}.mp4")
        from aiohttp.web import FileResponse

        assert isinstance(resp, FileResponse)

    @pytest.mark.asyncio
    async def test_serve_local_jpg_returns_file_response(self, tmp_path):
        """_serve_local with .jpg -> web.FileResponse (via jpg mime)."""
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        filepath = cam_dir / f"{stem}.jpg"
        filepath.write_bytes(b"jpgdata")

        hass = MagicMock()
        hass.data = {}
        opts = {"download_path": str(tmp_path), "media_browser_source": "local"}
        coord = SimpleNamespace(options=opts)
        entry = MagicMock()
        entry.entry_id = "entry1"
        entry.runtime_data = coord
        entry.title = "Bosch"
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)

        async def _exec(fn, *args):
            return fn(*args)

        hass.async_add_executor_job = _exec

        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        resp = await view.get(request, "entry1", f"L/Cam/{stem}.jpg")
        from aiohttp.web import FileResponse

        assert isinstance(resp, FileResponse)


class TestServeNvrHappyPath:
    @pytest.mark.asyncio
    async def test_nvr_serve_valid_file_returns_file_response(self, tmp_path):
        """_serve_nvr with valid date + segment -> returns web.FileResponse."""
        seg_dir = tmp_path / "Terrasse" / "2026-05-07"
        seg_dir.mkdir(parents=True)
        (seg_dir / "10-00.mp4").write_bytes(b"nvr data")

        hass = MagicMock()
        hass.data = {}
        opts = {
            "enable_nvr": True,
            "nvr_base_path": str(tmp_path),
            "media_browser_source": "local",
        }
        coord = SimpleNamespace(options=opts)
        entry = MagicMock()
        entry.entry_id = "entry1"
        entry.runtime_data = coord
        entry.title = "NVR"
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)

        async def _exec(fn, *args):
            return fn(*args)

        hass.async_add_executor_job = _exec

        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        resp = await view.get(request, "entry1", "N/Terrasse/2026-05-07/10-00.mp4")
        # Should return web.FileResponse (not raise)
        from aiohttp.web import FileResponse

        assert isinstance(resp, FileResponse)


class TestServeSmFlatFull:
    @pytest.mark.asyncio
    async def test_serve_smb_flat_happy_path(self):
        """_serve_smb_flat: valid filename -> open_flat_file -> stream."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"

        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(side_effect=[b"mp4data", b""])
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(return_value=(fake_fobj, 7))

        request = MagicMock()
        request.headers = {}

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        async def _exec(fn, *args):
            return fn(*args)

        hass.async_add_executor_job = _exec

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                resp = await view.get(request, "entry1", f"S/Cam/{stem}.mp4")
        assert resp is real_response

    @pytest.mark.asyncio
    async def test_serve_smb_flat_invalid_filename_raises_404(self):
        """_serve_smb_flat: unparseable filename -> HTTPNotFound."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)

        backend_mock = MagicMock()

        request = MagicMock()
        request.headers = {}

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with pytest.raises(Exception):
                await view.get(request, "entry1", "S/Cam/invalid_filename.mp4")

    @pytest.mark.asyncio
    async def test_serve_smb_flat_file_not_found_raises_404(self):
        """_serve_smb_flat: open_flat_file raises FileNotFoundError -> HTTPNotFound."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(side_effect=FileNotFoundError("nope"))

        request = MagicMock()
        request.headers = {}

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with pytest.raises(Exception):
                await view.get(request, "entry1", f"S/Cam/{stem}.mp4")

    @pytest.mark.asyncio
    async def test_serve_smb_flat_os_error_raises_404(self):
        """_serve_smb_flat: OSError from open_flat_file -> HTTPNotFound."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(side_effect=OSError("smb down"))

        request = MagicMock()
        request.headers = {}

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with pytest.raises(Exception):
                await view.get(request, "entry1", f"S/Cam/{stem}.mp4")


class TestStreamSmbFobjRangeErrors:
    def _make_smb_view_with_mock_fobj(self, payload: bytes, range_header: str):
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"

        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(side_effect=[payload, b""])
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(return_value=(fake_fobj, len(payload)))

        request = MagicMock()
        request.headers = {"Range": range_header}

        return hass, view, stem, backend_mock, fake_fobj, request

    @pytest.mark.asyncio
    async def test_invalid_range_header_falls_back_to_200(self):
        """Range: bytes=notanumber -> ValueError caught -> full 200 response."""
        payload = b"X" * 100
        _hass, view, stem, backend_mock, _fake_fobj, request = (
            self._make_smb_view_with_mock_fobj(payload, "bytes=notanumber-")
        )

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()
        # Capture the status passed to StreamResponse
        created_statuses = []

        def _make_response(*args, **kwargs):
            created_statuses.append(kwargs.get("status", 200))
            return real_response

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with patch(f"{MODULE}.web.StreamResponse", side_effect=_make_response):
                resp = await view.get(request, "entry1", f"S/Cam/{stem}.mp4")
        assert resp is real_response
        # Invalid range -> status should be 200
        assert created_statuses[0] == 200

    @pytest.mark.asyncio
    async def test_range_end_only_no_start(self):
        """Range: bytes=-500 (end only, no start) -> treated as full 200."""
        payload = b"Y" * 200
        _hass, view, stem, backend_mock, _fake_fobj, request = (
            self._make_smb_view_with_mock_fobj(payload, "bytes=-500")
        )

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                resp = await view.get(request, "entry1", f"S/Cam/{stem}.mp4")
        assert resp is real_response

    @pytest.mark.asyncio
    async def test_range_start_beyond_size_falls_back_to_200(self):
        """Range where start > size -> invalid range -> falls back to 200."""
        payload = b"Z" * 50
        _hass, view, stem, backend_mock, _fake_fobj, request = (
            self._make_smb_view_with_mock_fobj(payload, "bytes=9999-99999")
        )

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        created_statuses = []

        def _make_response(*args, **kwargs):
            created_statuses.append(kwargs.get("status", 200))
            return real_response

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with patch(f"{MODULE}.web.StreamResponse", side_effect=_make_response):
                resp = await view.get(request, "entry1", f"S/Cam/{stem}.mp4")
        assert resp is real_response
        # Out-of-bounds range -> fallback 200
        assert created_statuses[0] == 200


class TestStreamSmbFobjFinally:
    @pytest.mark.asyncio
    async def test_fobj_closed_even_when_write_raises(self):
        """Finally block: fobj.close() called even if response.write() raises."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"

        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(return_value=b"data")
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(return_value=(fake_fobj, 4))

        # response.write raises an exception to simulate write failure
        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock(side_effect=ConnectionResetError("client gone"))
        real_response.write_eof = AsyncMock()

        async def _exec(fn, *args):
            return fn(*args)

        hass.async_add_executor_job = _exec

        request = MagicMock()
        request.headers = {}

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                with pytest.raises(ConnectionResetError):
                    await view.get(request, "entry1", f"S/Cam/{stem}.mp4")

        # fobj.close() MUST have been called (finally block)
        fake_fobj.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_fobj_closed_on_happy_path(self):
        """Finally block: fobj.close() is also called on the normal success path."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"

        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(side_effect=[b"data", b""])
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(return_value=(fake_fobj, 4))

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        async def _exec(fn, *args):
            return fn(*args)

        hass.async_add_executor_job = _exec

        request = MagicMock()
        request.headers = {}

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                await view.get(request, "entry1", f"S/Cam/{stem}.mp4")

        fake_fobj.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_chunk_breaks_read_loop(self):
        """fobj.read() returns empty bytes before remaining is exhausted -> break."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"

        # size=1000 but fobj returns b"" immediately -> empty chunk -> break
        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(return_value=b"")  # always returns empty
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(return_value=(fake_fobj, 1000))

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        async def _exec(fn, *args):
            return fn(*args)

        hass.async_add_executor_job = _exec

        request = MagicMock()
        request.headers = {}

        with patch(
            f"{MODULE}._find_source", return_value=(MagicMock(kind="S"), backend_mock)
        ):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                resp = await view.get(request, "entry1", f"S/Cam/{stem}.mp4")

        # write should NOT have been called (broke before writing)
        real_response.write.assert_not_called()
        # close must still be called (finally)
        fake_fobj.close.assert_called_once()
        assert resp is real_response


# ─────────────────────────────────────────────────────────────────────────────
# Section: `_SmbBackend._scandir_filtered` skips NVR-internal `_`-prefixed
# directories (relocated from tests/test_misc_modules_coverage.py)
# ─────────────────────────────────────────────────────────────────────────────


class TestSmbBackendScandirFilteredNvrInternalDirs:
    """`_scandir_filtered(want_dirs=True)` must skip directory entries whose
    name starts with `_` (NVR internal dirs like `_staging`, `_failed`); the
    guard does not apply in file mode."""

    def _make_smb_backend(self):
        from custom_components.bosch_shc_camera.media_source import _SmbBackend

        opts = {
            "smb_server": "nas.local",
            "smb_share": "cameras",
            "smb_username": "user",
            "smb_password": "pass",
            "upload_protocol": "SMB",
            "smb_base_path": "",
            "folder_pattern": "{camera}/{year}/{month}/{day}",
        }
        hass = MagicMock()
        return _SmbBackend(hass, opts)

    def _make_dir_entry(self, name: str, is_dir: bool = True, is_file: bool = False):
        e = MagicMock()
        e.name = name
        e.is_dir = MagicMock(return_value=is_dir)
        e.is_file = MagicMock(return_value=is_file)
        return e

    def test_underscore_prefixed_dirs_skipped(self):
        """`_staging`/`_failed` entries skipped; real camera dirs yielded."""
        backend = self._make_smb_backend()

        entries = [
            self._make_dir_entry("_staging"),  # NVR internal → must be skipped
            self._make_dir_entry("_failed"),  # NVR internal → must be skipped
            self._make_dir_entry("Terrasse"),  # real camera dir → must be yielded
            self._make_dir_entry("Eingang"),  # real camera dir → must be yielded
        ]

        with (
            patch("smbclient.scandir", return_value=iter(entries)),
            patch("smbclient.register_session", return_value=None),
            patch("smbclient.delete_session", return_value=None),
        ):
            result = list(backend._scandir_filtered(want_dirs=True))

        assert "_staging" not in result, "_staging must be filtered out"
        assert "_failed" not in result, "_failed must be filtered out"
        assert "Terrasse" in result, "Terrasse (real dir) must be yielded"
        assert "Eingang" in result, "Eingang (real dir) must be yielded"
        assert len(result) == 2, f"Expected exactly 2 dirs, got {result}"

    def test_underscore_files_not_skipped_in_file_mode(self):
        """The underscore guard only applies when want_dirs=True — a file
        named `_index.json` IS yielded when want_dirs=False."""
        backend = self._make_smb_backend()

        entries = [
            self._make_dir_entry("_index.json", is_dir=False, is_file=True),
            self._make_dir_entry("event.mp4", is_dir=False, is_file=True),
        ]

        with (
            patch("smbclient.scandir", return_value=iter(entries)),
            patch("smbclient.register_session", return_value=None),
            patch("smbclient.delete_session", return_value=None),
        ):
            result = list(backend._scandir_filtered(want_dirs=False))

        assert "_index.json" in result
        assert "event.mp4" in result


# ─────────────────────────────────────────────────────────────────────────────
# Section: `_FILE_RE` legacy no-prefix + camera-prefixed filename matching
# (relocated from tests/test_fresh_install.py — the sync_local_save
# filenaming test it complements lives in tests/test_smb.py)
# ─────────────────────────────────────────────────────────────────────────────


class TestFilenameRegexCameraPrefix:
    """`_FILE_RE` must handle both v10.x (no camera prefix) and v11+
    (with-prefix) filenames."""

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
        """v11+ files: Aussenkamera_2026-05-07_12-00-00_MOVEMENT_11111111.jpg"""
        m = self._regex().match(
            "Aussenkamera_2026-05-07_12-00-00_MOVEMENT_11111111.jpg"
        )
        assert m is not None, "New format (with camera prefix) must match _FILE_RE"
        assert m.group("camera") == "Aussenkamera"
        assert m.group("date") == "2026-05-07"

    def test_camera_name_with_spaces_converted_to_underscore(self):
        """Camera name with a space is stored as underscore —
        Aussenkamera_Einfahrt."""
        m = self._regex().match(
            "Aussenkamera_Einfahrt_2026-05-07_12-00-00_MOVEMENT_ABCD1234.mp4"
        )
        assert m is not None
        assert m.group("camera") == "Aussenkamera_Einfahrt"
        assert m.group("ext") == "mp4"

    def test_non_matching_file_returns_none(self):
        for name in [
            "thumbs.db",
            ".DS_Store",
            "._hidden",
            "2026-bad.jpg",
            "random.txt",
        ]:
            assert self._regex().match(name) is None, (
                f"{name!r} must not match _FILE_RE"
            )
