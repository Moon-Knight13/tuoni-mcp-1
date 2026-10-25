"""Generated examples keep required structure and make bounded omissions explicit."""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from tuoni_mcp.schema_examples import MAX_EXAMPLE_BYTES, build_example


def _assert_bounded(result):
    assert len(json.dumps(asdict(result), ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_EXAMPLE_BYTES


def test_keeps_seven_root_required_fields_and_five_required_nested_fields():
    names = [f"field_{index}" for index in range(7)]
    nested = [f"nested_{index}" for index in range(5)]
    schema = {
        "type": "object", "required": [*names, "nested"],
        "properties": {**{name: {"type": "string"} for name in names},
                       "nested": {"type": "object", "required": nested,
                                  "properties": {name: {"type": "integer"} for name in nested}}},
    }

    result = build_example(schema)

    assert set(result.configuration) == {*names, "nested"}
    assert set(result.configuration["nested"]) == set(nested)
    assert not result.incomplete
    _assert_bounded(result)


def test_local_references_and_allof_keep_all_required_members():
    schema = {
        "$defs": {"shared": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                              "required": ["enabled"]}},
        "allOf": [{"$ref": "#/$defs/shared"},
                  {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]}],
    }

    result = build_example(schema)

    assert result.configuration == {"enabled": True, "count": 1}
    assert not result.incomplete


@pytest.mark.parametrize("keyword", ["oneOf", "anyOf"])
def test_branch_discriminator_and_required_fields_are_selected_explicitly(keyword):
    schema = {
        "type": "object", "required": ["label"], "properties": {"label": {"type": "string"}},
        keyword: [
            {"required": ["mode", "name"],
             "properties": {"mode": {"const": "first"}, "name": {"type": "string"}}},
            {"required": ["mode", "number"],
             "properties": {"mode": {"const": "second"}, "number": {"type": "integer"}}},
        ],
    }

    result = build_example(schema)

    assert result.configuration == {"label": "REPLACE_WITH_LABEL", "mode": "first", "name": "REPLACE_WITH_NAME"}
    assert any(keyword in note for note in result.notes)
    assert not result.incomplete


def test_types_enum_const_arrays_and_null_are_preserved():
    properties = {
        "mode": {"type": "string", "enum": ["safe", "other"]},
        "fixed": {"const": 17}, "enabled": {"type": "boolean"}, "count": {"type": "integer"},
        "amount": {"type": "number"}, "nothing": {"type": "null"},
        "values": {"type": "array", "minItems": 2, "items": {"type": "integer"}},
    }

    result = build_example({"type": "object", "properties": properties, "required": list(properties)})

    assert result.configuration == {"mode": "safe", "fixed": 17, "enabled": True, "count": 1,
                                    "amount": 1, "nothing": None, "values": [1, 1]}
    assert not result.incomplete


def test_required_attachment_names_are_not_limited_to_four():
    names = [f"file_{index}" for index in range(7)]

    result = build_example({"type": "object", "files": {name: {"required": True} for name in names}})

    assert result.required_files == tuple(names)
    assert not result.incomplete


@pytest.mark.parametrize("reference", ["https://example.test/schema", "file:///etc/passwd", "#/$defs/missing"])
def test_unresolved_references_are_explicitly_incomplete_and_never_fetched(reference):
    result = build_example({"type": "object", "required": ["value"],
                            "properties": {"value": {"$ref": reference}}})

    assert result.incomplete
    assert any("reference" in note for note in result.notes)
    _assert_bounded(result)


def test_recursive_references_and_cyclic_input_remain_bounded():
    schema = {"type": "object", "required": ["child"], "properties": {"child": {"$ref": "#"}}}

    result = build_example(schema)

    assert result.incomplete
    _assert_bounded(result)
    cyclic = {"type": "object", "required": ["child"], "properties": {}}
    cyclic["properties"]["child"] = cyclic
    cyclic_result = build_example(cyclic)
    assert cyclic_result.incomplete
    _assert_bounded(cyclic_result)


def test_huge_schema_never_truncates_field_names_into_different_keys():
    names = [f"exact_field_{index}_" + "long" * 15 for index in range(1000)]
    schema = {"type": "object", "required": names,
              "properties": {name: {"type": "string"} for name in names}}

    result = build_example(schema)

    assert result.incomplete
    assert set(result.configuration) <= set(names)
    _assert_bounded(result)


def test_conditional_structure_is_not_presented_as_a_complete_solution():
    result = build_example({"type": "object", "if": {"required": ["mode"]},
                            "then": {"required": ["conditional_value"]}})

    assert result.incomplete
    assert any("Conditional" in note for note in result.notes)


def test_sensitive_literals_and_shared_sensitive_definition_never_enter_the_example():
    schema = {
        "type": "object", "required": ["password", "ordinary", "mode"],
        "properties": {"password": {"$ref": "#/$defs/shared"}, "ordinary": {"$ref": "#/$defs/shared"},
                       "mode": {"type": "string", "enum": ["public"]}},
        "$defs": {"shared": {"type": "string", "const": "private-secret-literal", "minLength": 24}},
    }

    result = build_example(schema)

    assert result.configuration == {"password": "REPLACE_WITH_PASSWORD", "ordinary": "REPLACE_WITH_ORDINARY",
                                    "mode": "public"}
    assert "private-secret-literal" not in json.dumps(asdict(result))
    assert result.incomplete
    assert any("sensitive" in note for note in result.notes)


def test_projection_limits_remove_literals_when_sensitive_reference_context_may_be_missing():
    schema = {
        "type": "object", "required": ["ordinary"],
        "properties": {"ordinary": {"$ref": "#/$defs/shared"},
                       **{f"optional_{index}": {"type": "string"} for index in range(1000)},
                       "password": {"$ref": "#/$defs/shared"}},
        "$defs": {"shared": {"type": "string", "const": "private-secret-literal"}},
    }

    result = build_example(schema)

    assert result.incomplete
    assert "private-secret-literal" not in json.dumps(asdict(result))
    _assert_bounded(result)


def test_too_long_exact_field_identifier_is_omitted_and_marked_incomplete():
    name = "x" * 1000

    result = build_example({"type": "object", "required": [name], "properties": {name: {"type": "string"}}})

    assert result.incomplete
    assert result.configuration == {}
    _assert_bounded(result)


def test_literal_keywords_used_as_property_names_remain_exact_when_schema_is_bounded():
    properties = {"const": {"type": "integer"}, "enum": {"type": "boolean"},
                  **{f"extra_{index}": {"type": "string"} for index in range(1000)}}

    result = build_example({"type": "object", "required": ["const", "enum"], "properties": properties})

    assert result.configuration == {"const": 1, "enum": True}
    assert result.incomplete


def test_allof_intersects_enum_constraints_instead_of_selecting_an_invalid_literal():
    schema = {"type": "object", "required": ["mode"], "properties": {"mode": {
        "allOf": [{"type": "string", "enum": ["a", "b"]}, {"enum": ["c", "b"]}],
    }}}

    result = build_example(schema)

    assert result.configuration == {"mode": "b"}
    assert not result.incomplete


def test_allof_does_not_lose_required_attachment_identity():
    result = build_example({"type": "object", "allOf": [
        {"files": {"input": {"required": True}}},
        {"files": {"input": {"description": "public annotation"}}},
    ]})

    assert result.required_files == ("input",)


def test_boolean_and_malformed_type_schemas_are_handled_without_crashing():
    result = build_example({"type": "object", "required": ["anything", "invalid"],
                            "properties": {"anything": True, "invalid": {"type": {"wrong": "shape"}}}})

    assert set(result.configuration) == {"anything", "invalid"}
    assert result.incomplete
    _assert_bounded(result)


@pytest.mark.parametrize("bounds,expected", [
    ({"minimum": 1024}, 1024), ({"maximum": 0}, 0),
    ({"exclusiveMinimum": 1024}, 1025), ({"exclusiveMaximum": 0}, -1),
    ({"minimum": 1025, "multipleOf": 10}, 1030),
])
def test_numeric_placeholders_respect_bounded_public_constraints(bounds, expected):
    result = build_example({"type": "object", "required": ["port"],
                            "properties": {"port": {"type": "integer", **bounds}}})

    assert result.configuration == {"port": expected}
    assert not result.incomplete


def test_shared_sensitive_numeric_constraints_are_not_copied_into_ordinary_fields():
    schema = {"type": "object", "required": ["ordinary"],
              "properties": {"password": {"$ref": "#/$defs/shared"}, "ordinary": {"$ref": "#/$defs/shared"}},
              "$defs": {"shared": {"type": "integer", "minimum": 731987}}}

    result = build_example(schema)

    assert result.configuration == {"ordinary": 1}
    assert result.incomplete
    assert "731987" not in json.dumps(asdict(result))


def test_projection_limits_remove_numeric_constraints_if_private_context_was_omitted():
    schema = {"type": "object", "required": ["ordinary"],
              "properties": {"ordinary": {"type": "integer", "minimum": 731987},
                             **{f"optional_{index}": {"type": "string"} for index in range(1000)}}}

    result = build_example(schema)

    assert result.configuration == {"ordinary": 1}
    assert result.incomplete
    assert "731987" not in json.dumps(asdict(result))
