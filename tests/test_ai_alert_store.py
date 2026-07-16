"""Tests for ai_alert_store.py — AI Camera Analysis alert persistence
(JSONL + snapshot images, age-based retention).

Mirrors `test_recorder.py`'s house style: real ``tmp_path``-based
filesystem tests for the blocking-I/O sync helpers (this module's whole
job is disk I/O — mocking `os` calls would test nothing real), plus a
``SimpleNamespace`` coordinator stub whose ``async_add_executor_job``
actually runs the function in-thread (matching `_make_preroll_coord`'s
``_run_executor`` pattern) for the async round-trip tests.

Covers every branch (PIN_EVERY_MODE):
  - ``_cam_dir`` / ``_ts_to_filename`` — path construction, timestamp
    sanitization (colon/dot → dash).
  - ``_sync_append_and_prune`` — dir creation, image write success/failure
    (swallowed), JSONL append success/failure (swallowed, still returns
    image_path), retention_days<=0 skip vs >0 triggers prune, cam_dir
    creation failure returns None early.
  - ``_sync_prune`` — no file, all-kept (no rewrite, mtime unchanged),
    some pruned (images deleted, kept records verbatim), malformed JSON
    line kept not dropped, missing/malformed generated_at kept, rewrite
    failure leaves the original file intact.
  - ``async_store_alert`` — real image round-trip via a real executor,
    no-image path, ``ai_analysis_recent`` cache append + cap eviction.
  - ``recent_alerts`` — empty/in-window/out-of-window/minutes<=0/malformed
    generated_at.
  - ``async_load_recent_alerts`` — rebuild from real on-disk files,
    missing file per-camera, empty coordinator.data.
  - ``_sync_read_recent_tail`` — tail-only read past the cap, missing
    file, corrupt lines skipped individually, non-string generated_at /
    non-numeric score skipped.

Source: new module, zero prior test coverage.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.bosch_shc_camera import ai_alert_store
from custom_components.bosch_shc_camera.const import CONF_AI_ANALYSIS_RETENTION_DAYS

CAM_ID = "cam-1"
CAM_TITLE = "Terrasse"


def _make_coord(tmp_path: Path, *, retention_days: int = 30) -> SimpleNamespace:
    """Stub coordinator with the fields `ai_alert_store` reads, matching
    `_make_preroll_coord`'s `_run_executor` pattern (actually runs the
    function in-thread, not a no-op mock)."""

    async def _run_executor(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    coord = SimpleNamespace(
        data={CAM_ID: {"info": {"title": CAM_TITLE}, "status": "ONLINE"}},
        options={CONF_AI_ANALYSIS_RETENTION_DAYS: retention_days},
        ai_analysis_recent={},
    )
    coord.hass = SimpleNamespace(
        async_add_executor_job=_run_executor,
        config=SimpleNamespace(path=lambda *p: str(tmp_path.joinpath(*p))),
    )
    return coord


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


def _now_iso(offset_seconds: float = 0.0) -> str:
    return _iso(
        datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=offset_seconds)
    )


# ── _cam_dir / _ts_to_filename ─────────────────────────────────────────────


class TestCamDir:
    def test_builds_expected_layout(self):
        result = ai_alert_store._cam_dir("/config", "Terrasse")
        assert result == os.path.join(
            "/config", ".storage", "bosch_shc_camera", "ai_alerts", "Terrasse"
        )

    def test_sanitizes_unsafe_camera_name(self):
        # _safe_name (smb.py) strips path-traversal + unsafe chars.
        result = ai_alert_store._cam_dir("/config", "../../etc/passwd")
        assert ".." not in result
        assert "/etc/passwd" not in result


class TestTsToFilename:
    def test_replaces_colons_and_dots(self):
        assert (
            ai_alert_store._ts_to_filename("2026-07-16T12:34:56.789012+00:00")
            == "2026-07-16T12-34-56-789012+00-00.jpg"
        )

    def test_no_special_chars_passthrough(self):
        assert (
            ai_alert_store._ts_to_filename("20260716-123456") == "20260716-123456.jpg"
        )

    def test_empty_string(self):
        assert ai_alert_store._ts_to_filename("") == ".jpg"


# ── _sync_append_and_prune ─────────────────────────────────────────────────


class TestSyncAppendAndPrune:
    def test_creates_fresh_dir_and_writes_image_and_jsonl(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        record = {"generated_at": "2026-07-16T10-00-00", "score": 5}
        result = ai_alert_store._sync_append_and_prune(
            cam_dir, record, b"\xff\xd8\xff-fake-jpeg", retention_days=30
        )
        assert result == os.path.join("images", "2026-07-16T10-00-00.jpg")
        image_full = os.path.join(cam_dir, result)
        assert os.path.exists(image_full)
        with open(image_full, "rb") as f:
            assert f.read() == b"\xff\xd8\xff-fake-jpeg"

        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
        with open(jsonl_path, encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["score"] == 5
        assert rec["image_path"] == result

    def test_no_image_bytes_writes_record_with_none_path(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        record = {"generated_at": "2026-07-16T10-00-00", "score": 1}
        result = ai_alert_store._sync_append_and_prune(
            cam_dir, record, None, retention_days=30
        )
        assert result is None
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
        with open(jsonl_path, encoding="utf-8") as f:
            rec = json.loads(f.readline())
        assert rec["image_path"] is None

    def test_image_write_failure_swallowed_jsonl_still_written(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        record = {"generated_at": "2026-07-16T10-00-00", "score": 2}

        # Patch `open` narrowly so only the image write (inside images_dir)
        # fails — the subsequent JSONL append must still use the real open().
        real_open = open
        image_path_marker = os.path.join(cam_dir, ai_alert_store._IMAGES_DIRNAME)

        def flaky_open(path, mode="r", *args, **kwargs):
            if str(path).startswith(image_path_marker) and "wb" in mode:
                raise OSError("disk full")
            return real_open(path, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=flaky_open):
            result = ai_alert_store._sync_append_and_prune(
                cam_dir, record, b"some-bytes", retention_days=30
            )

        assert result is None
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
        with open(jsonl_path, encoding="utf-8") as f:
            rec = json.loads(f.readline())
        assert rec["image_path"] is None
        assert rec["score"] == 2

    def test_jsonl_append_failure_swallowed_returns_image_path_anyway(
        self, tmp_path: Path
    ):
        cam_dir = str(tmp_path / "Cam")
        record = {"generated_at": "2026-07-16T10-00-00", "score": 3}
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)

        real_open = open

        def flaky_open(path, mode="r", *args, **kwargs):
            if str(path) == jsonl_path and mode == "a":
                raise OSError("read-only filesystem")
            return real_open(path, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=flaky_open):
            result = ai_alert_store._sync_append_and_prune(
                cam_dir, record, b"img-bytes", retention_days=30
            )

        # Image was written successfully before the JSONL append failed.
        assert result == os.path.join("images", "2026-07-16T10-00-00.jpg")
        assert not os.path.exists(jsonl_path)

    def test_retention_zero_skips_prune(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        record = {"generated_at": _now_iso(), "score": 1}
        with patch.object(ai_alert_store, "_sync_prune") as prune:
            ai_alert_store._sync_append_and_prune(
                cam_dir, record, None, retention_days=0
            )
            prune.assert_not_called()

    def test_retention_positive_triggers_prune(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        record = {"generated_at": _now_iso(), "score": 1}
        with patch.object(ai_alert_store, "_sync_prune") as prune:
            ai_alert_store._sync_append_and_prune(
                cam_dir, record, None, retention_days=30
            )
            prune.assert_called_once_with(cam_dir, 30)

    def test_cam_dir_creation_failure_returns_none_early(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        record = {"generated_at": _now_iso(), "score": 1}

        with (
            patch("os.makedirs", side_effect=OSError("permission denied")),
            patch.object(ai_alert_store, "_sync_prune") as prune,
        ):
            result = ai_alert_store._sync_append_and_prune(
                cam_dir, record, b"bytes", retention_days=30
            )

        assert result is None
        prune.assert_not_called()
        assert not os.path.exists(os.path.join(cam_dir, ai_alert_store._JSONL_NAME))


# ── _sync_prune ─────────────────────────────────────────────────────────────


class TestSyncPrune:
    def test_no_file_is_noop(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        os.makedirs(cam_dir)
        # Must not raise.
        ai_alert_store._sync_prune(cam_dir, 30)
        assert not os.path.exists(os.path.join(cam_dir, ai_alert_store._JSONL_NAME))

    def test_all_within_retention_skips_rewrite(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        os.makedirs(cam_dir)
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
        content = (
            json.dumps({"generated_at": _now_iso(), "score": 1})
            + "\n"
            + json.dumps({"generated_at": _now_iso(), "score": 2})
            + "\n"
        )
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(content)
        mtime_before = os.stat(jsonl_path).st_mtime

        # Ensure the rewrite path would be observable via mtime change.
        time.sleep(0.01)
        ai_alert_store._sync_prune(cam_dir, 30)

        mtime_after = os.stat(jsonl_path).st_mtime
        assert mtime_after == mtime_before, (
            "file must not be rewritten when nothing is pruned"
        )
        with open(jsonl_path, encoding="utf-8") as f:
            assert f.read() == content

    def test_some_pruned_deletes_images_keeps_rest_verbatim(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        images_dir = os.path.join(cam_dir, ai_alert_store._IMAGES_DIRNAME)
        os.makedirs(images_dir)
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)

        old_dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=100)
        old_image_rel = os.path.join("images", "old.jpg")
        old_image_full = os.path.join(cam_dir, old_image_rel)
        with open(old_image_full, "wb") as f:
            f.write(b"old")

        old_rec = {
            "generated_at": _iso(old_dt),
            "score": 1,
            "image_path": old_image_rel,
        }
        kept_rec = {"generated_at": _now_iso(), "score": 9, "image_path": None}

        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(old_rec) + "\n")
            f.write(json.dumps(kept_rec) + "\n")

        ai_alert_store._sync_prune(cam_dir, retention_days=30)

        assert not os.path.exists(old_image_full), "orphaned image must be deleted"
        with open(jsonl_path, encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0]) == kept_rec

    def test_malformed_json_line_kept_not_dropped(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        os.makedirs(cam_dir)
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)

        old_dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=100)
        old_rec = {"generated_at": _iso(old_dt), "score": 1}
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write("{not valid json at all\n")
            f.write(json.dumps(old_rec) + "\n")

        ai_alert_store._sync_prune(cam_dir, retention_days=30)

        with open(jsonl_path, encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        # The malformed line must survive (kept, not silently dropped); the
        # valid-but-old record gets pruned away since it parses cleanly.
        assert lines == ["{not valid json at all"]

    def test_missing_generated_at_kept(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        os.makedirs(cam_dir)
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
        rec_no_ts = {"score": 1}
        old_dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=100)
        old_rec = {"generated_at": _iso(old_dt), "score": 2}
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec_no_ts) + "\n")
            f.write(json.dumps(old_rec) + "\n")

        ai_alert_store._sync_prune(cam_dir, retention_days=30)

        with open(jsonl_path, encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0]) == rec_no_ts

    def test_malformed_generated_at_string_kept(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        os.makedirs(cam_dir)
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
        rec_bad_ts = {"generated_at": "not-a-timestamp", "score": 1}
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec_bad_ts) + "\n")

        ai_alert_store._sync_prune(cam_dir, retention_days=30)

        with open(jsonl_path, encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0]) == rec_bad_ts

    def test_rewrite_failure_leaves_original_file_intact(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        os.makedirs(cam_dir)
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)

        old_dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=100)
        recent_rec = {"generated_at": _now_iso(), "score": 1}
        old_rec = {"generated_at": _iso(old_dt), "score": 2}
        original_content = json.dumps(recent_rec) + "\n" + json.dumps(old_rec) + "\n"
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(original_content)

        real_open = open

        def flaky_open(path, mode="r", *args, **kwargs):
            if str(path) == jsonl_path + ".tmp":
                raise OSError("no space left on device")
            return real_open(path, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=flaky_open):
            # Must not raise.
            ai_alert_store._sync_prune(cam_dir, retention_days=30)

        with open(jsonl_path, encoding="utf-8") as f:
            assert f.read() == original_content
        assert not os.path.exists(jsonl_path + ".tmp")

    def test_blank_lines_in_source_file_skipped(self, tmp_path: Path):
        """A blank line in alerts.jsonl (e.g. leftover from an interrupted
        write) must be skipped, not treated as a record to parse/keep."""
        cam_dir = str(tmp_path / "Cam")
        os.makedirs(cam_dir)
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
        old_ts = _now_iso(-3600 * 24 * 60)
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write("\n")
            f.write(json.dumps({"generated_at": old_ts, "score": 1}) + "\n")

        ai_alert_store._sync_prune(cam_dir, retention_days=30)

        with open(jsonl_path, encoding="utf-8") as f:
            content = f.read()
        assert content == ""  # the old record was pruned, blank line ignored

    def test_dropped_image_unlink_failure_swallowed(self, tmp_path: Path):
        """A pruned record's image file failing to delete (e.g. already
        gone, permission denied) must not raise — the JSONL rewrite has
        already succeeded and is the source of truth."""
        cam_dir = str(tmp_path / "Cam")
        images_dir = os.path.join(cam_dir, ai_alert_store._IMAGES_DIRNAME)
        os.makedirs(images_dir)
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
        old_ts = _now_iso(-3600 * 24 * 60)  # 60 days old
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "generated_at": old_ts,
                        "score": 1,
                        "image_path": os.path.join(
                            ai_alert_store._IMAGES_DIRNAME, "missing.jpg"
                        ),
                    }
                )
                + "\n"
            )
        # Deliberately do NOT create missing.jpg — the unlink() call below
        # must raise FileNotFoundError internally and swallow it.

        ai_alert_store._sync_prune(cam_dir, retention_days=30)

        with open(jsonl_path, encoding="utf-8") as f:
            assert f.read() == ""


# ── async_store_alert ───────────────────────────────────────────────────────


class TestAsyncStoreAlert:
    @pytest.mark.asyncio
    async def test_full_round_trip_with_real_image_bytes(self, tmp_path: Path):
        coord = _make_coord(tmp_path)
        result = await ai_alert_store.async_store_alert(
            coord,
            CAM_ID,
            {"score": 7, "summary": "person detected"},
            _now_iso(),
            b"\xff\xd8\xff-real-jpeg-bytes",
        )
        assert result is not None
        assert result["image_path"] is not None

        cam_dir = ai_alert_store._cam_dir(str(tmp_path), CAM_TITLE)
        image_full = os.path.join(cam_dir, result["image_path"])
        assert os.path.exists(image_full)
        with open(image_full, "rb") as f:
            assert f.read() == b"\xff\xd8\xff-real-jpeg-bytes"

        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
        with open(jsonl_path, encoding="utf-8") as f:
            rec = json.loads(f.readline())
        assert rec["score"] == 7
        assert rec["summary"] == "person detected"
        assert rec["cam_id"] == CAM_ID

    @pytest.mark.asyncio
    async def test_no_image_bytes_still_writes_jsonl_record(self, tmp_path: Path):
        coord = _make_coord(tmp_path)
        result = await ai_alert_store.async_store_alert(
            coord, CAM_ID, {"score": 0}, _now_iso(), None
        )
        assert result == {"image_path": None}

        cam_dir = ai_alert_store._cam_dir(str(tmp_path), CAM_TITLE)
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
        with open(jsonl_path, encoding="utf-8") as f:
            rec = json.loads(f.readline())
        assert rec["image_path"] is None

    @pytest.mark.asyncio
    async def test_updates_recent_cache_appends(self, tmp_path: Path):
        coord = _make_coord(tmp_path)
        ts1 = _now_iso(-10)
        ts2 = _now_iso()
        await ai_alert_store.async_store_alert(coord, CAM_ID, {"score": 1}, ts1, None)
        await ai_alert_store.async_store_alert(coord, CAM_ID, {"score": 2}, ts2, None)

        assert coord.ai_analysis_recent[CAM_ID] == [(ts1, 1), (ts2, 2)]

    @pytest.mark.asyncio
    async def test_recent_cache_capped_drops_oldest(self, tmp_path: Path):
        coord = _make_coord(tmp_path)
        cap = ai_alert_store._RECENT_CACHE_MAX_PER_CAM
        for i in range(cap + 5):
            await ai_alert_store.async_store_alert(
                coord, CAM_ID, {"score": i}, _now_iso(i), None
            )

        recent = coord.ai_analysis_recent[CAM_ID]
        assert len(recent) == cap
        # Oldest 5 entries (scores 0..4) dropped from the in-memory list —
        # the disk file itself is untouched by this cap (it's an in-memory
        # concern only).
        scores = [score for _, score in recent]
        assert scores == list(range(5, cap + 5))


# ── recent_alerts ────────────────────────────────────────────────────────────


class TestRecentAlerts:
    def _coord(self) -> SimpleNamespace:
        return SimpleNamespace(ai_analysis_recent={})

    def test_empty_cache_returns_empty(self):
        coord = self._coord()
        assert ai_alert_store.recent_alerts(coord, CAM_ID, minutes=30) == []

    def test_all_entries_within_window(self):
        coord = self._coord()
        ts1 = _now_iso(-5)
        ts2 = _now_iso(-1)
        coord.ai_analysis_recent[CAM_ID] = [(ts1, 1), (ts2, 2)]
        result = ai_alert_store.recent_alerts(coord, CAM_ID, minutes=30)
        assert result == [(ts1, 1), (ts2, 2)]

    def test_entries_outside_window_filtered(self):
        coord = self._coord()
        old_ts = _iso(
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=60)
        )
        recent_ts = _now_iso()
        coord.ai_analysis_recent[CAM_ID] = [(old_ts, 1), (recent_ts, 2)]
        result = ai_alert_store.recent_alerts(coord, CAM_ID, minutes=30)
        assert result == [(recent_ts, 2)]

    def test_minutes_zero_or_negative_returns_empty_immediately(self):
        coord = self._coord()
        coord.ai_analysis_recent[CAM_ID] = [(_now_iso(), 1)]
        assert ai_alert_store.recent_alerts(coord, CAM_ID, minutes=0) == []
        assert ai_alert_store.recent_alerts(coord, CAM_ID, minutes=-5) == []

    def test_malformed_generated_at_skipped_not_raised(self):
        coord = self._coord()
        good_ts = _now_iso()
        coord.ai_analysis_recent[CAM_ID] = [("not-a-timestamp", 1), (good_ts, 2)]
        result = ai_alert_store.recent_alerts(coord, CAM_ID, minutes=30)
        assert result == [(good_ts, 2)]


# ── async_load_recent_alerts ────────────────────────────────────────────────


class TestAsyncLoadRecentAlerts:
    @pytest.mark.asyncio
    async def test_rebuilds_cache_from_disk_for_multiple_cameras(self, tmp_path: Path):
        coord = _make_coord(tmp_path)
        coord.data = {
            CAM_ID: {"info": {"title": "Terrasse"}, "status": "ONLINE"},
            "cam-2": {"info": {"title": "Innenbereich"}, "status": "ONLINE"},
        }

        for _cam_id, title in (("cam-1", "Terrasse"), ("cam-2", "Innenbereich")):
            cam_dir = ai_alert_store._cam_dir(str(tmp_path), title)
            os.makedirs(cam_dir)
            jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
            with open(jsonl_path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": _now_iso(), "score": 3}) + "\n")

        await ai_alert_store.async_load_recent_alerts(coord)

        assert coord.ai_analysis_recent["cam-1"][0][1] == 3
        assert coord.ai_analysis_recent["cam-2"][0][1] == 3

    @pytest.mark.asyncio
    async def test_missing_file_for_camera_skipped_gracefully(self, tmp_path: Path):
        coord = _make_coord(tmp_path)
        # No file on disk for CAM_ID at all.
        await ai_alert_store.async_load_recent_alerts(coord)
        assert coord.ai_analysis_recent == {}

    @pytest.mark.asyncio
    async def test_empty_coordinator_data_is_noop(self, tmp_path: Path):
        coord = _make_coord(tmp_path)
        coord.data = {}
        await ai_alert_store.async_load_recent_alerts(coord)
        assert coord.ai_analysis_recent == {}

    @pytest.mark.asyncio
    async def test_one_camera_executor_failure_does_not_abort_others(
        self, tmp_path: Path
    ):
        """A per-camera executor-job failure (real production risk: a
        transient disk error on one camera's tail-read) must not abort the
        whole cache warm-up for every OTHER camera — this is the exact
        guard added after a CI regression where a bare `await` here could
        crash `async_setup_entry` on a loosely-mocked `hass` in unrelated
        tests."""
        coord = _make_coord(tmp_path)
        coord.data = {
            CAM_ID: {"info": {"title": "Terrasse"}, "status": "ONLINE"},
            "cam-2": {"info": {"title": "Innenbereich"}, "status": "ONLINE"},
        }
        good_dir = ai_alert_store._cam_dir(str(tmp_path), "Innenbereich")
        os.makedirs(good_dir)
        with open(
            os.path.join(good_dir, ai_alert_store._JSONL_NAME), "w", encoding="utf-8"
        ) as f:
            f.write(json.dumps({"generated_at": _now_iso(), "score": 9}) + "\n")

        call_count = 0

        async def _flaky_executor(fn, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("transient disk error")
            return fn(*args, **kwargs)

        coord.hass.async_add_executor_job = _flaky_executor

        await ai_alert_store.async_load_recent_alerts(coord)

        assert "cam-2" in coord.ai_analysis_recent
        assert coord.ai_analysis_recent["cam-2"][0][1] == 9


# ── _sync_read_recent_tail ──────────────────────────────────────────────────


class TestSyncReadRecentTail:
    def test_missing_file_returns_empty_list(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        assert ai_alert_store._sync_read_recent_tail(cam_dir) == []

    def test_reads_only_tail_when_more_than_cap_lines(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        os.makedirs(cam_dir)
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
        cap = ai_alert_store._RECENT_CACHE_MAX_PER_CAM
        total = cap + 10
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for i in range(total):
                f.write(json.dumps({"generated_at": _now_iso(i), "score": i}) + "\n")

        result = ai_alert_store._sync_read_recent_tail(cam_dir)

        assert len(result) == cap
        scores = [score for _, score in result]
        # Only the LAST `cap` lines are kept — the oldest 10 are excluded.
        assert scores == list(range(10, total))

    def test_corrupt_line_skipped_rest_survives(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        os.makedirs(cam_dir)
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
        good_ts = _now_iso()
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write("{not valid json\n")
            f.write(json.dumps({"generated_at": good_ts, "score": 5}) + "\n")

        result = ai_alert_store._sync_read_recent_tail(cam_dir)
        assert result == [(good_ts, 5)]

    def test_non_string_generated_at_skipped(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        os.makedirs(cam_dir)
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
        good_ts = _now_iso()
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"generated_at": 12345, "score": 1}) + "\n")
            f.write(json.dumps({"generated_at": good_ts, "score": 2}) + "\n")

        result = ai_alert_store._sync_read_recent_tail(cam_dir)
        assert result == [(good_ts, 2)]

    def test_non_numeric_score_skipped(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        os.makedirs(cam_dir)
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
        good_ts = _now_iso()
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(
                json.dumps({"generated_at": _now_iso(-1), "score": "not-a-number"})
                + "\n"
            )
            f.write(json.dumps({"generated_at": good_ts, "score": 2}) + "\n")

        result = ai_alert_store._sync_read_recent_tail(cam_dir)
        assert result == [(good_ts, 2)]

    def test_blank_lines_ignored(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        os.makedirs(cam_dir)
        jsonl_path = os.path.join(cam_dir, ai_alert_store._JSONL_NAME)
        good_ts = _now_iso()
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write("\n")
            f.write(json.dumps({"generated_at": good_ts, "score": 1}) + "\n")
            f.write("   \n")

        result = ai_alert_store._sync_read_recent_tail(cam_dir)
        assert result == [(good_ts, 1)]


# ── _sync_read_image / async_read_alert_image ──────────────────────────────


class TestSyncReadImage:
    def test_reads_existing_image_bytes(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        images_dir = os.path.join(cam_dir, ai_alert_store._IMAGES_DIRNAME)
        os.makedirs(images_dir)
        rel = os.path.join(ai_alert_store._IMAGES_DIRNAME, "alert.jpg")
        with open(os.path.join(cam_dir, rel), "wb") as f:
            f.write(b"jpeg-bytes")

        result = ai_alert_store._sync_read_image(cam_dir, rel)
        assert result == b"jpeg-bytes"

    def test_missing_file_returns_none(self, tmp_path: Path):
        cam_dir = str(tmp_path / "Cam")
        os.makedirs(cam_dir)
        result = ai_alert_store._sync_read_image(
            cam_dir, os.path.join(ai_alert_store._IMAGES_DIRNAME, "nope.jpg")
        )
        assert result is None

    def test_path_traversal_rejected(self, tmp_path: Path):
        """An `image_path` that resolves OUTSIDE `cam_dir` (e.g. via `..`
        segments) must be rejected, not read — same discipline as this
        repo's media_source path-traversal guard."""
        cam_dir = str(tmp_path / "Cam")
        os.makedirs(cam_dir)
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"outside cam_dir")

        result = ai_alert_store._sync_read_image(cam_dir, "../secret.txt")
        assert result is None


class TestAsyncReadAlertImage:
    @pytest.mark.asyncio
    async def test_falsy_image_path_returns_none_without_touching_disk(
        self, tmp_path: Path
    ):
        coord = _make_coord(tmp_path)
        result = await ai_alert_store.async_read_alert_image(coord, CAM_ID, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_string_image_path_returns_none(self, tmp_path: Path):
        coord = _make_coord(tmp_path)
        result = await ai_alert_store.async_read_alert_image(coord, CAM_ID, "")
        assert result is None

    @pytest.mark.asyncio
    async def test_full_round_trip_reads_real_bytes(self, tmp_path: Path):
        coord = _make_coord(tmp_path)
        cam_dir = ai_alert_store._cam_dir(str(tmp_path), CAM_TITLE)
        images_dir = os.path.join(cam_dir, ai_alert_store._IMAGES_DIRNAME)
        os.makedirs(images_dir)
        rel = os.path.join(ai_alert_store._IMAGES_DIRNAME, "alert.jpg")
        with open(os.path.join(cam_dir, rel), "wb") as f:
            f.write(b"real-jpeg-bytes")

        result = await ai_alert_store.async_read_alert_image(coord, CAM_ID, rel)
        assert result == b"real-jpeg-bytes"

    @pytest.mark.asyncio
    async def test_unknown_camera_falls_back_to_cam_id_dir_returns_none(
        self, tmp_path: Path
    ):
        """A cam_id not present in `coordinator.data` still resolves a
        (nonexistent) directory via the `.get(cam_id, cam_id)` fallback —
        must return None, not raise."""
        coord = _make_coord(tmp_path)
        result = await ai_alert_store.async_read_alert_image(
            coord, "unknown-cam", "images/x.jpg"
        )
        assert result is None
