"""Serve an isolated fixture over stdio for real coding-client model probes."""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

# Import the installed runtime first; add only evaluation fixtures from checkout.
from tuoni_mcp import connection, server
from tuoni_mcp.config import Profile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evaluation.extended.cases import CASES  # noqa: E402
from evaluation.extended.world import ExtendedC2, SyntheticBridge  # noqa: E402
from evaluation.portable.runner import _settings  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["native", "dispatch"])
    parser.add_argument("case")
    parser.add_argument("prefix")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--catalog", type=Path)
    args = parser.parse_args()
    templates = json.loads(args.catalog.read_text()) if args.catalog else None
    case = CASES.get(args.case)
    world = ExtendedC2(templates=templates, fault=case.fault if case else None)
    lock = threading.RLock()

    def save():
        with lock:
            (args.folder / "world.json").write_text(json.dumps(world.snapshot(), indent=2))

    for method in ("request_get", "request_post", "request_put", "request_patch", "request_delete", "request_put_multipart"):
        original = getattr(world, method)

        def recorded(*a, _original=original, **kw):
            try:
                return _original(*a, **kw)
            finally:
                save()

        setattr(world, method, recorded)
    save()

    async def login(ctx):
        ctx.last_login = time.monotonic()

    server.TuoniC2 = lambda **kwargs: world
    server.EventBridge = SyntheticBridge
    server.login = connection.login = login
    profiles = case.profiles if case else ("agent-ops",)
    cfg = replace(_settings(args.mode), tool_prefix=args.prefix,
                  profiles=frozenset(Profile(p) for p in profiles),
                  download_dir=str(args.folder / "downloads"))
    server.build_server(cfg).run()


if __name__ == "__main__":
    main()
