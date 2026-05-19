"""Coverage tests for recorder.py pre-roll segment helpers.

`_list_preroll_segments` tolerates a file disappearing between `os.listdir`
and `os.stat` (race with concurrent prune) by skipping it. This branch is
not exercised by the regular happy-path recorder tests.
"""

from __future__ import annotations

import os

import pytest

from custom_components.bosch_shc_camera import recorder


def test_list_preroll_skips_oserror_on_stat(monkeypatch, tmp_path):
    """A file that vanishes between `os.listdir` and `os.stat` must be
    skipped silently (race-condition tolerance). Exercises L236-237."""
    good = tmp_path / "000100.mp4"
    bad = tmp_path / "000200.mp4"
    # Both files must be ≥ _PREROLL_MIN_SIZE_BYTES so the size filter doesn't
    # swallow them before reaching the OSError branch.
    payload = b"x" * (recorder._PREROLL_MIN_SIZE_BYTES + 1024)
    good.write_bytes(payload)
    bad.write_bytes(payload)

    real_stat = os.stat

    def _stat(path, *a, **kw):
        # Simulate the bad file being unlinked between listdir + stat.
        if isinstance(path, (str, os.PathLike)) and str(path).endswith("000200.mp4"):
            raise OSError("simulated race — file vanished")
        return real_stat(path, *a, **kw)

    # `os.path.isfile` ALSO calls `os.stat` internally — patching the bare
    # stat raises in isfile too, so the loop body never reaches the explicit
    # `st = os.stat(full)` line we want to cover. Force isfile to True so
    # control reaches the real branch under test.
    monkeypatch.setattr(recorder.os.path, "isfile", lambda _p: True)
    monkeypatch.setattr(recorder.os, "stat", _stat)

    result = recorder._list_preroll_segments(str(tmp_path))
    paths = [p for p, _mt in result]
    assert any(p.endswith("000100.mp4") for p in paths)
    assert not any(p.endswith("000200.mp4") for p in paths)


def test_list_preroll_returns_empty_when_dir_missing(tmp_path):
    """Calling with a nonexistent path returns [] without raising."""
    result = recorder._list_preroll_segments(str(tmp_path / "no_such_dir"))
    assert result == []


def test_list_preroll_returns_empty_on_listdir_error(monkeypatch, tmp_path):
    """`OSError` from `os.listdir` (e.g. EACCES) is swallowed — return []."""
    def _bad_listdir(_p):
        raise OSError("EACCES")
    monkeypatch.setattr(recorder.os, "listdir", _bad_listdir)
    result = recorder._list_preroll_segments(str(tmp_path))
    assert result == []
