"""Regression tests for SMB credit-starvation bug (2026-05-15).

User-observed in production HA logs 2026-05-14 22:54 UTC:
9× ``smbprotocol.exceptions.SMBException: Request requires 1 credits but only 0
credits are available`` when the browser fired parallel HTTP Range requests
against the media-source ``/api/bosch_shc_camera/event/...`` endpoint to play
a video clip from the NAS.

Root cause (research findings in knowledge-base/smb-credit-starvation.md):
the integration registered ONE smbclient session via ``register_session()``
without a custom ``connection_cache``. smbclient's global cache then served
every concurrent executor thread the SAME ``Connection`` object, whose 64-credit
SMB2 sequence-window drained faster than responses replenished it.

Fix recommended by smbprotocol author (jborean93) in
https://github.com/jborean93/smbprotocol/issues/312#issuecomment-3027461329:
each concurrent worker passes its own ``connection_cache={}`` dict. A fresh
dict forces a new ``Connection`` object with its own credit window.

These tests pin the behaviour so future refactors cannot reintroduce the
shared-cache pattern:

  * Every SMB operation (``stat``, ``open_file``, ``scandir``) is invoked
    with an explicit ``connection_cache`` kwarg.
  * Two ``open_file()`` calls produce two DISTINCT cache dicts (not the
    same dict reused).
  * ``register_session`` is called per ``open_file()`` invocation, not
    once per process.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _backend(hass_data: dict | None = None):
    """Build a configured ``_SmbBackend`` with credentials for tests below."""
    from custom_components.bosch_shc_camera.media_source import _SmbBackend

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


def _fake_smbclient() -> MagicMock:
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


# ── core behaviour: each call → its own cache ─────────────────────────────


class TestSmbConnectionCachePerCall:
    """The fix: every public SMB call must use its own ``connection_cache``.

    A shared cache means a shared ``Connection``, which means a shared 64-credit
    pool that exhausts under burst — exactly the bug from the production trace.
    """

    def test_open_file_passes_connection_cache_to_all_smb_ops(self):
        """register_session, stat, and open_file must each receive the SAME
        per-call cache dict via the ``connection_cache`` kwarg."""
        backend = _backend()
        fake = _fake_smbclient()
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

        # share_access="r" must be passed on open_file — without it, FRITZ.NAS
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
        credit pool — the exact production bug. The 9× SMBException came
        from ≥9 concurrent ops landing on one shared session.
        """
        backend = _backend()
        fake = _fake_smbclient()
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
        """Flat-layout (legacy camera/file.mp4) — same isolation requirement."""
        backend = _backend()
        fake = _fake_smbclient()
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
        list_flat_dates) all go through ``_scandir_filtered`` → scandir(). They
        must also use a fresh cache so a burst of browse requests during a video
        playback can't contend on the same Connection."""
        backend = _backend()
        fake = _fake_smbclient()

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


# ── parallel burst simulation: pins the original failure mode ─────────────


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

        backend = _backend()
        fake = _fake_smbclient()
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
            f"9 parallel open_file → 9 register_session calls, got {len(register_calls)}"
        )
        caches = [c.kwargs["connection_cache"] for c in register_calls]
        cache_ids = {id(c) for c in caches}
        assert len(cache_ids) == 9, (
            f"all 9 caches must be distinct objects; got {len(cache_ids)} unique. "
            "Sharing caches across threads is the exact bug we're preventing."
        )
