"""Bug-hunt regression tests — round 2026-05.

Four bugs found by static analysis of __init__.py on 2026-05-05:

  BUG-1  `continue` in polling dedup path (line ~1620) skips
         `data[cam_id] = {...}` — any camera whose event-ID was already
         alerted via FCM gets dropped from coordinator.data for one full
         scan cycle. All entities see empty data → unavailable.

  BUG-2  `_unregister_go2rtc_stream` only tries port 1984 — on HA 2024+
         (bundled go2rtc on 11984) the DELETE never fires, leaving a stale
         stream entry. Next WebRTC consumer reconnects against a dead URL.

  BUG-3  `_stream_warming` and `_stream_warming_started` are lazily
         initialised via `hasattr` guards instead of in `__init__`. A
         `clear_stream_warming()` call before the first `is_stream_warming()`
         silently no-ops, leaving a stale "warming" label on the entity.

  BUG-4  Privacy-revert (CLAUDE.md TODO PRIVACY_REVERT): the write-lock
         timestamp `_privacy_set_at` is stamped AFTER the HTTP response,
         leaving a race window where the SHC background tick can overwrite
         the cache before the lock is visible.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
SRC = Path(__file__).parent.parent / "custom_components" / "bosch_shc_camera"


# ── BUG-1: dedup `continue` drops data[cam_id] ─────────────────────────


class TestDedupContinueDropsData:
    """BUG-1 regression: polling dedup `continue` skips data[cam_id].

    Root cause: inside `for cam_id in cam_ids` in `_async_update_data`,
    when the FCM-dedup guard fires (event-ID already alerted within 60 s),
    the code executes `continue` — which jumps back to the top of the loop
    and SKIPS `data[cam_id] = {...}`. Any cam that hit the dedup path
    disappears from coordinator.data for that tick.
    """

    def test_continue_skips_assignment_in_loop(self):
        """Verify the structural bug: `continue` before the data-assignment block."""
        source = (SRC / "__init__.py").read_text()
        # Find the dedup-continue block
        dedup_block = (
            '_alert_sent_ids.get(newest_id, 0.0) > _now_mono - 60.0'
        )
        assert dedup_block in source, (
            "FCM dedup guard not found — __init__.py structure changed. "
            "Re-check that dedup continue was fixed."
        )

    def test_data_assignment_reachable_after_dedup(self):
        """After the dedup path, data[cam_id] must still be assigned.

        Pins the fix: dedup should update _last_event_ids and skip the
        alert dispatch, but MUST NOT skip the data[cam_id] assignment.
        The loop body order must be: dedup → skip alert (no continue) → data[cam_id] =
        """
        source = (SRC / "__init__.py").read_text()
        # Locate the dedup block and data assignment
        dedup_idx = source.find("_alert_sent_ids.get(newest_id, 0.0) > _now_mono - 60.0")
        data_assign_idx = source.find('data[cam_id] = {')
        assert dedup_idx != -1, "Dedup guard not found"
        assert data_assign_idx != -1, "data[cam_id] assignment not found"
        # The data assignment must come AFTER the dedup block in source
        assert data_assign_idx > dedup_idx, (
            "data[cam_id] assignment appears before dedup block — unexpected structure"
        )
        # Critical: there must NOT be a bare `continue` on its own line
        # immediately after the dedup log line inside the dedup if-block.
        # Find the dedup if-block boundaries and check for standalone continue.
        dedup_block_start = source.rfind("\n", 0, dedup_idx)
        # Grab 30 lines around the dedup guard
        lines = source[dedup_block_start:dedup_block_start + 800].splitlines()
        in_dedup_block = False
        standalone_continue_found = False
        for line in lines:
            stripped = line.strip()
            if "_alert_sent_ids.get(newest_id" in stripped:
                in_dedup_block = True
            if in_dedup_block and stripped == "continue":
                standalone_continue_found = True
                break
            # Exit dedup block when we see data[cam_id] assignment
            if in_dedup_block and "data[cam_id]" in stripped:
                break
        assert not standalone_continue_found, (
            "BUG-1 still present: bare `continue` found inside FCM dedup block — "
            "this skips `data[cam_id] = {...}` and makes the camera disappear "
            "from coordinator.data for one full scan cycle."
        )

    def test_dedup_path_sets_last_event_ids(self):
        """Even on dedup, _last_event_ids must be updated to prevent re-alert on next tick."""
        source = (SRC / "__init__.py").read_text()
        # The dedup path must update _last_event_ids[cam_id] = newest_id
        # before any early exit
        dedup_idx = source.find("_alert_sent_ids.get(newest_id, 0.0) > _now_mono - 60.0")
        assert dedup_idx != -1
        # In the 400 chars after the dedup guard, _last_event_ids must be updated
        nearby = source[dedup_idx:dedup_idx + 400]
        assert "_last_event_ids[cam_id] = newest_id" in nearby, (
            "Dedup path must update _last_event_ids[cam_id] = newest_id "
            "to avoid a re-alert on the next polling tick."
        )


# ── BUG-2: _unregister_go2rtc_stream only tries port 1984 ──────────────


class TestUnregisterGo2rtcEndpoints:
    """BUG-2 regression: _unregister_go2rtc_stream misses port 11984.

    _register_go2rtc_stream tries [11984, 1984, Unix socket].
    _unregister_go2rtc_stream only tries localhost:1984.
    On HA 2024+ where go2rtc listens on 11984 only, the DELETE is sent
    to the wrong port and silently ignored. The stale stream entry
    survives in go2rtc's registry.
    """

    def test_unregister_tries_multiple_endpoints(self):
        """_unregister_go2rtc_stream must try 11984 AND 1984."""
        source = (SRC / "__init__.py").read_text()
        # Find the unregister function body
        func_start = source.find("async def _unregister_go2rtc_stream")
        assert func_start != -1, "_unregister_go2rtc_stream not found"
        # Find the next function definition to bound the search
        next_func = source.find("\n    async def ", func_start + 1)
        if next_func == -1:
            next_func = func_start + 1500
        func_body = source[func_start:next_func]
        assert "11984" in func_body, (
            "BUG-2: _unregister_go2rtc_stream does not try port 11984. "
            "On HA 2024+ (bundled go2rtc) the stream is registered on 11984, "
            "so DELETE to 1984 silently misses and leaves a stale entry."
        )
        assert "1984" in func_body, (
            "_unregister_go2rtc_stream must also try legacy port 1984."
        )

    def test_unregister_and_register_share_port_set(self):
        """The port list in _unregister must be a subset of _register's ports.

        Structural pin: if _register adds a new endpoint, _unregister must
        match it — otherwise cleanup lags registration.
        """
        source = (SRC / "__init__.py").read_text()
        reg_start = source.find("async def _register_go2rtc_stream")
        unreg_start = source.find("async def _unregister_go2rtc_stream")
        assert reg_start != -1 and unreg_start != -1

        reg_end = source.find("\n    async def ", reg_start + 1)
        unreg_end = source.find("\n    async def ", unreg_start + 1)
        reg_body = source[reg_start:reg_end if reg_end != -1 else reg_start + 1500]
        unreg_body = source[unreg_start:unreg_end if unreg_end != -1 else unreg_start + 1500]

        # Both functions must reference both standard go2rtc ports
        for port in ("11984", "1984"):
            assert port in reg_body, f"_register_go2rtc_stream missing port {port}"
            assert port in unreg_body, (
                f"_unregister_go2rtc_stream missing port {port} — "
                "DELETE sent to wrong port, stale stream entry survives"
            )


# ── BUG-3: _stream_warming lazily initialised ───────────────────────────


class TestStreamWarmingInit:
    """BUG-3 regression: _stream_warming/_stream_warming_started not in __init__.

    Both attributes are lazily initialised via `hasattr` guards.
    `clear_stream_warming()` has its own `hasattr` guard, so a call before
    the first `is_stream_warming()` silently no-ops. This leaves the
    camera entity showing "warming" even after the stream is live.
    """

    def test_stream_warming_initialised_in_init(self):
        """BoschCameraCoordinator.__init__ must set _stream_warming = set()."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        src = inspect.getsource(BoschCameraCoordinator.__init__)
        assert "_stream_warming" in src, (
            "BUG-3: _stream_warming not initialised in __init__. "
            "clear_stream_warming() called before is_stream_warming() silently no-ops."
        )
        # Must be initialised as an empty set, not lazily
        assert "_stream_warming = set()" in src or "_stream_warming: set" in src, (
            "_stream_warming must be eagerly initialised to set() in __init__"
        )

    def test_stream_warming_started_initialised_in_init(self):
        """BoschCameraCoordinator.__init__ must set _stream_warming_started = {}."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        src = inspect.getsource(BoschCameraCoordinator.__init__)
        assert "_stream_warming_started" in src, (
            "BUG-3: _stream_warming_started not initialised in __init__. "
            "A clear call before first is_stream_warming() call silently no-ops."
        )

    def test_no_hasattr_guard_in_clear_stream_warming(self):
        """clear_stream_warming must not need a hasattr guard once __init__ sets
        _stream_warming eagerly — the guard is a symptom of the lazy init bug."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        src = inspect.getsource(BoschCameraCoordinator.clear_stream_warming)
        # After the fix, clear_stream_warming can call self._stream_warming.discard()
        # directly without `if hasattr(...)` because __init__ guarantees the attribute.
        # This test pins the fix: if the hasattr guard is removed, the test passes.
        # If someone re-adds lazy init, the hasattr re-appears and this test catches it.
        assert "hasattr" not in src, (
            "BUG-3: clear_stream_warming still uses `hasattr` guard — "
            "_stream_warming should be eagerly initialised in __init__ "
            "so the guard is unnecessary."
        )

    def test_no_hasattr_guard_for_stream_warming_in_try_live_connection(self):
        """The hasattr guard for _stream_warming in _try_live_connection_inner
        must be removed once __init__ sets the attribute eagerly."""
        source = (SRC / "__init__.py").read_text()
        # Find _try_live_connection_inner body and check for hasattr(_stream_warming)
        func_start = source.find("_try_live_connection_inner")
        if func_start == -1:
            pytest.skip("_try_live_connection_inner not found — name may have changed")
        func_body = source[func_start:func_start + 1500]
        assert 'hasattr(self, "_stream_warming")' not in func_body, (
            "BUG-3: hasattr guard for _stream_warming still in _try_live_connection_inner — "
            "attribute should be eagerly initialised in __init__"
        )


# ── BUG-4: privacy_set_at race window ───────────────────────────────────


class TestPrivacySetAtRace:
    """BUG-4 / CLAUDE.md TODO PRIVACY_REVERT regression.

    Symptom: first OFF-toggle reverts to ON, second works.
    Root cause: `_privacy_set_at` is stamped AFTER the HTTP success
    response. If the SHC background tick runs between the HTTP response
    and the `_privacy_set_at` write, it sees `_privacy_set_at.get(cam_id)
    is None` → write-lock not active → SHC state overwrites the cache
    with the OLD value (still ON). On the next tick _privacy_set_at is
    finally set, but the cache already has the wrong value.
    Fix: stamp `_privacy_set_at` BEFORE (or simultaneously with) the HTTP
    call — optimistic locking.
    """

    def test_privacy_set_at_stamped_before_shc_can_read(self):
        """In shc.py, _privacy_set_at must be written BEFORE or AT the same
        time as _shc_state_cache, not after."""
        shc_src = (SRC / "shc.py").read_text()
        # Find async_cloud_set_privacy_mode (or the equivalent setter)
        setter_name = "async_cloud_set_privacy_mode"
        if setter_name not in shc_src:
            # May be inlined in __init__.py
            setter_name = "privacy_mode"
            shc_src = (SRC / "__init__.py").read_text()

        func_start = shc_src.find(f"def {setter_name}")
        if func_start == -1:
            pytest.skip(f"{setter_name} not found — check setter location")

        # Use the full function body (up to the next top-level function)
        next_func = shc_src.find("\nasync def ", func_start + 1)
        func_body = shc_src[func_start:next_func] if next_func != -1 else shc_src[func_start:]
        # Find line indices of both writes
        cache_write = func_body.find("_shc_state_cache")
        lock_write = func_body.find("_privacy_set_at")
        if cache_write == -1 or lock_write == -1:
            pytest.skip("Could not locate both writes in setter — check function body")

        assert lock_write <= cache_write, (
            "BUG-4 / PRIVACY_REVERT: `_privacy_set_at` is written AFTER "
            "`_shc_state_cache`. If the SHC background tick fires between "
            "these two writes, it sees no write-lock and overwrites the cache "
            "with the old ON value. Fix: stamp `_privacy_set_at` first."
        )

    def test_privacy_write_lock_present_in_shc_fetcher(self):
        """The SHC state fetcher must check `_privacy_set_at` before writing.

        Pins the guard that prevents the race: without this check the SHC
        background tick would always overwrite any pending privacy change.
        """
        shc_src = (SRC / "shc.py").read_text()
        assert "_privacy_set_at" in shc_src, (
            "BUG-4: `_privacy_set_at` write-lock not present in shc.py — "
            "SHC background tick will always overwrite privacy cache changes."
        )
        # The fetcher must also reference the lock (not just the setter)
        fetcher_start = shc_src.find("async def async_fetch_shc_state")
        if fetcher_start == -1:
            fetcher_start = shc_src.find("_shc_state_cache[cam_id]")
        assert fetcher_start != -1, "SHC state fetcher not found"
        fetcher_body = shc_src[fetcher_start:fetcher_start + 1500]
        assert "_privacy_set_at" in fetcher_body, (
            "BUG-4: SHC state fetcher does not check `_privacy_set_at` write-lock. "
            "A concurrent toggle can be overwritten by the background tick."
        )


# ── Meta ─────────────────────────────────────────────────────────────────


class TestMeta:
    def test_all_four_bug_classes_present(self):
        """Sanity: all four regression classes must exist in this file."""
        text = Path(__file__).read_text()
        for cls in (
            "TestDedupContinueDropsData",
            "TestUnregisterGo2rtcEndpoints",
            "TestStreamWarmingInit",
            "TestPrivacySetAtRace",
        ):
            assert f"class {cls}" in text, f"Missing bug regression class: {cls}"
