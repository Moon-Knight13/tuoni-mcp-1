"""The bounded transport preserves TuoniLib's UTF-8 Basic authentication."""

from __future__ import annotations

import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tuoni_mcp.http_client import BoundedTuoniC2


def test_login_sends_unicode_credentials_as_utf8():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append((self.path, self.headers.get("Authorization")))
            body = b'{"tokenType":"Bearer","accessToken":"synthetic-token","refreshToken":"fixture-refresh","expiresIn":3600,"refreshExpiresAt":"2099-01-01T00:00:00Z"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    client = BoundedTuoniC2(request_timeout=1)
    try:
        client.login(f"http://127.0.0.1:{upstream.server_port}", "opérator", "密码🔐")
        expected = "Basic " + base64.b64encode("opérator:密码🔐".encode("utf-8")).decode("ascii")
        assert received == [("/api/v2/auth/login", expected)]
        assert client._auth.access_token == "synthetic-token"
    finally:
        client.close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()
