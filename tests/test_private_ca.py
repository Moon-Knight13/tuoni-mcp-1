"""Real HTTPS verification with a private CA and a hostname-valid certificate."""
import asyncio
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import time
import ssl
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest
import requests

from tuoni_mcp.http_client import BoundedTuoniC2
from websockets.exceptions import ConnectionClosedOK
from websockets.asyncio.server import serve


@pytest.fixture
def private_ca(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))
        ]), critical=False).sign(key, hashes.SHA256())
    )
    ca = tmp_path / "ca.pem"
    ca.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca, key_path)
    return ca, context


def test_private_ca_verifies_authentication_and_reads_over_real_tls(private_ca):
    ca, context = private_ca

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"tokenType":"Bearer","accessToken":"fixture-token","refreshToken":"fixture-refresh","expiresIn":3600,"refreshExpiresAt":"2099-01-01T00:00:00Z"}')

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"verified":true}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    strict = BoundedTuoniC2()
    trusted = BoundedTuoniC2(verify=str(ca))
    url = f"https://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(requests.exceptions.SSLError):
            strict.login(url, "fixture", "fixture")
        trusted.login(url, "fixture", "fixture")
        assert trusted.request_get("/read") == {"verified": True}
    finally:
        strict.close()
        trusted.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_private_ca_verifies_websocket_handshake_and_rejects_untrusted_certificate(private_ca):
    ca, context = private_ca
    ready = threading.Event()
    state = {}
    errors = []

    async def peer(socket):
        try:
            await socket.send("o")
            await socket.recv()
            await socket.send('a["CONNECTED\\n\\n\\u0000"]')
            with pytest.raises(ConnectionClosedOK):
                await socket.recv()
        except BaseException as exc:
            errors.append(exc)
            raise

    async def serve_peer():
        stop = asyncio.Event()
        async with serve(peer, "127.0.0.1", 0, ssl=context, open_timeout=3, close_timeout=1) as upstream:
            state.update(loop=asyncio.get_running_loop(), stop=stop,
                         url=f"https://127.0.0.1:{upstream.sockets[0].getsockname()[1]}")
            ready.set()
            await stop.wait()

    def run_peer():
        try:
            asyncio.run(serve_peer())
        except BaseException as exc:
            errors.append(exc)
            ready.set()

    # The test peer also owns TLS on one loop: a concurrent sync SSL reader and
    # writer would add the same race that this client regression must detect.
    thread = threading.Thread(target=run_peer, name="private-ca-peer", daemon=True)
    thread.start()
    strict = BoundedTuoniC2(request_timeout=3)
    trusted = BoundedTuoniC2(verify=str(ca), request_timeout=3)
    try:
        assert ready.wait(3)
        assert not errors
        for client in (strict, trusted):
            response = requests.Response()
            response._content = json.dumps({
                "tokenType": "Bearer", "accessToken": "fixture-token", "refreshToken": "fixture-refresh",
                "expiresIn": 3600, "refreshExpiresAt": "2099-01-01T00:00:00Z",
            }).encode()
            client._auth = client._parse_tokens(response, state["url"], time.monotonic(), object())
        with pytest.raises(ssl.SSLCertVerificationError):
            strict.connect_websocket()
        # Quiet repeated handshakes expose the TLS race that debug logging hid.
        # The deadline stays unchanged; every cycle must release its owner.
        for _ in range(20):
            socket = trusted.connect_websocket()
            try:
                assert socket.is_connected
            finally:
                socket.disconnect()
            assert not socket._reader.is_alive()
    finally:
        strict.close()
        trusted.close()
        if state:
            state["loop"].call_soon_threadsafe(state["stop"].set)
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert not errors
