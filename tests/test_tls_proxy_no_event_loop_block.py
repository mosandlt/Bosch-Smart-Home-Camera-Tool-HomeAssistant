"""Regression test for Fix #6 (2026-05-26): `start_tls_proxy` was called
from the async path without `executor_job`, and it contained a
`threading.Event.wait(timeout=2)` — a blocking primitive on the asyncio
event loop. Fast in practice, but technically a sync-on-async violation.

Fix: removed the `ready = threading.Event()` and the
`_proxy_thread_with_signal` wrapper. The proxy port is already listening
before the thread starts (`srv.bind() + srv.listen()`), so no signal is
needed. Pin: nothing in `start_tls_proxy` may use `threading.Event.wait`
or any other blocking primitive after the listening socket is set up.
"""

from __future__ import annotations

import inspect
import textwrap

from custom_components.bosch_shc_camera import tls_proxy


class TestStartTlsProxyNoBlocking:

    def test_no_threading_event_wait_in_source(self) -> None:
        """The source of `start_tls_proxy` must not contain a
        `threading.Event` allocation or `ready.wait(` call. Comments are
        stripped before checking so explanatory prose may still mention them."""
        raw = textwrap.dedent(inspect.getsource(tls_proxy.start_tls_proxy))
        # Strip line comments so the test doesn't trip on prose like
        # "we removed the ready.wait() call".
        code_only = "\n".join(
            line.split("#", 1)[0] for line in raw.splitlines()
        )
        assert "threading.Event(" not in code_only, (
            "start_tls_proxy must not allocate a threading.Event — the port "
            "is already listening before the worker thread starts."
        )
        assert "ready.wait" not in code_only, (
            "start_tls_proxy must not wait on a thread-start signal — it runs "
            "on the asyncio event loop."
        )
