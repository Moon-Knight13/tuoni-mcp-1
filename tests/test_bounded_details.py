"""Oversized detail records remain bounded and preserve actionable identifiers."""

from __future__ import annotations

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tuoni_mcp.config import Profile, Settings
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import _fit_detail
from tuoni_mcp.server import build_server


def _payload(result) -> dict:
    return json.loads(next(item.text for item in result.content if item.type == "text"))


def test_a_record_that_already_fits_is_untouched() -> None:
    record = {"listener_id": 7, "name": "http-1", "configuration": {"port": 8080}}

    assert _fit_detail(record, "tuoni_listener_get") == record


def test_an_oversized_record_is_bounded_instead_of_refused() -> None:
    record = {
        "listener_id": 7,
        "name": "http-1",
        "configuration": {f"field_{index}": "x" * 400 for index in range(80)},
    }

    bounded = _fit_detail(record, "tuoni_listener_get")

    # The identifying fields survive; the bulk is structurally truncated.
    assert bounded["listener_id"] == 7
    assert bounded["name"] == "http-1"
    assert bounded != record
    from tuoni_mcp.protocol import would_fit_success

    assert would_fit_success(bounded, budget=DETAIL_RESULT_BUDGET)


@pytest.mark.parametrize(
    "record",
    [
        {"blob": "x" * 400_000},
        {f"key_{index}": "y" * 2_000 for index in range(500)},
        {"nested": {"deep": [{"row": "z" * 900} for _ in range(400)]}},
    ],
)
def test_even_an_extreme_record_comes_back_usable(record: dict) -> None:
    """Strings are truncated as well as collections, so nothing dead-ends."""

    from tuoni_mcp.protocol import would_fit_success

    bounded = _fit_detail(record, "tuoni_listener_get")

    assert would_fit_success(bounded, budget=DETAIL_RESULT_BUDGET)
    assert isinstance(bounded, dict) and bounded


async def test_a_fat_listener_detail_reaches_the_model_through_the_transport(
    monkeypatch, fake_c2
) -> None:
    import time

    from tuoni_mcp import connection, server as server_mod

    async def _login(context):
        context.last_login = time.monotonic()

    monkeypatch.setattr(connection, "login", _login)
    monkeypatch.setattr(server_mod, "TuoniC2", lambda *args, **kwargs: fake_c2)
    monkeypatch.setattr(server_mod, "login", _login)

    fake_c2.responses[("GET", "/api/v1/listeners/7")] = {
        "id": 7,
        "name": "http-1",
        "info": "HTTP listener",
        "status": "RUNNING",
        "plugin": "shelldot.listener.agent-reverse-http",
        "configuration": {f"field_{index}": "y" * 500 for index in range(120)},
        "configurationFiles": [],
    }
    cfg = Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        profiles=frozenset({Profile.ACCESS}),
    )
    server = build_server(cfg)

    async with create_connected_server_and_client_session(server.protocol_server) as session:
        result = await session.call_tool("tuoni_listener_get", {"listener_id": 7})

    assert result.isError is False
    payload = _payload(result)
    assert payload["data"]["name"] == "http-1"
    assert call_result_size(result) <= DETAIL_RESULT_BUDGET
