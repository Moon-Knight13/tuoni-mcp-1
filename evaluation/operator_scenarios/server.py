"""Serve one multi-stage operator scenario over real MCP stdio."""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from tuoni_mcp import connection, server
from tuoni_mcp.config import Profile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evaluation.extended.world import SyntheticBridge  # noqa: E402
from evaluation.operator_scenarios.cases import CASES  # noqa: E402
from evaluation.operator_scenarios.world import OperatorC2  # noqa: E402
from evaluation.portable.runner import _settings  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["native", "dispatch"])
    parser.add_argument("case", choices=tuple(CASES))
    parser.add_argument("prefix")
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    args.folder.mkdir(parents=True, exist_ok=True)
    world = OperatorC2(args.case)
    lock = threading.RLock()

    def save():
        with lock:
            (args.folder / "world.json").write_text(json.dumps(world.snapshot(), indent=2))

    for name in ("request_get", "request_post", "request_put", "request_patch", "request_delete", "request_put_multipart"):
        original = getattr(world, name)

        def recorded(*a, _original=original, **kw):
            with lock:
                try:
                    return _original(*a, **kw)
                finally:
                    save()

        setattr(world, name, recorded)
    save()

    async def login(ctx):
        ctx.last_login = time.monotonic()

    server.TuoniC2 = lambda **kwargs: world
    server.EventBridge = SyntheticBridge
    server.login = connection.login = login
    cfg = replace(_settings(args.mode), tool_prefix=args.prefix,
                  profiles=frozenset(Profile(p) for p in CASES[args.case].profiles),
                  download_dir=str(args.folder / "downloads"))
    server.build_server(cfg).run()


if __name__ == "__main__":
    main()
