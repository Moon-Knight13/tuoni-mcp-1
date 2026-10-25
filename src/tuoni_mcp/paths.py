from __future__ import annotations

from typing import Any
from urllib.parse import quote


def url_path_segment(value: Any) -> str:
    """Encode one untrusted identifier as exactly one URL path segment.

    ``urllib.parse.quote(..., safe="")`` intentionally leaves RFC-unreserved
    dots alone, while ``requests`` canonicalizes singly encoded dot segments.
    Double-encode the two traversal-only values so the prepared request keeps
    them inside the identifier segment.
    """
    text = str(value)
    if text == ".":
        return "%252E"
    if text == "..":
        return "%252E%252E"
    return quote(text, safe="")
