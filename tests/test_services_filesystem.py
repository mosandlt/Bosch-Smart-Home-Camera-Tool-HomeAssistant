"""Filesystem service handlers: handle_migrate_flat_events + handle_delete_event.

Covers `__init__.py` lines 5139-5268 (the two pure-filesystem service handlers
registered by `_register_services`). Both use `hass.async_add_executor_job` to
perform path mutation in a thread, then post a persistent_notification on
completion.

Tests use `tmp_path` (pure filesystem fixtures, no real HA) and patch
`hass.async_add_executor_job` to invoke the executor body inline so we can
inspect filesystem state synchronously.

Critical paths pinned:
  - migrate: flat → Y/M/D move, skip when dest exists, non-matching files
    untouched, persistent_notification fires exactly once with total count.
  - delete: file_path traversal attack rejected, by-camera + by-date filter,
    empty-dir cleanup, persistent_notification fires with deleted count.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera"


def _resp_cm(status: int):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value="")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_hass():
    """Mock hass that runs executor jobs inline so the filesystem mutation
    completes within the test (no real thread)."""
    hass = MagicMock()
    hass.services.has_service.return_value = False
    hass.services.async_register = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.config_entries.async_loaded_entries.return_value = []

    async def _exec(func, *args, **kwargs):
        return func(*args, **kwargs)

    hass.async_add_executor_job = _exec
    return hass


def _get_handlers(hass):
    return {c.args[1]: c.args[2] for c in hass.services.async_register.call_args_list}


def _make_entry(download_path: str):
    coord = SimpleNamespace(options={"download_path": download_path})
    entry = SimpleNamespace(runtime_data=coord)
    return entry


# ── handle_migrate_flat_events ──────────────────────────────────────────────


class TestHandleMigrateFlatEvents:
    @pytest.mark.asyncio
    async def test_flat_file_moved_to_y_m_d(self, tmp_path):
        """Flat file `<cam>/<cam>_YYYY-MM-DD_HH-MM-SS_MOTION_ABC.mp4` must
        be moved into `<cam>/YYYY/MM/DD/<file>` (year-first hierarchy)."""
        from custom_components.bosch_shc_camera import _register_services

        cam = tmp_path / "terrasse"
        cam.mkdir()
        flat = cam / "terrasse_2026-04-15_18-30-22_MOTION_F00DCAFE.mp4"
        flat.write_bytes(b"x")

        entry = _make_entry(str(tmp_path))
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        _register_services(hass)
        handler = _get_handlers(hass)["migrate_flat_events"]
        await handler(MagicMock(data={}))

        assert not flat.exists(), "flat file must be moved away"
        moved = cam / "2026" / "04" / "15" / "terrasse_2026-04-15_18-30-22_MOTION_F00DCAFE.mp4"
        assert moved.is_file(), f"file must land at {moved}"

    @pytest.mark.asyncio
    async def test_skip_when_dest_exists(self, tmp_path):
        """If destination already exists, source file must NOT be overwritten
        and must remain in place (caller can resolve manually)."""
        from custom_components.bosch_shc_camera import _register_services

        cam = tmp_path / "garten"
        cam.mkdir()
        name = "garten_2026-04-15_18-30-22_MOTION_AABBCCDD.mp4"
        flat = cam / name
        flat.write_bytes(b"new")
        dest_dir = cam / "2026" / "04" / "15"
        dest_dir.mkdir(parents=True)
        existing = dest_dir / name
        existing.write_bytes(b"old")

        entry = _make_entry(str(tmp_path))
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        _register_services(hass)
        handler = _get_handlers(hass)["migrate_flat_events"]
        await handler(MagicMock(data={}))

        assert flat.exists(), "source kept on collision"
        assert existing.read_bytes() == b"old", "destination not overwritten"

    @pytest.mark.asyncio
    async def test_non_matching_files_left_untouched(self, tmp_path):
        """Files that don't match the regex (no date prefix) must stay flat."""
        from custom_components.bosch_shc_camera import _register_services

        cam = tmp_path / "kamera"
        cam.mkdir()
        random = cam / "thumbnail.png"
        random.write_bytes(b"png")
        broken = cam / "no_date_here.mp4"
        broken.write_bytes(b"x")

        entry = _make_entry(str(tmp_path))
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        _register_services(hass)
        handler = _get_handlers(hass)["migrate_flat_events"]
        await handler(MagicMock(data={}))

        assert random.exists()
        assert broken.exists()

    @pytest.mark.asyncio
    async def test_persistent_notification_fired_once(self, tmp_path):
        """After migration, exactly one persistent_notification.create call
        must fire with the total moved count in the message."""
        from custom_components.bosch_shc_camera import _register_services

        cam = tmp_path / "innen"
        cam.mkdir()
        (cam / "innen_2026-01-02_03-04-05_MOTION_DEADBEEF.jpg").write_bytes(b"x")
        (cam / "innen_2026-01-02_03-04-06_MOTION_DEADBEEE.jpg").write_bytes(b"y")

        entry = _make_entry(str(tmp_path))
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        _register_services(hass)
        handler = _get_handlers(hass)["migrate_flat_events"]
        await handler(MagicMock(data={}))

        hass.services.async_call.assert_awaited_once()
        call_args = hass.services.async_call.call_args
        assert call_args[0][0] == "persistent_notification"
        assert call_args[0][1] == "create"
        assert "2 file" in call_args[0][2]["message"]

    @pytest.mark.asyncio
    async def test_no_download_path_no_notification_skip(self, tmp_path):
        """When the coordinator has no `download_path` configured, the entry
        must be skipped (no crash). Notification still fires with 0 count."""
        from custom_components.bosch_shc_camera import _register_services

        entry = _make_entry("")
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        _register_services(hass)
        handler = _get_handlers(hass)["migrate_flat_events"]
        await handler(MagicMock(data={}))

        hass.services.async_call.assert_awaited_once()
        assert "0 file" in hass.services.async_call.call_args[0][2]["message"]


# ── handle_delete_event ─────────────────────────────────────────────────────


class TestHandleDeleteEvent:
    @pytest.mark.asyncio
    async def test_file_path_outside_base_rejected(self, tmp_path):
        """Path-traversal: a `file_path` resolving outside the configured
        `download_path` must be rejected (0 deletions, warning logged)."""
        from custom_components.bosch_shc_camera import _register_services

        base = tmp_path / "events"
        base.mkdir()
        outside = tmp_path / "elsewhere" / "secret.mp4"
        outside.parent.mkdir()
        outside.write_bytes(b"secret")

        entry = _make_entry(str(base))
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        _register_services(hass)
        handler = _get_handlers(hass)["delete_event"]
        await handler(MagicMock(data={"file_path": str(outside)}))

        # Must NOT have been deleted
        assert outside.exists(), "outside-of-base file must survive"
        # Notification reports 0
        assert "Deleted 0" in hass.services.async_call.call_args[0][2]["message"]

    @pytest.mark.asyncio
    async def test_file_path_inside_base_deleted(self, tmp_path):
        """A `file_path` resolving inside base must be unlinked."""
        from custom_components.bosch_shc_camera import _register_services

        base = tmp_path / "events"
        base.mkdir()
        cam = base / "terrasse"
        cam.mkdir()
        target = cam / "terrasse_2026-04-15_18-30-22_MOTION_AAAA.mp4"
        target.write_bytes(b"x")

        entry = _make_entry(str(base))
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        _register_services(hass)
        handler = _get_handlers(hass)["delete_event"]
        await handler(MagicMock(data={"file_path": str(target)}))

        assert not target.exists(), "in-base target must be deleted"
        assert "Deleted 1" in hass.services.async_call.call_args[0][2]["message"]

    @pytest.mark.asyncio
    async def test_by_camera_filter(self, tmp_path):
        """Without `date`, all files under `<base>/<camera>/` matching the
        pattern are deleted (recursive)."""
        from custom_components.bosch_shc_camera import _register_services

        base = tmp_path / "events"
        base.mkdir()
        cam = base / "garten"
        cam.mkdir()
        a = cam / "garten_2026-04-15_18-30-22_MOTION_AAAA.mp4"
        a.write_bytes(b"a")
        sub = cam / "2026" / "04" / "16"
        sub.mkdir(parents=True)
        b = sub / "garten_2026-04-16_09-00-00_MOTION_BBBB.mp4"
        b.write_bytes(b"b")
        # Different camera — must NOT be touched
        other = base / "innen"
        other.mkdir()
        o = other / "innen_2026-04-15_10-00-00_MOTION_CCCC.mp4"
        o.write_bytes(b"o")

        entry = _make_entry(str(base))
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        _register_services(hass)
        handler = _get_handlers(hass)["delete_event"]
        await handler(MagicMock(data={"camera": "garten"}))

        assert not a.exists()
        assert not b.exists()
        assert o.exists(), "other camera must be untouched"
        assert "Deleted 2" in hass.services.async_call.call_args[0][2]["message"]

    @pytest.mark.asyncio
    async def test_by_camera_and_date_filter(self, tmp_path):
        """`date` filter narrows by-camera deletion to files whose filename
        date matches exactly."""
        from custom_components.bosch_shc_camera import _register_services

        base = tmp_path / "events"
        base.mkdir()
        cam = base / "garten"
        cam.mkdir()
        keep = cam / "garten_2026-04-16_09-00-00_MOTION_BBBB.mp4"
        keep.write_bytes(b"keep")
        kill = cam / "garten_2026-04-15_18-30-22_MOTION_AAAA.mp4"
        kill.write_bytes(b"kill")

        entry = _make_entry(str(base))
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        _register_services(hass)
        handler = _get_handlers(hass)["delete_event"]
        await handler(MagicMock(data={"camera": "garten", "date": "2026-04-15"}))

        assert keep.exists(), "wrong-date file must survive"
        assert not kill.exists(), "matching-date file must be gone"
        assert "Deleted 1" in hass.services.async_call.call_args[0][2]["message"]

    @pytest.mark.asyncio
    async def test_empty_dirs_cleaned_up(self, tmp_path):
        """After deletion, empty Y/M/D parent dirs must be removed."""
        from custom_components.bosch_shc_camera import _register_services

        base = tmp_path / "events"
        base.mkdir()
        cam = base / "garten"
        deep = cam / "2026" / "04" / "16"
        deep.mkdir(parents=True)
        f = deep / "garten_2026-04-16_09-00-00_MOTION_BBBB.mp4"
        f.write_bytes(b"x")

        entry = _make_entry(str(base))
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        _register_services(hass)
        handler = _get_handlers(hass)["delete_event"]
        await handler(MagicMock(data={"camera": "garten"}))

        assert not f.exists()
        assert not deep.exists(), "empty day dir must be cleaned"
        assert not (cam / "2026" / "04").exists(), "empty month dir must be cleaned"
        assert not (cam / "2026").exists(), "empty year dir must be cleaned"

    @pytest.mark.asyncio
    async def test_no_camera_no_path_zero_deletions(self, tmp_path):
        """Neither `file_path` nor `camera` given → 0 deletions, no crash."""
        from custom_components.bosch_shc_camera import _register_services

        base = tmp_path / "events"
        base.mkdir()
        cam = base / "garten"
        cam.mkdir()
        keep = cam / "garten_2026-04-16_09-00-00_MOTION_BBBB.mp4"
        keep.write_bytes(b"keep")

        entry = _make_entry(str(base))
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        _register_services(hass)
        handler = _get_handlers(hass)["delete_event"]
        await handler(MagicMock(data={}))

        assert keep.exists()
        assert "Deleted 0" in hass.services.async_call.call_args[0][2]["message"]
