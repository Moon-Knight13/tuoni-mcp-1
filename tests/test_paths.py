from __future__ import annotations

import requests

from tuoni_mcp.paths import url_path_segment


def test_url_path_segment_cannot_escape_its_route_segment() -> None:
    encoded = url_path_segment("agent/../admin?force=true#fragment")

    assert encoded == (
        "agent%2F..%2Fadmin%3Fforce%3Dtrue%23fragment"
    )
    assert all(character not in encoded for character in "/?#")


def test_requests_cannot_normalize_exact_dot_segments_out_of_the_route() -> None:
    for raw in (".", ".."):
        encoded = url_path_segment(raw)
        prepared = requests.Request(
            "PUT",
            f"https://example.test/api/v1/agents/{encoded}/block",
        ).prepare()

        assert prepared.url is not None
        assert "/agents/./" not in prepared.url
        assert "/agents/../" not in prepared.url
        assert prepared.url.endswith("/block")


def test_url_path_segment_preserves_normal_identifiers_semantically() -> None:
    assert url_path_segment("agent-123_ABC") == "agent-123_ABC"
    assert url_path_segment("operator@example.test") == (
        "operator%40example.test"
    )
