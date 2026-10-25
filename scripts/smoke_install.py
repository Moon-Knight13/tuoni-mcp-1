"""Exercise an installed executable or Linux Docker image through real MCP stdio.

Uses only a temporary synthetic upstream: no live C2 credentials or operations.
Run from the development environment, passing the installed executable's absolute
path, or --docker-image IMAGE for a locally built production image.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@contextmanager
def upstream():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, body: bytes):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            calls.append(("POST", self.path))
            expected = "Basic " + base64.b64encode(b"smoke:synthetic-password").decode()
            if self.path != "/api/v2/auth/login" or self.headers.get("Authorization") != expected:
                self.send_error(403)
                return
            self.reply(b'{"tokenType":"Bearer","accessToken":"synthetic-token","refreshToken":"fixture-refresh","expiresIn":3600,"refreshExpiresAt":"2099-01-01T00:00:00Z"}')

        def do_GET(self):
            calls.append(("GET", self.path))
            request = urlsplit(self.path)
            params = parse_qs(request.query)
            if (request.path != "/api/v2/agents" or params.get("pageSize") != ["1"]
                    or self.headers.get("Authorization") != "Bearer synthetic-token"):
                self.send_error(403)
                return
            page = int(params.get("page", ["0"])[0])
            self.reply(json.dumps({
                "items": [{"guid": f"smoke-agent-{page}", "active": True, "status": "ACTIVE",
                           "metadata": {"hostname": f"smoke-host-{page}", "os": "WINDOWS"}}],
                "page": page, "totalCount": 2, "hasMorePages": page == 0,
            }).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


async def check(executable: str | None, docker_image: str | None) -> list[dict]:
    reports = []
    with upstream() as (url, requests), tempfile.TemporaryDirectory(prefix="tuoni-mcp-smoke-") as cwd:
        for mode in ("native", "dispatch"):
            env = {
                "TUONI_URL": url, "TUONI_USERNAME": "smoke", "TUONI_PASSWORD": "synthetic-password",
                "TUONI_MCP_MODE": mode, "TUONI_MCP_PROFILES": "agent-ops",
                "TUONI_MCP_READ_ONLY": "false", "TUONI_VERIFY_TLS": "true",
            }
            if docker_image:
                args = ["run", "--rm", "-i", "--network", "host", "--read-only", "--cap-drop", "ALL",
                        "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m"]
                for key, value in env.items():
                    args.extend(["-e", f"{key}={value}"])
                params = StdioServerParameters(command="docker", args=[*args, docker_image], cwd=cwd)
            else:
                params = StdioServerParameters(command=executable, env=env, cwd=cwd)
            start = len(requests)
            with anyio.fail_after(30):
                async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    catalog = await session.list_tools()
                    names = {tool.name for tool in catalog.tools}
                    assert "tuoni_call" in names if mode == "dispatch" else "tuoni_agents_list" in names

                    def call(arguments):
                        if mode == "dispatch":
                            return session.call_tool("tuoni_call", {"name": "tuoni_agents_list", "arguments": arguments})
                        return session.call_tool("tuoni_agents_list", arguments)

                    invalid = await call({"limit": 0})
                    assert invalid.isError, "Invalid input was accepted"
                    # Best-effort startup login runs concurrently with MCP initialization.
                    # Validation must not issue an operational request, regardless of when
                    # that single authentication request finishes.
                    assert all(method == "POST" for method, _ in requests[start:]), "Invalid input read upstream"
                    first = await call({"limit": 1})
                    assert not first.isError, first
                    first_data = json.loads(first.content[0].text)
                    assert first_data["data"]["agents"][0]["guid"] == "smoke-agent-0", first_data
                    continuation = first_data["next"]
                    second = await session.call_tool(continuation["name"], continuation["arguments"])
                    assert not second.isError, second
                    second_data = json.loads(second.content[0].text)
                    assert second_data["data"]["agents"][0]["guid"] == "smoke-agent-1", second_data
                    assert second_data.get("next") is None, second_data
                    assert [method for method, _ in requests[start:]] == ["POST", "GET", "GET"], requests[start:]
                    reports.append({"mode": mode, "protocol": initialized.protocolVersion,
                                    "catalog_tools": len(names), "agent_pages": 2,
                                    "validation_before_read": True, "authenticated_reads": 2})
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--executable", type=Path)
    target.add_argument("--docker-image", help="Linux image; uses Docker host networking")
    args = parser.parse_args()
    executable = str(args.executable.resolve(strict=True)) if args.executable else None
    print(json.dumps(asyncio.run(check(executable, args.docker_image)), indent=2))


if __name__ == "__main__":
    main()
