"""Read-only REST client for the camera's local interface.

Pins: GET-only endpoints and auth, status classification (up / wrong password
/ unreachable / model without the endpoint), the privacy-state and firmware
parsers, and TLS — chain verified against the published root CA with hostname
checking off, exercised against a real TLS server (trusted chain answers, an
untrusted chain and a wrong password do not).
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import ssl
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from custom_components.bosch_shc_camera import ldi_rest

MOD = "custom_components.bosch_shc_camera.ldi_rest"
IP = "10.0.0.50"
USER = "localuser"
PW = "test-pw"


class _Resp:
    def __init__(self, status: int, text: str = "") -> None:
        self.status = status
        self._text = text

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None

    async def text(self) -> str:
        return self._text


class _Session:
    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append((url, kwargs))
        if isinstance(self.reply, BaseException):
            raise self.reply
        if callable(self.reply):
            return self.reply(url)
        return self.reply


def _patched(session: _Session) -> Any:
    ctx = object()
    return (
        patch(f"{MOD}.async_get_clientsession", return_value=session),
        patch(f"{MOD}.async_get_camera_ssl_context", new=AsyncMock(return_value=ctx)),
        ctx,
    )


async def _call(fn: Any, reply: Any) -> tuple[ldi_rest.LdiRestResponse, _Session, Any]:
    session = _Session(reply)
    p1, p2, ctx = _patched(session)
    with p1, p2:
        result = await fn(MagicMock(), IP, USER, PW)
    return result, session, ctx


class TestEndpoints:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("fn", "path"),
        [
            (ldi_rest.ping, "/sh/ping"),
            (ldi_rest.get_version, "/sh/data/version"),
            (ldi_rest.get_privacy_state, "/sh/data/privacyState"),
            (ldi_rest.get_wifi_info, "/sh/data/wifiInfo"),
            (ldi_rest.get_current_noise, "/sh/data/audio/currentNoise"),
        ],
    )
    async def test_each_function_is_one_authenticated_get(
        self, fn: Any, path: str
    ) -> None:
        result, session, ctx = await _call(fn, _Resp(200, '{"a": 1}'))
        assert result.ok and result.data == {"a": 1}
        assert len(session.calls) == 1
        url, kwargs = session.calls[0]
        assert url == f"https://{IP}{path}"
        assert kwargs["headers"] == {
            "Authorization": "Basic "
            + base64.b64encode(f"{USER}:{PW}".encode()).decode()
        }
        assert kwargs["ssl"] is ctx
        assert kwargs["allow_redirects"] is False

    @pytest.mark.asyncio
    async def test_ping_has_an_empty_body(self) -> None:
        result, _, _ = await _call(ldi_rest.ping, _Resp(200, ""))
        assert result.ok and result.data is None

    @pytest.mark.asyncio
    async def test_non_json_body_gives_no_data(self) -> None:
        result, _, _ = await _call(ldi_rest.ping, _Resp(200, "<html>"))
        assert result.ok and result.data is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_wrong_password(self, status: int) -> None:
        result, _, _ = await _call(ldi_rest.get_version, _Resp(status, "x"))
        assert result.unauthorized and not result.ok and result.data is None

    @pytest.mark.asyncio
    async def test_404_means_not_available_on_this_model(self) -> None:
        result, _, _ = await _call(ldi_rest.get_wifi_info, _Resp(404))
        assert result.unavailable and not result.unauthorized

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            TimeoutError(),
            aiohttp.ClientConnectionError(),
            OSError("unreachable"),
            ssl.SSLCertVerificationError(),
            UnicodeDecodeError("utf-8", b"", 0, 1, "x"),
        ],
    )
    async def test_network_failure_never_raises(self, error: BaseException) -> None:
        result, _, _ = await _call(ldi_rest.ping, error)
        assert result.unreachable and result.status is None

    def test_constants_are_the_documented_values(self) -> None:
        assert ldi_rest.LDI_REST_TIMEOUT == 5.0


class TestParsers:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ({"privacyModeState": "ON", "timeout": None}, True),
            ({"privacyModeState": "TURNING_ON"}, True),
            ({"privacyModeState": "OFF"}, False),
            ({"privacyModeState": "TURNING_OFF"}, None),
            ({"privacyModeState": "garbage"}, None),
            ({"privacyModeState": 1}, None),
            ({}, None),
            ([], None),
            (None, None),
        ],
    )
    def test_privacy_on_from(self, body: object, expected: bool | None) -> None:
        assert ldi_rest.privacy_on_from(body) is expected

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ({"firmwareVersion": "9.40.0202"}, "9.40.0202"),
            ({"firmwareVersion": "  "}, None),
            ({"firmwareVersion": 9}, None),
            ({}, None),
            (None, None),
            ("9.40", None),
        ],
    )
    def test_firmware_from(self, body: object, expected: str | None) -> None:
        assert ldi_rest.firmware_from(body) == expected


def _script(**by_path: Any) -> Any:
    """Reply per request path; records the order of paths asked."""
    asked: list[str] = []

    def reply(url: str) -> Any:
        path = url.removeprefix(f"https://{IP}")
        asked.append(path)
        return by_path[path]

    reply.asked = asked  # type: ignore[attr-defined]
    return reply


async def _probe(reply: Any, **kw: Any) -> ldi_rest.LdiProbe:
    p1, p2, _ = _patched(_Session(reply))
    with p1, p2:
        return await ldi_rest.probe_camera(MagicMock(), IP, USER, PW, **kw)


class TestProbe:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("state", "privacy"), [("ON", True), ("OFF", False), ("TURNING_OFF", None)]
    )
    async def test_one_request_answers_up_password_and_privacy(
        self, state: str, privacy: bool | None
    ) -> None:
        reply = _script(
            **{
                "/sh/data/privacyState": _Resp(
                    200, f'{{"privacyModeState": "{state}"}}'
                )
            }
        )
        probe = await _probe(reply)
        assert probe == ldi_rest.LdiProbe(ldi_rest.PROBE_OK, privacy, None)
        assert reply.asked == ["/sh/data/privacyState"]

    @pytest.mark.asyncio
    async def test_firmware_only_when_asked(self) -> None:
        reply = _script(
            **{
                "/sh/data/privacyState": _Resp(200, '{"privacyModeState": "OFF"}'),
                "/sh/data/version": _Resp(200, '{"firmwareVersion": "9.40.0202"}'),
            }
        )
        probe = await _probe(reply, want_version=True)
        assert probe.firmware == "9.40.0202"
        assert reply.asked == ["/sh/data/privacyState", "/sh/data/version"]

    @pytest.mark.asyncio
    async def test_wrong_password_reads_no_further(self) -> None:
        reply = _script(**{"/sh/data/privacyState": _Resp(401)})
        probe = await _probe(reply, want_version=True)
        assert probe.result == ldi_rest.PROBE_AUTH
        assert probe.privacy_on is None and probe.firmware is None
        assert reply.asked == ["/sh/data/privacyState"]

    @pytest.mark.asyncio
    async def test_unreachable(self) -> None:
        probe = await _probe(TimeoutError(), want_version=True)
        assert probe.result == ldi_rest.PROBE_UNREACHABLE

    @pytest.mark.asyncio
    async def test_server_error_is_an_error_not_a_password_verdict(self) -> None:
        reply = _script(**{"/sh/data/privacyState": _Resp(500)})
        assert (await _probe(reply)).result == ldi_rest.PROBE_ERROR

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("ping_reply", "expected"),
        [
            (_Resp(200), ldi_rest.PROBE_OK),
            (_Resp(401), ldi_rest.PROBE_AUTH),
            (_Resp(500), ldi_rest.PROBE_ERROR),
        ],
    )
    async def test_model_without_privacy_endpoint_falls_back_to_ping(
        self, ping_reply: _Resp, expected: str
    ) -> None:
        """404 on a documented path = this model does not have it; never an error."""
        reply = _script(**{"/sh/data/privacyState": _Resp(404), "/sh/ping": ping_reply})
        probe = await _probe(reply)
        assert probe.result == expected
        assert probe.privacy_on is None
        assert reply.asked == ["/sh/data/privacyState", "/sh/ping"]

    @pytest.mark.asyncio
    async def test_ping_fallback_unreachable(self) -> None:
        calls = {"n": 0}

        def reply(url: str) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp(404)
            raise TimeoutError

        assert (await _probe(reply)).result == ldi_rest.PROBE_UNREACHABLE


# ── TLS ──────────────────────────────────────────────────────────────────────
def _make_ca(name: str) -> tuple[Any, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _make_leaf(ca_key: Any, ca_cert: x509.Certificate) -> tuple[Any, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.UTC)
    # CN is a MAC address, as on the camera: no hostname can ever match.
    cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "aa:bb:cc:dd:ee:ff")])
        )
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def _pem(obj: Any) -> bytes:
    if isinstance(obj, x509.Certificate):
        return obj.public_bytes(serialization.Encoding.PEM)
    return obj.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


async def _serve(tmp_path: Path, ca: tuple[Any, x509.Certificate]) -> tuple[Any, int]:
    leaf_key, leaf = _make_leaf(*ca)
    (tmp_path / "chain.pem").write_bytes(_pem(leaf))
    (tmp_path / "key.pem").write_bytes(_pem(leaf_key))
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(tmp_path / "chain.pem", tmp_path / "key.pem")

    async def version(request: web.Request) -> web.Response:
        sent = base64.b64decode(request.headers["Authorization"][6:]).decode()
        if sent != f"{USER}:{PW}":
            return web.Response(status=401)
        return web.json_response({"firmwareVersion": "9.40.0202"})

    app = web.Application()
    app.router.add_get("/sh/data/version", version)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ctx)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, port


@pytest.fixture
def _loopback(socket_enabled: None) -> None:
    return None


@pytest.mark.usefixtures("_loopback")
class TestTls:
    def test_published_root_ca_is_pinned(self) -> None:
        cert = x509.load_pem_x509_certificate(ldi_rest.LDI_ROOT_CA_PEM.encode())
        assert cert.fingerprint(hashes.SHA256()).hex(":").upper() == (
            "16:AC:1F:4C:B5:43:2F:F2:EA:29:30:E5:0E:B8:63:04:"
            "77:5A:98:2F:FB:D4:C2:8E:01:AB:35:E2:EC:F4:BB:74"
        )
        assert hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).digest()

    def test_context_verifies_the_chain_but_not_the_hostname(self) -> None:
        ctx = ldi_rest._build_ssl_context()
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is False
        # only the pinned root is trusted, not the system store
        assert len(ctx.get_ca_certs()) == 1

    @pytest.mark.asyncio
    async def test_context_is_built_once_off_the_event_loop(self) -> None:
        ldi_rest._SSL_CONTEXT = None
        ldi_rest._SSL_CONTEXT_LOCK = None
        built = ssl.create_default_context()

        async def run_in_executor(fn: Any) -> Any:
            return built if fn is ldi_rest._build_ssl_context else fn()

        hass = SimpleNamespace(
            async_add_executor_job=AsyncMock(side_effect=run_in_executor)
        )
        try:
            first = await ldi_rest.async_get_camera_ssl_context(hass)  # type: ignore[arg-type]
            second = await ldi_rest.async_get_camera_ssl_context(hass)  # type: ignore[arg-type]
        finally:
            ldi_rest._SSL_CONTEXT = None
            ldi_rest._SSL_CONTEXT_LOCK = None
        assert first is second is built
        hass.async_add_executor_job.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_trusted_chain_answers_despite_mac_common_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ca = _make_ca("Test Device Root")
        monkeypatch.setattr(ldi_rest, "LDI_ROOT_CA_PEM", _pem(ca[1]).decode())
        monkeypatch.setattr(ldi_rest, "_SSL_CONTEXT", None)
        runner, port = await _serve(tmp_path, ca)
        ctx = ldi_rest._build_ssl_context()
        try:
            async with aiohttp.ClientSession() as session:
                with (
                    patch(f"{MOD}.async_get_clientsession", return_value=session),
                    patch(
                        f"{MOD}.async_get_camera_ssl_context",
                        new=AsyncMock(return_value=ctx),
                    ),
                ):
                    hass = MagicMock()
                    ok = await ldi_rest.get_version(hass, f"127.0.0.1:{port}", USER, PW)
                    bad = await ldi_rest.get_version(
                        hass, f"127.0.0.1:{port}", USER, "x"
                    )
        finally:
            await runner.cleanup()
        assert ok.ok and ok.data == {"firmwareVersion": "9.40.0202"}
        assert bad.unauthorized

    @pytest.mark.asyncio
    async def test_untrusted_chain_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The server side logs the aborted handshake; that is the expected outcome.
        asyncio.get_running_loop().set_exception_handler(lambda _l, _c: None)
        server_ca = _make_ca("Some Other Root")
        trusted_ca = _make_ca("Test Device Root")
        monkeypatch.setattr(ldi_rest, "LDI_ROOT_CA_PEM", _pem(trusted_ca[1]).decode())
        runner, port = await _serve(tmp_path, server_ca)
        ctx = ldi_rest._build_ssl_context()
        try:
            async with aiohttp.ClientSession() as session:
                with (
                    patch(f"{MOD}.async_get_clientsession", return_value=session),
                    patch(
                        f"{MOD}.async_get_camera_ssl_context",
                        new=AsyncMock(return_value=ctx),
                    ),
                ):
                    result = await ldi_rest.get_version(
                        MagicMock(), f"127.0.0.1:{port}", USER, PW
                    )
        finally:
            await runner.cleanup()
        assert result.unreachable
