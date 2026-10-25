from __future__ import annotations

from tuoni import TuoniListener

from tuoni_mcp import serialize


def test_listener_to_dict(fake_c2):
    raw = {
        "id": 7,
        "name": "http-1",
        "info": "HTTP listener",
        "status": "RUNNING",
        "plugin": "shelldot.listener.agent-reverse-http",
        "configuration": {"port": 8080},
        "configurationFiles": [],
    }
    out = serialize.listener_to_dict(TuoniListener(raw, fake_c2))
    assert out == {
        "listener_id": 7,
        "name": "http-1",
        "info": "HTTP listener",
        "status": "RUNNING",
        "plugin": "shelldot.listener.agent-reverse-http",
        "configuration": {"port": 8080},
        "configuration_files": [],
    }
