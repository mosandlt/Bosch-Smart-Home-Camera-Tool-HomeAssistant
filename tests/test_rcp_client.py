"""Regression tests for rcp_client.py — RCP session/read primitives and the
RCP-proxy-host SSRF validators, extracted out of coordinator.py (structural
cleanup toward Platinum quality_scale).

Tests call the module functions directly with a lightweight stub
(SimpleNamespace) standing in for the coordinator, mirroring
test_quality_prefs.py's convention. Where the original coordinator method
called another extracted coordinator method on `self` (e.g. `rcp_read`
calling `self._proxy_hash_from_rcp_base`/`self._invalidate_rcp_session`,
`get_cached_rcp_session` calling `self._rcp_session`), the stub binds an
instance-level callable for that name so a test can both observe the call
and (via a subclass-style override) prove virtual dispatch is preserved —
matching quality_prefs.py's `coord.get_quality = lambda cam_id: ...` pattern.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import rcp_client

CAM_A = "cam-a"
RCP_CLIENT_MODULE = "custom_components.bosch_shc_camera.rcp_client"
PKG_MODULE = "custom_components.bosch_shc_camera"


def _resp_cm(status: int, text: str = "", body: bytes = b""):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.read = AsyncMock(return_value=body or text.encode())
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _timeout_cm():
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=TimeoutError())
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_coord(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "hass": MagicMock(),
        "rcp_session_cache": {},
        "rcp_session_locks": {},
        "local_creds_cache": {},
        "rcp_lan_ip_cache": {},
        "_rcp_lan_denied_until": {},
        "_RCP_LAN_DENIED_TTL": 300.0,
    }
    base.update(overrides)
    coord = SimpleNamespace(**base)
    # Delegating-stub bindings — mirrors coordinator.py's real thin wrappers,
    # so a test overriding one of these on the instance (e.g. AsyncMock())
    # is honored by the callee exactly like a real per-instance patch would be.
    coord._get_rcp_session_lock = lambda proxy_hash: MagicMock(  # type: ignore[attr-defined]
        __aenter__=AsyncMock(return_value=None), __aexit__=AsyncMock(return_value=None)
    )
    coord._rcp_session = lambda proxy_host, proxy_hash: rcp_client._rcp_session(  # type: ignore[attr-defined]
        coord, proxy_host, proxy_hash
    )
    coord._proxy_hash_from_rcp_base = rcp_client._proxy_hash_from_rcp_base  # type: ignore[attr-defined]
    coord._invalidate_rcp_session = lambda proxy_hash: (
        rcp_client._invalidate_rcp_session(  # type: ignore[attr-defined]
            coord, proxy_hash
        )
    )
    coord._is_rcp_lan_denied = lambda cam_id, opcode: False  # type: ignore[attr-defined]
    coord._mark_rcp_lan_denied = MagicMock()  # type: ignore[attr-defined]
    coord._clear_rcp_lan_denied = MagicMock()  # type: ignore[attr-defined]
    coord.get_cam_lan_ip = lambda cam_id: coord.rcp_lan_ip_cache.get(cam_id)  # type: ignore[attr-defined]
    return coord


class TestIsSafeBoschHost:
    def test_accepts_boschsecurity_domain(self) -> None:
        assert rcp_client._is_safe_bosch_host(
            "proxy-01.live.cbs.boschsecurity.com:42090"
        )

    def test_accepts_bosch_domain(self) -> None:
        assert rcp_client._is_safe_bosch_host("cam.bosch.com:443")

    def test_rejects_non_bosch_host(self) -> None:
        assert rcp_client._is_safe_bosch_host("169.254.169.254:80") is False

    def test_rejects_lookalike_domain(self) -> None:
        assert rcp_client._is_safe_bosch_host("attacker-boschsecurity.com:443") is False

    def test_rejects_userinfo_smuggling(self) -> None:
        assert (
            rcp_client._is_safe_bosch_host(
                "proxy.boschsecurity.com:443@attacker.example"
            )
            is False
        )
        assert (
            rcp_client._is_safe_bosch_host("attacker.example@proxy.boschsecurity.com")
            is False
        )

    def test_rejects_malformed_url(self) -> None:
        assert rcp_client._is_safe_bosch_host("[::1") is False


class TestParseSafeRcpProxyUrl:
    def test_valid_entry_splits_host_and_hash(self) -> None:
        result = rcp_client._parse_safe_rcp_proxy_url(
            "proxy-01.live.cbs.boschsecurity.com:42090/abcdef1234", CAM_A
        )
        assert result == ("proxy-01.live.cbs.boschsecurity.com:42090", "abcdef1234")

    def test_unsafe_host_rejected(self) -> None:
        assert rcp_client._parse_safe_rcp_proxy_url("evil.com/somehash", CAM_A) is None

    def test_missing_slash_rejected(self) -> None:
        assert (
            rcp_client._parse_safe_rcp_proxy_url(
                "proxy-01.live.cbs.boschsecurity.com:42090", CAM_A
            )
            is None
        )


class TestParseOnvifScopes:
    def test_full_tlv_parses_all_fields(self) -> None:
        raw = (
            b"onvif://www.onvif.org/name/Bosch%20Smart%20Home%20Camera\x00"
            b"onvif://www.onvif.org/hardware/HOME_Eyes_Outdoor\x00"
            b"onvif://www.onvif.org/Profile/Streaming\x00"
        )
        result = rcp_client._parse_onvif_scopes(raw)
        assert result["supported"] is True
        assert result["name"] == "Bosch Smart Home Camera"
        assert result["hardware"] == "HOME_Eyes_Outdoor"
        assert result["profiles"] == ["Streaming"]
        assert len(result["raw_scopes"]) == 3

    def test_empty_bytes_returns_supported_true_empty_fields(self) -> None:
        result = rcp_client._parse_onvif_scopes(b"")
        assert result == {
            "supported": True,
            "raw_scopes": [],
            "name": "",
            "hardware": "",
            "profiles": [],
        }

    def test_non_onvif_scope_ignored(self) -> None:
        result = rcp_client._parse_onvif_scopes(b"garbage-not-a-scope\x00")
        assert result["name"] == ""
        assert result["raw_scopes"] == ["garbage-not-a-scope"]

    def test_malformed_bytes_do_not_raise(self) -> None:
        # non-ASCII-decodable-but-still-bytes input must not raise
        result = rcp_client._parse_onvif_scopes(b"\xff\xfe\x00garbage")
        assert result["supported"] is True

    def test_onvif_scope_without_slash_after_prefix_ignored(self) -> None:
        # "onvif://www.onvif.org/nameonly" has no "/" left after stripping
        # the fixed prefix, so key/val can't be split — must be skipped,
        # not crash.
        result = rcp_client._parse_onvif_scopes(b"onvif://www.onvif.org/nameonly\x00")
        assert result["name"] == ""
        assert result["hardware"] == ""
        assert result["raw_scopes"] == ["onvif://www.onvif.org/nameonly"]


class TestInvalidateRcpSession:
    def test_pops_existing_entry(self) -> None:
        coord = _make_coord(rcp_session_cache={"hashA": ("0xSESS", 999.0)})
        rcp_client._invalidate_rcp_session(coord, "hashA")
        assert "hashA" not in coord.rcp_session_cache

    def test_missing_entry_is_a_noop(self) -> None:
        coord = _make_coord(rcp_session_cache={})
        rcp_client._invalidate_rcp_session(coord, "nonexistent")  # must not raise
        assert coord.rcp_session_cache == {}

    def test_does_not_touch_other_hashes(self) -> None:
        coord = _make_coord(
            rcp_session_cache={"hashA": ("0xA", 1.0), "hashB": ("0xB", 2.0)}
        )
        rcp_client._invalidate_rcp_session(coord, "hashA")
        assert coord.rcp_session_cache == {"hashB": ("0xB", 2.0)}


class TestGetCachedRcpSession:
    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_id_without_opening_new(self) -> None:
        expires = time.monotonic() + 200
        coord = _make_coord(rcp_session_cache={"abc123": ("0xCAFEBABE", expires)})
        coord._rcp_session = AsyncMock(return_value="0xSHOULD-NOT-BE-CALLED")
        result = await rcp_client.get_cached_rcp_session(
            coord, "proxy-01:42090", "abc123"
        )
        assert result == "0xCAFEBABE"
        coord._rcp_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_expired_calls_rcp_session_and_restores_ttl(self) -> None:
        coord = _make_coord(
            rcp_session_cache={"abc123": ("0xOLD", time.monotonic() - 1)}
        )
        coord._rcp_session = AsyncMock(return_value="0xFRESH")
        result = await rcp_client.get_cached_rcp_session(
            coord, "proxy-01:42090", "abc123"
        )
        assert result == "0xFRESH"
        assert coord.rcp_session_cache["abc123"][0] == "0xFRESH"

    @pytest.mark.asyncio
    async def test_cache_miss_stores_new_session(self) -> None:
        coord = _make_coord(rcp_session_cache={})
        coord._rcp_session = AsyncMock(return_value="0x12345678")
        result = await rcp_client.get_cached_rcp_session(
            coord, "proxy-01:42090", "abc123"
        )
        assert result == "0x12345678"
        assert "abc123" in coord.rcp_session_cache

    @pytest.mark.asyncio
    async def test_rcp_session_returning_none_is_not_cached(self) -> None:
        coord = _make_coord(rcp_session_cache={})
        coord._rcp_session = AsyncMock(return_value=None)
        result = await rcp_client.get_cached_rcp_session(
            coord, "proxy-01:42090", "abc123"
        )
        assert result is None
        assert "abc123" not in coord.rcp_session_cache

    @pytest.mark.asyncio
    async def test_calls_through_coordinator_instance_not_module_function(self) -> None:
        """Virtual-dispatch guard: an instance-level override of `_rcp_session`
        (as a test/subclass would install) must be honored — get_cached_rcp_session
        must call `coordinator._rcp_session(...)`, not `rcp_client._rcp_session(...)`
        directly."""
        coord = _make_coord(rcp_session_cache={})
        coord._rcp_session = AsyncMock(return_value="0xOVERRIDDEN")
        result = await rcp_client.get_cached_rcp_session(
            coord, "proxy-01:42090", "abc123"
        )
        assert result == "0xOVERRIDDEN"
        coord._rcp_session.assert_awaited_once_with("proxy-01:42090", "abc123")


class TestRcpSession:
    """`_rcp_session` — the 2-step RCP handshake over the cloud proxy."""

    def _mock_session(self, *side_effects):
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.get = MagicMock(side_effect=list(side_effects))
        return session

    @pytest.mark.asyncio
    async def test_success_returns_session_id(self) -> None:
        coord = _make_coord()
        session = self._mock_session(
            _resp_cm(200, text="<sessionid>0x12345678</sessionid>"), _resp_cm(200)
        )
        with patch(
            f"{RCP_CLIENT_MODULE}.async_bosch_cloud_session_cm", return_value=session
        ):
            result = await rcp_client._rcp_session(
                coord, "proxy-01:42090", "abc123hash"
            )
        assert result == "0x12345678"

    @pytest.mark.asyncio
    async def test_step1_non200_returns_none(self) -> None:
        coord = _make_coord()
        session = self._mock_session(_resp_cm(403))
        with patch(
            f"{RCP_CLIENT_MODULE}.async_bosch_cloud_session_cm", return_value=session
        ):
            result = await rcp_client._rcp_session(
                coord, "proxy-01:42090", "abc123hash"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_no_sessionid_in_response_returns_none(self) -> None:
        coord = _make_coord()
        session = self._mock_session(_resp_cm(200, text="<result>ok</result>"))
        with patch(
            f"{RCP_CLIENT_MODULE}.async_bosch_cloud_session_cm", return_value=session
        ):
            result = await rcp_client._rcp_session(
                coord, "proxy-01:42090", "abc123hash"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_step1_timeout_returns_none(self) -> None:
        coord = _make_coord()
        session = self._mock_session()
        session.get = MagicMock(return_value=_timeout_cm())
        with patch(
            f"{RCP_CLIENT_MODULE}.async_bosch_cloud_session_cm", return_value=session
        ):
            result = await rcp_client._rcp_session(
                coord, "proxy-01:42090", "abc123hash"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_step2_timeout_still_returns_session_id(self) -> None:
        """Step2 (ACK) is best-effort — a timeout there is non-fatal since
        session_id was already extracted from step1."""
        coord = _make_coord()
        session = self._mock_session(
            _resp_cm(200, text="<sessionid>0xABCDEF01</sessionid>"), None
        )
        session.get = MagicMock(
            side_effect=[
                _resp_cm(200, text="<sessionid>0xABCDEF01</sessionid>"),
                _timeout_cm(),
            ]
        )
        with patch(
            f"{RCP_CLIENT_MODULE}.async_bosch_cloud_session_cm", return_value=session
        ):
            result = await rcp_client._rcp_session(
                coord, "proxy-01:42090", "abc123hash"
            )
        assert result == "0xABCDEF01"


class TestProxyHashFromRcpBase:
    def test_extracts_hash(self) -> None:
        url = "https://proxy-01.live.cbs.boschsecurity.com:42090/abcdef1234/rcp.xml"
        assert rcp_client._proxy_hash_from_rcp_base(url) == "abcdef1234"

    def test_trailing_slash_still_extracts(self) -> None:
        url = "https://host/myhash/rcp.xml/"
        assert rcp_client._proxy_hash_from_rcp_base(url) == "myhash"

    def test_no_rcp_xml_suffix_returns_none(self) -> None:
        assert rcp_client._proxy_hash_from_rcp_base("https://nohash") is None

    def test_empty_string_returns_none(self) -> None:
        assert rcp_client._proxy_hash_from_rcp_base("") is None


RCP_BASE = "https://proxy-01.live.cbs.boschsecurity.com:42090/abc123/rcp.xml"


class TestRcpRead:
    @pytest.mark.asyncio
    async def test_success_returns_bytes(self) -> None:
        coord = _make_coord()
        with patch(
            f"{PKG_MODULE}.async_get_bosch_cloud_session",
            AsyncMock(return_value=self._session(_resp_cm(200, body=b"\xff\xd8\xff"))),
        ):
            result = await rcp_client.rcp_read(coord, RCP_BASE, "0x099e", "sess-1")
        assert result == b"\xff\xd8\xff"

    @pytest.mark.asyncio
    async def test_401_invalidates_session_via_coordinator_instance(self) -> None:
        """Virtual-dispatch guard: rcp_read must invalidate via
        `coordinator._invalidate_rcp_session(...)`, not the raw module
        function — an instance-level override must be honored."""
        coord = _make_coord()
        coord._invalidate_rcp_session = MagicMock()
        with patch(
            f"{PKG_MODULE}.async_get_bosch_cloud_session",
            AsyncMock(return_value=self._session(_resp_cm(401))),
        ):
            result = await rcp_client.rcp_read(coord, RCP_BASE, "0x099e", "sess-1")
        assert result is None
        coord._invalidate_rcp_session.assert_called_once_with("abc123")

    @pytest.mark.asyncio
    async def test_403_invalidates_session(self) -> None:
        coord = _make_coord()
        coord._invalidate_rcp_session = MagicMock()
        with patch(
            f"{PKG_MODULE}.async_get_bosch_cloud_session",
            AsyncMock(return_value=self._session(_resp_cm(403))),
        ):
            result = await rcp_client.rcp_read(coord, RCP_BASE, "0x099e", "sess-1")
        assert result is None
        coord._invalidate_rcp_session.assert_called_once_with("abc123")

    @pytest.mark.asyncio
    async def test_other_non200_does_not_invalidate(self) -> None:
        coord = _make_coord()
        coord._invalidate_rcp_session = MagicMock()
        with patch(
            f"{PKG_MODULE}.async_get_bosch_cloud_session",
            AsyncMock(return_value=self._session(_resp_cm(500))),
        ):
            result = await rcp_client.rcp_read(coord, RCP_BASE, "0x099e", "sess-1")
        assert result is None
        coord._invalidate_rcp_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_closed_error_0c0d_invalidates_and_returns_none(self) -> None:
        coord = _make_coord()
        coord._invalidate_rcp_session = MagicMock()
        with patch(
            f"{PKG_MODULE}.async_get_bosch_cloud_session",
            AsyncMock(
                return_value=self._session(_resp_cm(200, body=b"<err>0x0c0d</err>"))
            ),
        ):
            result = await rcp_client.rcp_read(coord, RCP_BASE, "0x099e", "sess-1")
        assert result is None
        coord._invalidate_rcp_session.assert_called_once_with("abc123")

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self) -> None:
        coord = _make_coord()
        session = MagicMock()
        session.get = MagicMock(return_value=_timeout_cm())
        with patch(
            f"{PKG_MODULE}.async_get_bosch_cloud_session",
            AsyncMock(return_value=session),
        ):
            result = await rcp_client.rcp_read(coord, RCP_BASE, "0x099e", "sess-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_num_param_included_when_truthy(self) -> None:
        coord = _make_coord()
        session = self._session(_resp_cm(200, body=b"data"))
        with patch(
            f"{PKG_MODULE}.async_get_bosch_cloud_session",
            AsyncMock(return_value=session),
        ):
            await rcp_client.rcp_read(coord, RCP_BASE, "0x099e", "sess-1", num=5)
        _args, kwargs = session.get.call_args
        assert kwargs["params"]["num"] == "5"

    @staticmethod
    def _session(*resp_cms):
        session = MagicMock()
        session.get = MagicMock(side_effect=list(resp_cms) or [None])
        return session


class TestFetchRcpLan:
    @pytest.mark.asyncio
    async def test_denied_short_circuits(self) -> None:
        coord = _make_coord(rcp_lan_ip_cache={CAM_A: "10.0.0.5"})
        coord._is_rcp_lan_denied = lambda cam_id, opcode: True
        result = await rcp_client._fetch_rcp_lan(coord, CAM_A, "0x0a98")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_lan_ip_returns_none(self) -> None:
        coord = _make_coord(rcp_lan_ip_cache={})
        result = await rcp_client._fetch_rcp_lan(coord, CAM_A, "0x0a98")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_creds_returns_none(self) -> None:
        coord = _make_coord(rcp_lan_ip_cache={CAM_A: "10.0.0.5"}, local_creds_cache={})
        result = await rcp_client._fetch_rcp_lan(coord, CAM_A, "0x0a98")
        assert result is None

    @pytest.mark.asyncio
    async def test_incomplete_creds_returns_none(self) -> None:
        coord = _make_coord(
            rcp_lan_ip_cache={CAM_A: "10.0.0.5"},
            local_creds_cache={CAM_A: {"user": "cbs-1", "password": ""}},
        )
        result = await rcp_client._fetch_rcp_lan(coord, CAM_A, "0x0a98")
        assert result is None

    @pytest.mark.asyncio
    async def test_success_extracts_hex_payload(self) -> None:
        coord = _make_coord(
            rcp_lan_ip_cache={CAM_A: "10.0.0.5"},
            local_creds_cache={CAM_A: {"user": "cbs-1", "password": "pw", "port": 443}},
        )
        resp = AsyncMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=b"<str>deadbeef</str>")
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        with (
            patch(f"{PKG_MODULE}.async_digest_request", AsyncMock(return_value=resp)),
            patch(f"{PKG_MODULE}.async_get_clientsession", MagicMock()),
        ):
            result = await rcp_client._fetch_rcp_lan(coord, CAM_A, "0x0a98")
        assert result == bytes.fromhex("deadbeef")

    @pytest.mark.asyncio
    async def test_401_marks_denied_via_coordinator_instance(self) -> None:
        coord = _make_coord(
            rcp_lan_ip_cache={CAM_A: "10.0.0.5"},
            local_creds_cache={CAM_A: {"user": "cbs-1", "password": "pw", "port": 443}},
        )
        coord._mark_rcp_lan_denied = MagicMock()
        resp = AsyncMock()
        resp.status = 401
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        with (
            patch(f"{PKG_MODULE}.async_digest_request", AsyncMock(return_value=resp)),
            patch(f"{PKG_MODULE}.async_get_clientsession", MagicMock()),
        ):
            result = await rcp_client._fetch_rcp_lan(coord, CAM_A, "0x0a98")
        assert result is None
        coord._mark_rcp_lan_denied.assert_called_once_with(CAM_A, "0x0a98")

    @pytest.mark.asyncio
    async def test_rcp_error_returns_none(self) -> None:
        coord = _make_coord(
            rcp_lan_ip_cache={CAM_A: "10.0.0.5"},
            local_creds_cache={CAM_A: {"user": "cbs-1", "password": "pw", "port": 443}},
        )
        resp = AsyncMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=b"<err>0x0c0d</err>")
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        with (
            patch(f"{PKG_MODULE}.async_digest_request", AsyncMock(return_value=resp)),
            patch(f"{PKG_MODULE}.async_get_clientsession", MagicMock()),
        ):
            result = await rcp_client._fetch_rcp_lan(coord, CAM_A, "0x0a98")
        assert result is None

    @pytest.mark.asyncio
    async def test_non_xml_raw_bytes_fallback(self) -> None:
        coord = _make_coord(
            rcp_lan_ip_cache={CAM_A: "10.0.0.5"},
            local_creds_cache={CAM_A: {"user": "cbs-1", "password": "pw", "port": 443}},
        )
        resp = AsyncMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=b"\x01\x02\x03")
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        with (
            patch(f"{PKG_MODULE}.async_digest_request", AsyncMock(return_value=resp)),
            patch(f"{PKG_MODULE}.async_get_clientsession", MagicMock()),
        ):
            result = await rcp_client._fetch_rcp_lan(coord, CAM_A, "0x0a98")
        assert result == b"\x01\x02\x03"

    @pytest.mark.asyncio
    async def test_empty_body_no_str_no_err_no_raw_fallback_returns_none(self) -> None:
        """Status 200 but a genuinely empty body — neither `<err>`, `<str>HEX</str>`,
        nor raw-bytes fallback (empty is falsy) applies — must return None."""
        coord = _make_coord(
            rcp_lan_ip_cache={CAM_A: "10.0.0.5"},
            local_creds_cache={CAM_A: {"user": "cbs-1", "password": "pw", "port": 443}},
        )
        resp = AsyncMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=b"")
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        with (
            patch(f"{PKG_MODULE}.async_digest_request", AsyncMock(return_value=resp)),
            patch(f"{PKG_MODULE}.async_get_clientsession", MagicMock()),
        ):
            result = await rcp_client._fetch_rcp_lan(coord, CAM_A, "0x0a98")
        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self) -> None:
        coord = _make_coord(
            rcp_lan_ip_cache={CAM_A: "10.0.0.5"},
            local_creds_cache={CAM_A: {"user": "cbs-1", "password": "pw", "port": 443}},
        )
        with (
            patch(
                f"{PKG_MODULE}.async_digest_request",
                AsyncMock(side_effect=TimeoutError()),
            ),
            patch(f"{PKG_MODULE}.async_get_clientsession", MagicMock()),
        ):
            result = await rcp_client._fetch_rcp_lan(coord, CAM_A, "0x0a98")
        assert result is None

    @pytest.mark.asyncio
    async def test_unexpected_exception_returns_none(self) -> None:
        coord = _make_coord(
            rcp_lan_ip_cache={CAM_A: "10.0.0.5"},
            local_creds_cache={CAM_A: {"user": "cbs-1", "password": "pw", "port": 443}},
        )
        with (
            patch(
                f"{PKG_MODULE}.async_digest_request",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch(f"{PKG_MODULE}.async_get_clientsession", MagicMock()),
        ):
            result = await rcp_client._fetch_rcp_lan(coord, CAM_A, "0x0a98")
        assert result is None
