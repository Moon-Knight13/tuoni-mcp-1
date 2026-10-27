"""Tests for the tuoni_api_spec tool — offline, no C2 server needed."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tuoni_mcp.tools.api_spec import ApiSpecInput, _HTTP_METHODS


# -- Input validation --

def test_empty_filter_accepted() -> None:
    inp = ApiSpecInput(filter="")
    assert inp.filter == ""


def test_default_filter_is_empty() -> None:
    inp = ApiSpecInput()
    assert inp.filter == ""


def test_filter_max_length_enforced() -> None:
    with pytest.raises(Exception):
        ApiSpecInput(filter="x" * 257)


def test_filter_within_max_length() -> None:
    inp = ApiSpecInput(filter="x" * 256)
    assert len(inp.filter) == 256


# -- Spec parsing helpers --

SAMPLE_SPEC = {
    "info": {"version": "0.17.0", "title": "Tuoni API"},
    "paths": {
        "/api/v1/agents": {
            "get": {"summary": "List agents", "tags": ["Agents"]},
        },
        "/api/v1/agents/{guid}": {
            "get": {"summary": "Get agent by GUID", "tags": ["Agents"]},
        },
        "/api/v1/listeners": {
            "get": {"summary": "List listeners", "tags": ["Listeners"]},
            "post": {"summary": "Create listener", "tags": ["Listeners"]},
        },
        "/api/v2/auth/login": {
            "post": {"summary": "Authenticate", "tags": ["Auth"]},
        },
    },
}


def test_http_methods_filter_non_operations() -> None:
    assert "get" in _HTTP_METHODS
    assert "post" in _HTTP_METHODS
    assert "parameters" not in _HTTP_METHODS
    assert "servers" not in _HTTP_METHODS


def test_exact_path_match_case_insensitive() -> None:
    paths = SAMPLE_SPEC["paths"]
    query = "/api/v1/agents"
    path_lower_map = {p.lower(): p for p in paths}
    assert query.lower() in path_lower_map


def test_filter_narrows_by_tag() -> None:
    paths = SAMPLE_SPEC["paths"]
    query = "listener"
    matches = []
    for path in sorted(paths):
        for method, operation in paths[path].items():
            if method not in _HTTP_METHODS:
                continue
            tag = (operation.get("tags") or [""])[0]
            summary = operation.get("summary", "")
            if query and any(query in text.lower() for text in (path, tag, summary)):
                matches.append(f"{method.upper()} {path}")
    assert len(matches) == 2
    assert "GET /api/v1/listeners" in matches
    assert "POST /api/v1/listeners" in matches


def test_filter_no_match_returns_empty() -> None:
    paths = SAMPLE_SPEC["paths"]
    query = "nonexistent"
    matches = [
        path
        for path in paths
        for method, op in paths[path].items()
        if method in _HTTP_METHODS
        and any(query in t.lower() for t in (path, (op.get("tags") or [""])[0], op.get("summary", "")))
    ]
    assert len(matches) == 0


def test_unfiltered_lists_all_operations() -> None:
    paths = SAMPLE_SPEC["paths"]
    count = sum(
        1
        for path in paths
        for method in paths[path]
        if method in _HTTP_METHODS
    )
    assert count == 5
