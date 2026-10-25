from __future__ import annotations

from jsonschema.exceptions import ValidationError
import pytest

import tuoni_mcp.tools.commands as commands_module
from tuoni_mcp.registry import ToolFailure


def test_validation_errors_sort_array_and_object_paths_without_mixed_type_comparison(
    monkeypatch,
) -> None:
    class FakeValidator:
        @classmethod
        def check_schema(cls, schema) -> None:
            del schema

        def __init__(self, schema, **kwargs) -> None:
            del schema, kwargs

        def iter_errors(self, configuration):
            del configuration
            # Comparing the raw paths [0] and ["field"] raises TypeError on
            # Python 3.  Both are legitimate jsonschema path segment types.
            return [
                ValidationError("object field", validator="type", path=["field"]),
                ValidationError("array item", validator="type", path=[0]),
            ]

    monkeypatch.setattr(commands_module, "validator_for", lambda schema: FakeValidator)

    errors = commands_module._validation_errors({"type": "object"}, {})

    assert errors == [
        "configuration[0]: does not satisfy type constraint",
        "configuration.field: does not satisfy type constraint",
    ]


@pytest.mark.parametrize("reference", ["http://example.test/schema", "file:///etc/passwd"])
def test_external_schema_references_never_fetch_network_or_local_files(monkeypatch, reference):
    import urllib.request

    requests = []

    def retrieve(uri, *args, **kwargs):
        requests.append(uri)
        raise AssertionError("External retrieval must not happen")

    monkeypatch.setattr(urllib.request, "urlopen", retrieve)
    with pytest.raises(ToolFailure) as error:
        commands_module._validation_errors({"$ref": reference}, {})
    assert error.value.code == "INVALID_TEMPLATE_SCHEMA"
    assert not requests


def test_bundled_schema_references_still_validate_real_values():
    schema = {
        "$defs": {"number": {"type": "integer", "minimum": 1}},
        "type": "object", "properties": {"value": {"$ref": "#/$defs/number"}},
    }
    assert commands_module._validation_errors(schema, {"value": 2}) == []
    assert commands_module._validation_errors(schema, {"value": 0}) == [
        "configuration.value: does not satisfy minimum constraint"
    ]


def test_large_error_sets_stop_after_a_bounded_number_of_violations(monkeypatch):
    checked = []

    class Validator:
        @classmethod
        def check_schema(cls, schema):
            pass

        def __init__(self, schema, **kwargs):
            pass

        def iter_errors(self, configuration):
            for index in range(100000):
                checked.append(index)
                yield ValidationError("bad value", validator="type", path=[index])

    monkeypatch.setattr(commands_module, "validator_for", lambda schema: Validator)
    assert len(commands_module._validation_errors({"type": "object"}, {})) == 8
    assert len(checked) == 8
