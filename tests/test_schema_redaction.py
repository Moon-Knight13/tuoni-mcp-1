"""Privacy and structural fidelity for schema-only metadata reads."""

import copy
import json

import pytest

from tuoni_mcp.tools._json_detail import redact_schema


def _literal_schema():
    return {"type": "string", "minLength": 8, "default": "sensitive-default",
            "enum": ["sensitive-enum"], "const": "sensitive-const", "examples": ["sensitive-example"]}


@pytest.mark.parametrize("reference", ["#/$defs/value/allOf/0", "#%2F$defs%2Fvalue%2FallOf%2F0"])
def test_sensitive_array_pointer_preserves_public_constraints(reference):
    schema = {"properties": {"normal": {"$ref": reference}, "password": {"$ref": reference}},
              "$defs": {"value": {"allOf": [_literal_schema()]}, "public": {"enum": ["red", "blue"]}}}
    original = copy.deepcopy(schema)
    clean = redact_schema(schema)
    assert clean["$defs"]["value"]["allOf"] == [{"type": "string", "minLength": 8}]
    assert clean["$defs"]["public"] == schema["$defs"]["public"]
    assert clean["properties"] == schema["properties"]
    assert "sensitive-" not in json.dumps(clean)
    assert schema == original


@pytest.mark.parametrize("keyword,anchor", [("$ref", "$anchor"), ("$dynamicRef", "$dynamicAnchor")])
def test_sensitive_named_anchor_shared_and_cyclic_references(keyword, anchor):
    schema = {"properties": {"normal": {keyword: "#value"}, "password": {keyword: "#wrapper"}},
              "$defs": {"wrapper": {anchor: "wrapper", "allOf": [{keyword: "#value"}]},
                        "value": {anchor: "value", **_literal_schema(), "allOf": [{keyword: "#wrapper"}]},
                        "public": {"enum": ["red", "blue"]}}}
    clean = redact_schema(schema)
    assert "sensitive-" not in json.dumps(clean)
    assert clean["$defs"]["value"]["minLength"] == 8
    assert clean["$defs"]["public"] == schema["$defs"]["public"]
    assert clean["properties"] == schema["properties"]


def test_dynamic_reference_redacts_every_possible_anchor_target():
    schema = {"properties": {"password": {"$dynamicRef": "#value"}},
              "$defs": {name: {"$dynamicAnchor": "value", **_literal_schema()} for name in ("first", "second")}}
    clean = redact_schema(schema)
    assert "sensitive-" not in json.dumps(clean)
    assert all(item["type"] == "string" for item in clean["$defs"].values())


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_sensitive_root_reference_terminates_and_removes_root_literals(keyword):
    schema = {"$recursiveAnchor": True, "properties": {"password": {keyword: "#"}}, "default": {"a": "secret"}}
    clean = redact_schema(schema)
    assert clean == {"$recursiveAnchor": True, "properties": {"password": {keyword: "#"}}}


@pytest.mark.parametrize("reference", [
    "#/$defs/value/allOf/00", "#/$defs/value/allOf/01", "#/$defs/value/allOf/-", "#/$defs/value/allOf/true",
    "#/$defs/value/allOf/-1", "#/$defs/value/allOf/1", "#/$defs/value/allOf/" + "9" * 5000,
    "#/$defs/value~2", "#missing", "https://invalid.example/schema#value",
])
def test_unresolved_sensitive_reference_conservatively_omits_literals(reference):
    schema = {"properties": {"password": {"$ref": reference}},
              "$defs": {"value": {"allOf": [_literal_schema()]}, "public": {"enum": ["red", "blue"]}}}
    clean = redact_schema(schema)
    assert clean["properties"]["password"]["$ref"] == reference
    assert clean["$defs"]["value"]["allOf"] == [{"type": "string", "minLength": 8}]
    assert clean["$defs"]["public"] == {}


def test_nested_schema_resource_fails_closed_instead_of_resolving_wrong_root():
    schema = {"properties": {"password": {"$ref": "#/$defs/value"}},
              "$defs": {"value": {"type": "string"},
                        "resource": {"$id": "nested", "$defs": {"value": _literal_schema()}}}}
    clean = redact_schema(schema)
    assert "sensitive-" not in json.dumps(clean)
    assert clean["$defs"]["resource"]["$defs"]["value"] == {"type": "string", "minLength": 8}


def test_schema_property_and_definition_names_are_not_annotation_keywords():
    names = ["default", "enum", "const", "examples", "token=value"]
    schema = {"type": "object", "required": names, "dependentRequired": {"token=value": names},
              "properties": {name: {"type": "string", "description": "Field"} for name in names},
              "$defs": {name: {"type": "string", "enum": ["red", "blue"]} for name in names[:4]}}
    assert redact_schema(schema) == schema


def test_escaped_pointer_and_secret_named_property_preserve_identity():
    schema = {"properties": {"apiToken": {"$ref": "#/$defs/setting~1with~0slash"}},
              "required": ["apiToken"], "$defs": {"setting/with~slash": _literal_schema()}}
    clean = redact_schema(schema)
    assert clean["properties"] == schema["properties"]
    assert clean["required"] == ["apiToken"]
    assert clean["$defs"]["setting/with~slash"] == {"type": "string", "minLength": 8}
