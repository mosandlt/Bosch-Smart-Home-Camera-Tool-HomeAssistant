"""Tests for `BoschCameraMediaSource._browse` dispatch tree (Round 4).

`_browse` is the routing brain of the Media Browser — given an
identifier path like `01ENT/Cam/2026-05-04`, it picks the right
backend, level, and child set. ~250 LOC across `_browse`,
`_browse_entry_root`, `_browse_local`, `_browse_nvr`, `_browse_smb`.

Strategy: build a minimal hass with `_enabled_sources` returning
controlled fixtures, then exercise each identifier-path shape:

  - Empty identifier with 0 / 1 / 2+ sources
  - Entry-root with single backend → directly into backend tree
  - Entry-root with multiple sources → source chooser
  - Single-source-skip behavior (parts[1] not in L/S/N)
  - Local backend: cameras / dates / events
  - NVR backend: cameras / dates / segments
  - Unknown entry → Unresolvable
  - Too-deep path → Unresolvable
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


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


# ── No sources → empty root ──────────────────────────────────────────────


class TestBrowseEmpty:
    def test_no_enabled_sources_returns_empty_root(self):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
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


# ── Single entry single backend → direct tree ────────────────────────────


class TestBrowseSingleEntrySingleBackend:
    def test_root_lists_cameras_directly(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        _seed_local_event(tmp_path, "Terrasse", "2026-05-04")
        _seed_local_event(tmp_path, "Garten", "2026-05-04")
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        root = src._browse("")
        # Root with single entry + single source → skips chooser, lists cameras
        titles = [c.title for c in root.children]
        assert "Terrasse" in titles
        assert "Garten" in titles

    def test_camera_level_lists_years(self, tmp_path):
        """Camera-first tree (default): browsing a camera shows years, not flat dates."""
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        _seed_local_event(tmp_path, "Terrasse", "2026-05-04")
        _seed_local_event(tmp_path, "Terrasse", "2026-05-03")
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse")
        titles = [c.title for c in out.children]
        assert "2026" in titles

    def test_day_level_lists_events(self, tmp_path):
        """Camera-first tree: browsing camera/year/month/day shows events."""
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
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
        """Camera-first tree: 6 rest segments (beyond year/month/day/events) → Unresolvable."""
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        from homeassistant.components.media_source.error import Unresolvable
        _seed_local_event(tmp_path, "Cam", "2026-05-04")
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        with pytest.raises(Unresolvable):
            src._browse("01TESTENTRY/Cam/2026/05/04/extra/extra2")


# ── Multiple entries → entry chooser ─────────────────────────────────────


class TestBrowseMultipleEntries:
    def test_root_lists_entries(self, tmp_path):
        """Two loaded config entries → root level shows them as
        chooser nodes (one per entry)."""
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        # Set up two entries each with their own download dir
        dir_a = tmp_path / "entry_a"
        dir_b = tmp_path / "entry_b"
        dir_a.mkdir()
        dir_b.mkdir()
        _seed_local_event(dir_a, "CamA", "2026-05-04")
        _seed_local_event(dir_b, "CamB", "2026-05-04")

        coord_a = SimpleNamespace(options={
            "enable_auto_download": True,
            "download_path": str(dir_a),
            "media_browser_source": "auto",
        })
        coord_b = SimpleNamespace(options={
            "enable_auto_download": True,
            "download_path": str(dir_b),
            "media_browser_source": "auto",
        })
        entry_a = SimpleNamespace(
            entry_id="01ENT_A", runtime_data=coord_a, title="Account A",
        )
        entry_b = SimpleNamespace(
            entry_id="01ENT_B", runtime_data=coord_b, title="Account B",
        )

        def _get_entry(eid):
            return entry_a if eid == "01ENT_A" else entry_b if eid == "01ENT_B" else None

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
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        from homeassistant.components.media_source.error import Unresolvable
        _seed_local_event(tmp_path, "Cam", "2026-05-04")
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        with pytest.raises(Unresolvable):
            src._browse("01DOESNOTEXIST")


# ── NVR backend dispatch ─────────────────────────────────────────────────


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
        coord = SimpleNamespace(options={
            "enable_auto_download": False,
            "enable_nvr": True,
            "nvr_base_path": str(nvr_base),
            "media_browser_source": "auto",
        })
        entry = SimpleNamespace(
            entry_id="01NVRONLY", runtime_data=coord, title="NVR-Only",
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
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        hass = self._setup_nvr_only(tmp_path)
        src = BoschCameraMediaSource(hass)
        root = src._browse("")
        titles = [c.title for c in root.children]
        assert "Cam" in titles

    def test_nvr_camera_lists_dates(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        hass = self._setup_nvr_only(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01NVRONLY/Cam")
        titles = [c.title for c in out.children]
        assert "2026-05-04" in titles

    def test_nvr_date_lists_segments_with_time_label(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
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
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        from homeassistant.components.media_source.error import Unresolvable
        hass = self._setup_nvr_only(tmp_path)
        src = BoschCameraMediaSource(hass)
        with pytest.raises(Unresolvable):
            src._browse("01NVRONLY/Cam/2026-05-04/foo/bar")


# ── Multi-source single-entry: source chooser appears ────────────────────


class TestMultiSourceSingleEntry:
    def _setup_local_plus_nvr(self, tmp_path):
        """Single config entry that has BOTH local download AND NVR
        enabled — the entry root should show a source chooser."""
        local_dir = tmp_path / "local"
        nvr_dir = tmp_path / "nvr"
        local_dir.mkdir()
        nvr_dir.mkdir()
        _seed_local_event(local_dir, "Cam", "2026-05-04")
        (nvr_dir / "Cam" / "2026-05-04").mkdir(parents=True)
        (nvr_dir / "Cam" / "2026-05-04" / "10-30.mp4").write_text("x")

        coord = SimpleNamespace(options={
            "enable_auto_download": True,
            "download_path": str(local_dir),
            "enable_nvr": True,
            "nvr_base_path": str(nvr_dir),
            "media_browser_source": "auto",
        })
        entry = SimpleNamespace(
            entry_id="01MULTI", runtime_data=coord, title="Multi-Source",
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
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
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
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        hass = self._setup_local_plus_nvr(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01MULTI/L")
        titles = [c.title for c in out.children]
        assert "Cam" in titles

    def test_nvr_source_explicit_kind(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        hass = self._setup_local_plus_nvr(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01MULTI/N")
        titles = [c.title for c in out.children]
        assert "Cam" in titles

    def test_unknown_kind_raises(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        from homeassistant.components.media_source.error import Unresolvable
        hass = self._setup_local_plus_nvr(tmp_path)
        src = BoschCameraMediaSource(hass)
        with pytest.raises(Unresolvable):
            src._browse("01MULTI/XYZ")


# ── async_browse_media wraps Unresolvable into BrowseError ───────────────


class TestAsyncBrowseMedia:
    @pytest.mark.asyncio
    async def test_unresolvable_becomes_browse_error(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
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
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        _seed_local_event(tmp_path, "Cam", "2026-05-04")
        hass, _ = _hass_with_local_dir(tmp_path)
        async def _run_executor(func, *args, **kw):
            return func(*args, **kw)
        hass.async_add_executor_job = _run_executor
        src = BoschCameraMediaSource(hass)
        item = SimpleNamespace(identifier="")
        out = await src.async_browse_media(item)
        # Single entry single source → cameras at root level
        titles = [c.title for c in out.children]
        assert "Cam" in titles
