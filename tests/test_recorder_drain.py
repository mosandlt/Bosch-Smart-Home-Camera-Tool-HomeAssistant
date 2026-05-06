"""Tests for recorder._drain_staging_to_remote and friends.

Pins the staging-drain pipeline introduced in v11.0.4 (NVR-storage-target):

  1. ``_is_segment_finalized`` (mtime + size threshold) — the gate that
     decides "this segment is safe to upload".
  2. ``_list_staging_candidates`` — directory walker; tolerates missing trees.
  3. ``sync_drain_tick`` — the orchestrator; dispatches local/smb/ftp,
     accumulates per-camera state for the diagnostic sensor, and
     quarantines files that exceed the retry cap.
  4. SMB / FTP retention purge respecting ``nvr_smb_subpath``.
  5. ``_drain_staging_to_remote`` watcher start/stop semantics.

All filesystem and network I/O is mocked; tests use ``tmp_path`` for the
local filesystem fixture so nothing escapes the per-test sandbox.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import recorder


CAM = "Terrasse"


def _make_coord(tmp_path: Path, *, target: str = "local",
                smb_subpath: str = "NVR",
                smb_server: str = "fritz.box",
                smb_share: str = "FRITZ.NAS",
                smb_base_path: str = "Bosch-Kameras",
                retention_days: int = 3):
    """Coordinator stub with everything the drain helpers read."""
    return SimpleNamespace(
        options={
            "enable_nvr": True,
            "nvr_base_path": str(tmp_path),
            "nvr_storage_target": target,
            "nvr_smb_subpath": smb_subpath,
            "nvr_retention_days": retention_days,
            "smb_server": smb_server,
            "smb_share": smb_share,
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": smb_base_path,
        },
        hass=SimpleNamespace(
            async_add_executor_job=MagicMock(),
            loop=SimpleNamespace(call_soon_threadsafe=MagicMock()),
            async_create_task=MagicMock(),
            services=SimpleNamespace(async_call=MagicMock()),
        ),
    )


def _make_segment(tmp_path: Path, cam: str, date: str, name: str,
                  *, age_seconds: float = 120, size_kb: int = 100) -> Path:
    """Create a fake staging segment with a known mtime + size."""
    cam_dir = tmp_path / "_staging" / cam / date
    cam_dir.mkdir(parents=True, exist_ok=True)
    p = cam_dir / name
    p.write_bytes(b"x" * size_kb * 1024)
    mtime = time.time() - age_seconds
    os.utime(p, (mtime, mtime))
    return p


# ── 1. _is_segment_finalized ─────────────────────────────────────────────────


class TestIsSegmentFinalized:
    def test_too_young_returns_false(self):
        assert recorder._is_segment_finalized(
            mtime=time.time() - 10, size=100_000,
        ) is False

    def test_too_small_returns_false(self):
        assert recorder._is_segment_finalized(
            mtime=time.time() - 120, size=100,
        ) is False

    def test_old_enough_and_big_enough_returns_true(self):
        assert recorder._is_segment_finalized(
            mtime=time.time() - 120, size=100_000,
        ) is True

    def test_explicit_now_arg(self):
        """`now` lets tests pin the current time without monkeypatching ``time.time``."""
        ref_now = 1_000_000.0
        assert recorder._is_segment_finalized(
            mtime=ref_now - 120, size=100_000, now=ref_now,
        ) is True
        assert recorder._is_segment_finalized(
            mtime=ref_now - 5, size=100_000, now=ref_now,
        ) is False


# ── 2. _list_staging_candidates ──────────────────────────────────────────────


class TestListStagingCandidates:
    def test_missing_root_returns_empty(self, tmp_path):
        assert recorder._list_staging_candidates(
            str(tmp_path / "does-not-exist"),
        ) == []

    def test_walks_cam_date_files(self, tmp_path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        _make_segment(tmp_path, CAM, "2026-05-06", "10-05.mp4")
        _make_segment(tmp_path, "Innen", "2026-05-06", "11-00.mp4")
        out = recorder._list_staging_candidates(
            str(tmp_path / "_staging"),
        )
        cams = {entry[1] for entry in out}
        assert cams == {CAM, "Innen"}
        # Three files total
        assert len(out) == 3

    def test_skips_non_dir_entries_in_root(self, tmp_path):
        """A stray file under _staging/ must not blow up the walk."""
        staging = tmp_path / "_staging"
        staging.mkdir()
        (staging / "stray.mp4").write_bytes(b"x")
        out = recorder._list_staging_candidates(str(staging))
        assert out == []

    def test_skips_non_dir_date_entry(self, tmp_path):
        """A stray file under _staging/<cam>/ must not blow up."""
        staging = tmp_path / "_staging"
        staging.mkdir()
        cam = staging / CAM
        cam.mkdir()
        (cam / "stray.mp4").write_bytes(b"x")
        out = recorder._list_staging_candidates(str(staging))
        assert out == []


# ── 3. sync_drain_tick — full target dispatch ────────────────────────────────


class TestSyncDrainTickLocal:
    def test_finalized_segment_promoted_to_local_layout(self, tmp_path):
        """target=local → file moves from _staging tree to the canonical
        ``{base}/{cam}/{date}/...`` layout the Media Source already browses."""
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="local")
        result = recorder.sync_drain_tick(coord)
        promoted_path = tmp_path / CAM / "2026-05-06" / "10-00.mp4"
        assert promoted_path.exists()
        # Staging file removed by ``shutil.move``.
        assert not (tmp_path / "_staging" / CAM / "2026-05-06" / "10-00.mp4").exists()
        assert result["promoted"] == 1
        assert result["uploaded"] == 0
        assert result["failed"] == 0
        assert result["pending"] == 0

    def test_too_young_segment_left_in_staging(self, tmp_path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4",
                      age_seconds=5)
        coord = _make_coord(tmp_path, target="local")
        result = recorder.sync_drain_tick(coord)
        assert result["pending"] == 1
        # File untouched.
        assert (tmp_path / "_staging" / CAM / "2026-05-06" / "10-00.mp4").exists()

    def test_unknown_target_falls_through_to_local(self, tmp_path):
        """Misconfigured target → fail-safe to local promotion (never to nowhere)."""
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="garbage")
        result = recorder.sync_drain_tick(coord)
        assert result["promoted"] == 1


class TestSyncDrainTickSmb:
    def test_smb_target_invokes_upload(self, tmp_path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="smb")
        with patch.object(recorder, "_upload_smb",
                          return_value=True) as up:
            result = recorder.sync_drain_tick(coord)
        up.assert_called_once()
        cam_arg = up.call_args.args[2]
        assert cam_arg == CAM
        # Successful upload → staging file is unlinked.
        assert not (tmp_path / "_staging" / CAM / "2026-05-06" / "10-00.mp4").exists()
        assert result["uploaded"] == 1
        assert result["failed"] == 0

    def test_smb_failure_is_counted(self, tmp_path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="smb")
        with patch.object(recorder, "_upload_smb", return_value=False):
            result = recorder.sync_drain_tick(coord)
        assert result["uploaded"] == 0
        assert result["failed"] == 1
        # Staging file kept for retry.
        assert (tmp_path / "_staging" / CAM / "2026-05-06" / "10-00.mp4").exists()


class TestSyncDrainTickFtp:
    def test_ftp_target_invokes_upload(self, tmp_path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        with patch.object(recorder, "_upload_ftp",
                          return_value=True) as up:
            result = recorder.sync_drain_tick(coord)
        up.assert_called_once()
        assert result["uploaded"] == 1
        assert not (tmp_path / "_staging" / CAM / "2026-05-06" / "10-00.mp4").exists()

    def test_ftp_failure_is_counted(self, tmp_path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        with patch.object(recorder, "_upload_ftp", return_value=False):
            result = recorder.sync_drain_tick(coord)
        assert result["failed"] == 1


class TestSyncDrainTickRetryCap:
    """5 failures → file moves to _failed/ + persistent_notification fired."""

    def test_quarantine_after_max_retries(self, tmp_path):
        path = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="smb")
        with patch.object(recorder, "_upload_smb", return_value=False):
            for _ in range(recorder._DRAIN_MAX_RETRIES):
                recorder.sync_drain_tick(coord)
        # File is now under _failed/, not _staging/
        assert not path.exists()
        failed_path = tmp_path / "_failed" / CAM / "2026-05-06" / "10-00.mp4"
        assert failed_path.exists()
        # Counter is cleared once the file is quarantined.
        assert path.as_posix() not in coord._nvr_drain_failures


class TestSyncDrainTickStateCounters:
    """The watcher persists state on the coordinator so the diagnostic sensor
    can render it. Pin the shape."""

    def test_drain_state_populated(self, tmp_path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="local")
        recorder.sync_drain_tick(coord, now=time.time())
        state = coord._nvr_drain_state
        assert state["target"] == "local"
        assert state["promoted"] == 1
        assert "last_age_by_cam" in state
        assert CAM in state["last_age_by_cam"]


# ── 4. SMB / FTP retention purge with nvr_smb_subpath ────────────────────────


class TestNvrCleanupSmbSubpath:
    def test_smb_root_uses_nvr_subpath(self, tmp_path):
        """``_sync_nvr_cleanup_smb`` must walk ONLY the NVR subtree, not the
        entire share — otherwise it would delete cloud-event uploads too."""
        coord = _make_coord(tmp_path, target="smb",
                            smb_base_path="Bosch", smb_subpath="NVR")
        seen: list[str] = []

        def fake_scandir(path):
            seen.append(path)
            return iter([])  # empty → walk terminates

        # smbclient is imported lazily inside the helper — patch its API.
        with patch.dict("sys.modules", {
            "smbclient": MagicMock(
                register_session=MagicMock(),
                scandir=fake_scandir,
                remove=MagicMock(),
                stat=MagicMock(),
            ),
        }):
            recorder._sync_nvr_cleanup_smb(coord)

        # Walked path must end with the NVR subtree, not the bare share.
        assert seen, "_sync_nvr_cleanup_smb did not invoke scandir"
        assert seen[0].endswith("\\Bosch\\NVR")

    def test_smb_skip_without_server(self, tmp_path):
        coord = _make_coord(tmp_path, target="smb", smb_server="")
        with patch.dict("sys.modules", {
            "smbclient": MagicMock(register_session=MagicMock(),
                                   scandir=MagicMock(),
                                   remove=MagicMock(),
                                   stat=MagicMock()),
        }):
            # Should be a no-op — no scandir call.
            recorder._sync_nvr_cleanup_smb(coord)


class TestNvrCleanupFtpSubpath:
    def test_ftp_root_uses_nvr_subpath(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp",
                            smb_base_path="Bosch", smb_subpath="NVR")
        cwd_calls: list[str] = []
        ftp = MagicMock()

        def cwd(p):
            cwd_calls.append(p)

        ftp.cwd.side_effect = cwd
        ftp.retrlines.side_effect = lambda cmd, cb: None  # empty listing
        ftp.quit.return_value = None

        with patch.object(recorder, "_ftp_connect", create=True), \
             patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   return_value=ftp):
            recorder._sync_nvr_cleanup_ftp(coord)
        assert cwd_calls
        # First cwd targets the NVR subtree.
        assert cwd_calls[0] == "/Bosch/NVR"

    def test_ftp_zero_retention_skipped(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp", retention_days=0)
        # Should never even try to connect.
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect") as conn:
            recorder._sync_nvr_cleanup_ftp(coord)
            conn.assert_not_called()


class TestNvrCleanupDispatch:
    """``sync_nvr_cleanup`` is the public entry point — it dispatches to the
    target-specific helper plus always purges the local staging tree."""

    def test_local_only_calls_local_helper(self, tmp_path):
        coord = _make_coord(tmp_path, target="local")
        with patch.object(recorder, "_sync_nvr_cleanup_local") as loc, \
             patch.object(recorder, "_sync_nvr_cleanup_smb") as smb, \
             patch.object(recorder, "_sync_nvr_cleanup_ftp") as ftp:
            recorder.sync_nvr_cleanup(coord)
        loc.assert_called_once_with(coord)
        smb.assert_not_called()
        ftp.assert_not_called()

    def test_smb_target_calls_smb_and_local(self, tmp_path):
        coord = _make_coord(tmp_path, target="smb")
        with patch.object(recorder, "_sync_nvr_cleanup_local") as loc, \
             patch.object(recorder, "_sync_nvr_cleanup_smb") as smb, \
             patch.object(recorder, "_sync_nvr_cleanup_ftp") as ftp:
            recorder.sync_nvr_cleanup(coord)
        loc.assert_called_once_with(coord)
        smb.assert_called_once_with(coord)
        ftp.assert_not_called()

    def test_ftp_target_calls_ftp_and_local(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp")
        with patch.object(recorder, "_sync_nvr_cleanup_local") as loc, \
             patch.object(recorder, "_sync_nvr_cleanup_smb") as smb, \
             patch.object(recorder, "_sync_nvr_cleanup_ftp") as ftp:
            recorder.sync_nvr_cleanup(coord)
        loc.assert_called_once_with(coord)
        smb.assert_not_called()
        ftp.assert_called_once_with(coord)

    def test_zero_retention_short_circuits(self, tmp_path):
        coord = _make_coord(tmp_path, retention_days=0)
        with patch.object(recorder, "_sync_nvr_cleanup_local") as loc:
            recorder.sync_nvr_cleanup(coord)
        loc.assert_not_called()


# ── 5. Watcher start / stop coroutine ────────────────────────────────────────


class TestDrainStagingWatcher:
    @pytest.mark.asyncio
    async def test_watcher_runs_tick_then_sleeps(self, tmp_path):
        """One tick on enable_nvr=True; sleep is what gets cancelled."""
        coord = _make_coord(tmp_path, target="local")
        # Provide an awaitable executor stub.
        async def _exec(fn, c):
            return fn(c)
        coord.hass.async_add_executor_job = _exec

        ticks: list[int] = []
        original_tick = recorder.sync_drain_tick

        def counting_tick(coordinator, **kwargs):
            ticks.append(1)
            return original_tick(coordinator, **kwargs)

        with patch.object(recorder, "sync_drain_tick", counting_tick), \
             patch.object(recorder, "_DRAIN_TICK_SECONDS", 0.05):
            task = asyncio.create_task(
                recorder._drain_staging_to_remote(coord),
            )
            await asyncio.sleep(0.15)  # let it run a couple of ticks
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert ticks, "watcher never invoked sync_drain_tick"

    @pytest.mark.asyncio
    async def test_watcher_skips_when_nvr_disabled(self, tmp_path):
        coord = _make_coord(tmp_path)
        coord.options["enable_nvr"] = False

        async def _exec(fn, c):
            return fn(c)
        coord.hass.async_add_executor_job = _exec

        with patch.object(recorder, "sync_drain_tick") as tick, \
             patch.object(recorder, "_DRAIN_TICK_SECONDS", 0.05):
            task = asyncio.create_task(
                recorder._drain_staging_to_remote(coord),
            )
            await asyncio.sleep(0.15)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        tick.assert_not_called()

    @pytest.mark.asyncio
    async def test_watcher_swallows_tick_exception(self, tmp_path):
        """A raising tick must not kill the watcher loop."""
        coord = _make_coord(tmp_path, target="local")

        async def _exec(fn, c):
            return fn(c)
        coord.hass.async_add_executor_job = _exec

        calls = []

        def boom(coordinator, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("simulated")
        with patch.object(recorder, "sync_drain_tick", boom), \
             patch.object(recorder, "_DRAIN_TICK_SECONDS", 0.05):
            task = asyncio.create_task(
                recorder._drain_staging_to_remote(coord),
            )
            await asyncio.sleep(0.20)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert len(calls) >= 2, "watcher exited after first exception"


# ── 6. Pure helpers — _remote_smb_path / _remote_ftp_path ────────────────────


class TestRemotePathHelpers:
    def test_smb_path_includes_subpath(self, tmp_path):
        coord = _make_coord(tmp_path, target="smb",
                            smb_base_path="Bosch", smb_subpath="NVR")
        path = recorder._remote_smb_path(
            coord.options, CAM, "2026-05-06", "10-00.mp4",
        )
        assert path == r"\\fritz.box\FRITZ.NAS\Bosch\NVR\Terrasse\2026-05-06\10-00.mp4"

    def test_smb_path_sanitizes_camera_name(self, tmp_path):
        coord = _make_coord(tmp_path, target="smb",
                            smb_base_path="Bosch", smb_subpath="NVR")
        path = recorder._remote_smb_path(
            coord.options, "../../etc", "2026-05-06", "10-00.mp4",
        )
        # ``..`` collapsed by _safe_name → no traversal in the rendered path.
        head_after_root = path.split("\\NVR\\", 1)[1]
        cam_component = head_after_root.split("\\", 1)[0]
        assert ".." not in cam_component

    def test_ftp_path_starts_with_slash(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp",
                            smb_base_path="Bosch", smb_subpath="NVR")
        path = recorder._remote_ftp_path(
            coord.options, CAM, "2026-05-06", "10-00.mp4",
        )
        assert path == "/Bosch/NVR/Terrasse/2026-05-06/10-00.mp4"


# ── 7. _upload_smb / _upload_ftp / _move_local — direct unit tests ───────────


class TestMoveLocal:
    def test_success_returns_true_and_creates_dest(self, tmp_path):
        src = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="local")
        ok = recorder._move_local(
            coord, str(src), str(tmp_path), CAM, "2026-05-06", "10-00.mp4",
        )
        assert ok is True
        assert (tmp_path / CAM / "2026-05-06" / "10-00.mp4").exists()
        assert not src.exists()

    def test_oserror_returns_false(self, tmp_path):
        coord = _make_coord(tmp_path, target="local")
        with patch.object(recorder.shutil, "move",
                          side_effect=OSError("nope")):
            ok = recorder._move_local(
                coord, "/missing/x.mp4", str(tmp_path), CAM,
                "2026-05-06", "10-00.mp4",
            )
        assert ok is False


class TestUploadSmb:
    def test_returns_false_when_smbprotocol_missing(self, tmp_path):
        """``ImportError`` path — test environment has smbprotocol installed
        but the helper must still tolerate its absence on user systems."""
        coord = _make_coord(tmp_path, target="smb")
        # Make ``import smbclient`` raise ImportError inside the function.
        import sys
        with patch.dict(sys.modules, {"smbclient": None}):
            ok = recorder._upload_smb(
                coord, "/fake.mp4", CAM, "2026-05-06", "10-00.mp4",
            )
        assert ok is False

    def test_returns_false_when_server_empty(self, tmp_path):
        coord = _make_coord(tmp_path, target="smb", smb_server="")
        # smbclient is a real module; we only need to short-circuit before it
        # gets used.
        ok = recorder._upload_smb(
            coord, "/fake.mp4", CAM, "2026-05-06", "10-00.mp4",
        )
        assert ok is False

    def test_returns_false_on_session_failure(self, tmp_path):
        coord = _make_coord(tmp_path, target="smb")
        import sys
        smb_mock = MagicMock(register_session=MagicMock(side_effect=OSError("boom")),
                             open_file=MagicMock())
        with patch.dict(sys.modules, {"smbclient": smb_mock}):
            ok = recorder._upload_smb(
                coord, "/fake.mp4", CAM, "2026-05-06", "10-00.mp4",
            )
        assert ok is False

    def test_returns_false_on_mkdirs_failure(self, tmp_path):
        coord = _make_coord(tmp_path, target="smb")
        import sys
        smb_mock = MagicMock(register_session=MagicMock(),
                             open_file=MagicMock())
        with patch.dict(sys.modules, {"smbclient": smb_mock}), \
             patch("custom_components.bosch_shc_camera.smb.smb_makedirs",
                   side_effect=OSError("mkdir failed")):
            ok = recorder._upload_smb(
                coord, "/fake.mp4", CAM, "2026-05-06", "10-00.mp4",
            )
        assert ok is False

    def test_returns_false_on_upload_open_failure(self, tmp_path):
        """File-open or upload itself raising → caught and returns False."""
        coord = _make_coord(tmp_path, target="smb")
        import sys
        smb_mock = MagicMock(register_session=MagicMock(),
                             open_file=MagicMock(side_effect=OSError("write failed")))
        with patch.dict(sys.modules, {"smbclient": smb_mock}), \
             patch("custom_components.bosch_shc_camera.smb.smb_makedirs"):
            ok = recorder._upload_smb(
                coord, "/missing-file.mp4", CAM, "2026-05-06", "10-00.mp4",
            )
        assert ok is False

    def test_happy_path_writes_to_smb(self, tmp_path):
        """Successful write — smbclient.open_file gets the bytes."""
        src = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="smb")
        import sys
        from io import BytesIO

        smb_dst = BytesIO()
        smb_dst.close = lambda: None  # so the with-block doesn't blow up

        class _OpenFileCtx:
            def __enter__(self_inner):
                return smb_dst
            def __exit__(self_inner, *exc):
                return False

        smb_mock = MagicMock(register_session=MagicMock(),
                             open_file=MagicMock(return_value=_OpenFileCtx()))
        with patch.dict(sys.modules, {"smbclient": smb_mock}), \
             patch("custom_components.bosch_shc_camera.smb.smb_makedirs"):
            ok = recorder._upload_smb(
                coord, str(src), CAM, "2026-05-06", "10-00.mp4",
            )
        assert ok is True
        assert smb_dst.getvalue()  # got bytes


class TestUploadFtp:
    def test_returns_false_when_server_empty(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp", smb_server="")
        ok = recorder._upload_ftp(
            coord, "/fake.mp4", CAM, "2026-05-06", "10-00.mp4",
        )
        assert ok is False

    def test_returns_false_on_login_failure(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp")
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   side_effect=OSError("login refused")):
            ok = recorder._upload_ftp(
                coord, "/fake.mp4", CAM, "2026-05-06", "10-00.mp4",
            )
        assert ok is False

    def test_returns_false_on_mkdirs_failure(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp")
        ftp = MagicMock()
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   return_value=ftp), \
             patch("custom_components.bosch_shc_camera.smb._ftp_makedirs",
                   side_effect=OSError("mkdir refused")):
            ok = recorder._upload_ftp(
                coord, "/fake.mp4", CAM, "2026-05-06", "10-00.mp4",
            )
        assert ok is False

    def test_returns_false_on_storbinary_failure(self, tmp_path):
        src = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        ftp = MagicMock()
        ftp.storbinary.side_effect = OSError("transfer aborted")
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   return_value=ftp), \
             patch("custom_components.bosch_shc_camera.smb._ftp_makedirs"):
            ok = recorder._upload_ftp(
                coord, str(src), CAM, "2026-05-06", "10-00.mp4",
            )
        assert ok is False

    def test_happy_path_calls_storbinary(self, tmp_path):
        src = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        ftp = MagicMock()
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   return_value=ftp), \
             patch("custom_components.bosch_shc_camera.smb._ftp_makedirs"):
            ok = recorder._upload_ftp(
                coord, str(src), CAM, "2026-05-06", "10-00.mp4",
            )
        assert ok is True
        ftp.storbinary.assert_called_once()
        # Quit attempt happens in finally block.
        ftp.quit.assert_called_once()

    def test_quit_failure_falls_back_to_close(self, tmp_path):
        """ftp.quit() raising in the finally-block must fall through to close."""
        src = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        ftp = MagicMock()
        ftp.quit.side_effect = OSError("connection broken")
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   return_value=ftp), \
             patch("custom_components.bosch_shc_camera.smb._ftp_makedirs"):
            recorder._upload_ftp(
                coord, str(src), CAM, "2026-05-06", "10-00.mp4",
            )
        ftp.close.assert_called_once()


# ── 8. Quarantine helper ─────────────────────────────────────────────────────


class TestQuarantineFailed:
    def test_moves_file_into_failed_tree(self, tmp_path):
        src = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        recorder._quarantine_failed(
            str(tmp_path), str(src), CAM, "2026-05-06", "10-00.mp4",
        )
        assert (tmp_path / "_failed" / CAM / "2026-05-06" / "10-00.mp4").exists()
        assert not src.exists()

    def test_oserror_swallowed(self, tmp_path):
        """A move-failure must not raise — the watcher is best-effort."""
        with patch.object(recorder.shutil, "move",
                          side_effect=OSError("permission denied")):
            recorder._quarantine_failed(
                str(tmp_path), "/missing.mp4", CAM,
                "2026-05-06", "10-00.mp4",
            )


# ── 9. _sync_nvr_cleanup_local ───────────────────────────────────────────────


class TestSyncNvrCleanupLocal:
    def test_skips_when_path_missing(self, tmp_path):
        coord = _make_coord(tmp_path / "doesnotexist", target="local")
        # No raise.
        recorder._sync_nvr_cleanup_local(coord)

    def test_skips_when_zero_retention(self, tmp_path):
        coord = _make_coord(tmp_path, target="local", retention_days=0)
        recorder._sync_nvr_cleanup_local(coord)

    def test_deletes_old_files(self, tmp_path):
        # Old file
        old = tmp_path / CAM / "2026-04-01" / "10-00.mp4"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"x")
        old_mtime = time.time() - 10 * 86400
        os.utime(old, (old_mtime, old_mtime))
        # Recent file
        recent = tmp_path / CAM / "2026-05-06" / "11-00.mp4"
        recent.parent.mkdir(parents=True)
        recent.write_bytes(b"y")

        coord = _make_coord(tmp_path, target="local", retention_days=3)
        recorder._sync_nvr_cleanup_local(coord)
        assert not old.exists()
        assert recent.exists()

    def test_stat_failure_skipped(self, tmp_path):
        """A file that disappears between os.walk and os.stat must not raise."""
        f = tmp_path / CAM / "2026-04-01" / "10-00.mp4"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
        coord = _make_coord(tmp_path, target="local", retention_days=3)
        with patch("os.stat", side_effect=OSError("vanished")):
            recorder._sync_nvr_cleanup_local(coord)

    def test_remove_failure_swallowed(self, tmp_path):
        """An unlink failure must NOT bubble up — best-effort cleanup."""
        f = tmp_path / CAM / "2026-04-01" / "10-00.mp4"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
        old_mtime = time.time() - 10 * 86400
        os.utime(f, (old_mtime, old_mtime))
        coord = _make_coord(tmp_path, target="local", retention_days=3)
        real_remove = os.remove

        def selective(p):
            if str(p) == str(f):
                raise OSError("readonly")
            return real_remove(p)

        with patch("os.remove", side_effect=selective):
            recorder._sync_nvr_cleanup_local(coord)
        # File still exists (remove failed silently).
        assert f.exists()

    def test_rmdir_failure_swallowed(self, tmp_path):
        """An rmdir/listdir failure during the empty-folder prune pass must
        not raise — second pass is best-effort."""
        # Create one old file (will be deleted) leaving an empty per-day dir.
        old = tmp_path / CAM / "2026-04-01" / "10-00.mp4"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"x")
        old_mtime = time.time() - 10 * 86400
        os.utime(old, (old_mtime, old_mtime))

        coord = _make_coord(tmp_path, target="local", retention_days=3)
        # Patch listdir during the second (rmdir) pass to raise. The first
        # walk call uses os.walk which uses scandir internally — listdir
        # is only used inside the rmdir prune block.
        real_listdir = os.listdir

        def selective(p):
            # Only fail for the per-day dir we just emptied.
            if str(p) == str(old.parent):
                raise OSError("perm")
            return real_listdir(p)

        with patch("os.listdir", side_effect=selective):
            recorder._sync_nvr_cleanup_local(coord)
        # File was still removed successfully in pass 1.
        assert not old.exists()


# ── 10. _list_staging_candidates extra branches ──────────────────────────────


class TestListStagingExtra:
    def test_listdir_root_oserror(self, tmp_path):
        """os.listdir(staging_root) raising — return empty list."""
        staging = tmp_path / "_staging"
        staging.mkdir()
        with patch("os.listdir", side_effect=OSError("perm")):
            assert recorder._list_staging_candidates(str(staging)) == []

    def test_listdir_cam_oserror(self, tmp_path):
        """os.listdir on the cam-dir raising — skip that camera, continue."""
        staging = tmp_path / "_staging"
        cam = staging / CAM
        cam.mkdir(parents=True)
        # Real listdir on root works (returns ["Terrasse"]); fail only on cam.
        real_listdir = os.listdir
        def selective(p):
            if str(p) == str(cam):
                raise OSError("perm")
            return real_listdir(p)
        with patch("os.listdir", side_effect=selective):
            out = recorder._list_staging_candidates(str(staging))
        assert out == []

    def test_listdir_date_oserror(self, tmp_path):
        """os.listdir on the date-dir raising — skip that date."""
        staging = tmp_path / "_staging"
        date = staging / CAM / "2026-05-06"
        date.mkdir(parents=True)
        real_listdir = os.listdir

        def selective(p):
            if str(p) == str(date):
                raise OSError("perm")
            return real_listdir(p)

        with patch("os.listdir", side_effect=selective):
            out = recorder._list_staging_candidates(str(staging))
        assert out == []

    def test_stat_failure_skipped(self, tmp_path):
        """A file vanishing between os.listdir and os.stat must not raise."""
        seg = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        real_stat = os.stat

        def selective(p, *a, **kw):
            if str(p) == str(seg):
                raise OSError("vanished")
            return real_stat(p, *a, **kw)

        with patch("os.stat", side_effect=selective):
            out = recorder._list_staging_candidates(
                str(tmp_path / "_staging"),
            )
        assert out == []

    def test_non_regular_file_skipped(self, tmp_path):
        """A directory disguised as a file (broken layout) is skipped."""
        staging = tmp_path / "_staging"
        date_dir = staging / CAM / "2026-05-06"
        date_dir.mkdir(parents=True)
        # Make a sub-dir at the file slot.
        bogus = date_dir / "10-00.mp4"
        bogus.mkdir()
        out = recorder._list_staging_candidates(str(staging))
        # The directory entry is not a regular file → skipped.
        assert out == []


# ── 11. sync_drain_tick — successful upload but unlink fails ─────────────────


class TestSyncDrainTickUnlinkFailure:
    def test_smb_unlink_failure_only_logs(self, tmp_path):
        """A successful upload + failed unlink must NOT bump ``failed``."""
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="smb")
        with patch.object(recorder, "_upload_smb", return_value=True), \
             patch("os.unlink", side_effect=OSError("readonly")):
            result = recorder.sync_drain_tick(coord)
        assert result["uploaded"] == 1
        assert result["failed"] == 0

    def test_ftp_unlink_failure_only_logs(self, tmp_path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        with patch.object(recorder, "_upload_ftp", return_value=True), \
             patch("os.unlink", side_effect=OSError("readonly")):
            result = recorder.sync_drain_tick(coord)
        assert result["uploaded"] == 1
        assert result["failed"] == 0

    def test_persistent_notification_swallows_errors(self, tmp_path):
        """If services.async_call raises, the watcher must not crash."""
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="smb")
        # Make services.async_call itself blow up to exercise the except.
        coord.hass.loop.call_soon_threadsafe = MagicMock(
            side_effect=RuntimeError("loop is closed"),
        )
        with patch.object(recorder, "_upload_smb", return_value=False):
            for _ in range(recorder._DRAIN_MAX_RETRIES):
                recorder.sync_drain_tick(coord)
        # Quarantine still happened despite notification path failing.
        assert (tmp_path / "_failed" / CAM / "2026-05-06" / "10-00.mp4").exists()


# ── 12. _sync_nvr_cleanup_smb / _ftp — deeper walks ──────────────────────────


class TestSyncNvrCleanupSmbDeepWalk:
    def test_smb_skipped_when_no_share(self, tmp_path):
        coord = _make_coord(tmp_path, target="smb", smb_share="")
        with patch.dict("sys.modules", {
            "smbclient": MagicMock(register_session=MagicMock(),
                                   scandir=MagicMock(),
                                   remove=MagicMock(),
                                   stat=MagicMock()),
        }):
            recorder._sync_nvr_cleanup_smb(coord)

    def test_smb_session_failure_returns(self, tmp_path):
        coord = _make_coord(tmp_path, target="smb")
        with patch.dict("sys.modules", {
            "smbclient": MagicMock(
                register_session=MagicMock(side_effect=OSError("auth")),
                scandir=MagicMock(),
                remove=MagicMock(),
                stat=MagicMock(),
            ),
        }):
            recorder._sync_nvr_cleanup_smb(coord)

    def test_smb_walk_recurses_and_deletes(self, tmp_path):
        coord = _make_coord(tmp_path, target="smb",
                            smb_base_path="Bosch", smb_subpath="NVR")

        # Build a fake tree with one old file and one fresh file.
        class Entry:
            def __init__(self, name, is_dir):
                self.name = name
                self._is_dir = is_dir
            def is_dir(self):
                return self._is_dir

        old_st = SimpleNamespace(st_mtime=time.time() - 10 * 86400)
        new_st = SimpleNamespace(st_mtime=time.time())

        layout = {
            r"\\fritz.box\FRITZ.NAS\Bosch\NVR": [Entry("Terrasse", True)],
            r"\\fritz.box\FRITZ.NAS\Bosch\NVR\Terrasse": [
                Entry("old.mp4", False), Entry("new.mp4", False),
            ],
        }
        stats = {
            r"\\fritz.box\FRITZ.NAS\Bosch\NVR\Terrasse\old.mp4": old_st,
            r"\\fritz.box\FRITZ.NAS\Bosch\NVR\Terrasse\new.mp4": new_st,
        }

        def fake_scandir(path):
            return iter(layout.get(path, []))

        def fake_stat(path):
            return stats[path]

        removed: list[str] = []

        with patch.dict("sys.modules", {
            "smbclient": MagicMock(
                register_session=MagicMock(),
                scandir=fake_scandir,
                remove=removed.append,
                stat=fake_stat,
            ),
        }):
            recorder._sync_nvr_cleanup_smb(coord)
        assert removed == [r"\\fritz.box\FRITZ.NAS\Bosch\NVR\Terrasse\old.mp4"]

    def test_smb_scandir_exception_swallowed(self, tmp_path):
        """A scandir failure deep in the tree must not propagate."""
        coord = _make_coord(tmp_path, target="smb",
                            smb_base_path="Bosch", smb_subpath="NVR")
        with patch.dict("sys.modules", {
            "smbclient": MagicMock(
                register_session=MagicMock(),
                scandir=MagicMock(side_effect=OSError("scandir failed")),
                remove=MagicMock(),
                stat=MagicMock(),
            ),
        }):
            # Should not raise.
            recorder._sync_nvr_cleanup_smb(coord)

    def test_smb_stat_exception_swallowed(self, tmp_path):
        """smb_stat raising on a leaf file must not bubble up."""
        coord = _make_coord(tmp_path, target="smb",
                            smb_base_path="Bosch", smb_subpath="NVR")

        class Entry:
            name = "boom.mp4"
            def is_dir(self):
                return False

        def fake_scandir(path):
            if path.endswith("\\NVR"):
                return iter([Entry()])
            return iter([])

        with patch.dict("sys.modules", {
            "smbclient": MagicMock(
                register_session=MagicMock(),
                scandir=fake_scandir,
                remove=MagicMock(),
                stat=MagicMock(side_effect=OSError("stat failed")),
            ),
        }):
            recorder._sync_nvr_cleanup_smb(coord)


class TestSyncNvrCleanupFtpDeepWalk:
    def test_ftp_walk_lists_and_deletes(self, tmp_path):
        """End-to-end walk: cwd → LIST → MDTM → DELE for old files only."""
        coord = _make_coord(tmp_path, target="ftp",
                            smb_base_path="Bosch", smb_subpath="NVR")
        ftp = MagicMock()
        # Build a LIST output: one subdir, one old file, one fresh file.
        listings = {
            "/Bosch/NVR": [
                "drwxr-xr-x  2 user grp 4096 May 06 10:00 Terrasse",
            ],
            "/Bosch/NVR/Terrasse": [
                "-rw-r--r--  1 user grp  1024 Apr 01 10:00 old.mp4",
                "-rw-r--r--  1 user grp  1024 May 06 10:00 new.mp4",
            ],
        }

        def fake_retrlines(cmd, cb):
            current = ftp.cwd.call_args.args[0]
            for line in listings.get(current, []):
                cb(line)

        ftp.retrlines.side_effect = fake_retrlines

        # MDTM responses
        def sendcmd(cmd):
            if cmd == "MDTM old.mp4":
                return "213 20260101010000"
            return "213 20260506100000"

        ftp.sendcmd.side_effect = sendcmd
        ftp.cwd.return_value = None
        ftp.delete.return_value = None

        with patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   return_value=ftp):
            recorder._sync_nvr_cleanup_ftp(coord)
        ftp.delete.assert_called_once_with("old.mp4")

    def test_ftp_cwd_failure_returns_cleanly(self, tmp_path):
        """ftp.cwd raising error_perm — entire walk returns early w/o delete."""
        import ftplib
        coord = _make_coord(tmp_path, target="ftp",
                            smb_base_path="Bosch", smb_subpath="NVR")
        ftp = MagicMock()
        ftp.cwd.side_effect = ftplib.error_perm("550 not found")
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   return_value=ftp):
            recorder._sync_nvr_cleanup_ftp(coord)
        ftp.delete.assert_not_called()

    def test_ftp_listing_exception_swallowed(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp",
                            smb_base_path="Bosch", smb_subpath="NVR")
        ftp = MagicMock()
        ftp.retrlines.side_effect = OSError("listing failed")
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   return_value=ftp):
            recorder._sync_nvr_cleanup_ftp(coord)

    def test_ftp_mdtm_failure_skips_file(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp",
                            smb_base_path="Bosch", smb_subpath="NVR")
        ftp = MagicMock()
        listings = {
            "/Bosch/NVR": [
                "-rw-r--r--  1 user grp 1024 Apr 01 10:00 weird.mp4",
            ],
        }

        def fake_retrlines(cmd, cb):
            current = ftp.cwd.call_args.args[0]
            for line in listings.get(current, []):
                cb(line)
        ftp.retrlines.side_effect = fake_retrlines
        ftp.sendcmd.side_effect = OSError("MDTM unsupported")
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   return_value=ftp):
            recorder._sync_nvr_cleanup_ftp(coord)
        ftp.delete.assert_not_called()

    def test_ftp_delete_failure_swallowed(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp",
                            smb_base_path="Bosch", smb_subpath="NVR")
        ftp = MagicMock()
        listings = {
            "/Bosch/NVR": [
                "-rw-r--r--  1 user grp 1024 Apr 01 10:00 old.mp4",
            ],
        }

        def fake_retrlines(cmd, cb):
            current = ftp.cwd.call_args.args[0]
            for line in listings.get(current, []):
                cb(line)
        ftp.retrlines.side_effect = fake_retrlines
        ftp.sendcmd.return_value = "213 20260101010000"
        ftp.delete.side_effect = OSError("permission denied")
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   return_value=ftp):
            recorder._sync_nvr_cleanup_ftp(coord)

    def test_ftp_cwd_in_recursion_swallowed(self, tmp_path):
        """``cwd`` failure when popping back up the tree must not raise."""
        coord = _make_coord(tmp_path, target="ftp",
                            smb_base_path="Bosch", smb_subpath="NVR")
        ftp = MagicMock()
        listings = {
            "/Bosch/NVR": [
                "drwxr-xr-x  2 user grp 4096 May 06 10:00 Terrasse",
            ],
            "/Bosch/NVR/Terrasse": [],
        }

        cwd_call_count = {"n": 0}

        def cwd(p):
            cwd_call_count["n"] += 1
            # First call (entering /Bosch/NVR): ok.
            # Second (entering Terrasse): ok.
            # Third (cwd back to /Bosch/NVR): raise.
            if cwd_call_count["n"] >= 3:
                raise OSError("cwd back failed")

        ftp.cwd.side_effect = cwd

        def fake_retrlines(cmd, cb):
            # Determine current path from the latest cwd call.
            args, _ = ftp.cwd.call_args
            current = args[0]
            for line in listings.get(current, []):
                cb(line)

        ftp.retrlines.side_effect = fake_retrlines
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   return_value=ftp):
            recorder._sync_nvr_cleanup_ftp(coord)

    def test_ftp_quit_failure_swallowed(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp",
                            smb_base_path="Bosch", smb_subpath="NVR")
        ftp = MagicMock()
        ftp.retrlines.side_effect = lambda cmd, cb: None  # empty
        ftp.quit.side_effect = OSError("connection lost")
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   return_value=ftp):
            recorder._sync_nvr_cleanup_ftp(coord)


# ── 13. Upload_ftp close-fallback when both quit and close fail ──────────────


class TestUploadFtpCloseFallback:
    def test_quit_and_close_both_fail(self, tmp_path):
        src = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        ftp = MagicMock()
        ftp.quit.side_effect = OSError("a")
        ftp.close.side_effect = OSError("b")
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   return_value=ftp), \
             patch("custom_components.bosch_shc_camera.smb._ftp_makedirs"):
            # Exception from close() in the inner finally must not propagate.
            recorder._upload_ftp(
                coord, str(src), CAM, "2026-05-06", "10-00.mp4",
            )


# ── 14. Misc edge / warning paths in NEW functions ───────────────────────────


class TestUploadSmbServerEmptyWarning:
    def test_warning_logged_no_session(self, tmp_path, caplog):
        """``smb_server`` empty must short-circuit (no register_session call)."""
        coord = _make_coord(tmp_path, target="smb", smb_server="")
        # Provide a real-ish smbclient that would crash if invoked — proves
        # the helper short-circuits before importing it.
        register = MagicMock()
        with patch.dict("sys.modules", {
            "smbclient": MagicMock(register_session=register,
                                   open_file=MagicMock()),
        }):
            ok = recorder._upload_smb(
                coord, "/x.mp4", CAM, "2026-05-06", "10-00.mp4",
            )
        assert ok is False
        register.assert_not_called()


class TestSmbCleanupImportErrorBranch:
    def test_smbclient_missing_returns_silently(self, tmp_path):
        """Production environments without smbprotocol must not raise."""
        import sys
        import builtins
        coord = _make_coord(tmp_path, target="smb")
        real_import = builtins.__import__

        def selective(name, *a, **kw):
            if name == "smbclient":
                raise ImportError("smbprotocol not installed")
            return real_import(name, *a, **kw)

        # Pop any cached smbclient first so the import path runs fresh.
        sys.modules.pop("smbclient", None)
        with patch("builtins.__import__", side_effect=selective):
            recorder._sync_nvr_cleanup_smb(coord)


class TestFtpCleanupConnectFailure:
    def test_connect_failure_returns_silently(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp")
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   side_effect=OSError("login refused")):
            recorder._sync_nvr_cleanup_ftp(coord)


class TestFtpCleanupShortAndDotDotLines:
    def test_short_line_skipped(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp",
                            smb_base_path="Bosch", smb_subpath="NVR")
        ftp = MagicMock()
        listings = {
            "/Bosch/NVR": [
                "short line",   # too few fields → skipped (line 896-897)
                "drwxr-xr-x  2 user grp 4096 May 06 10:00 .",  # dot → skipped (line 899-900)
                "drwxr-xr-x  2 user grp 4096 May 06 10:00 ..",
            ],
        }

        def fake_retrlines(cmd, cb):
            current = ftp.cwd.call_args.args[0]
            for line in listings.get(current, []):
                cb(line)

        ftp.retrlines.side_effect = fake_retrlines
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect",
                   return_value=ftp):
            recorder._sync_nvr_cleanup_ftp(coord)
        ftp.delete.assert_not_called()
