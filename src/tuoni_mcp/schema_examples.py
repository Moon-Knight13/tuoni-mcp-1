"""Bounded, explicit skeletons for live command configuration schemas.

These are editable examples, not a general JSON Schema satisfiability solver.
Field names stay exact, and omitted structure is always reported as incomplete.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import islice
from typing import Any
from urllib.parse import unquote

from tuoni_mcp.protocol import redact_text, should_redact_key

MAX_EXAMPLE_BYTES = 2048
MAX_NODES = 512
MAX_DEPTH = 12
MAX_FIELDS = 64
MAX_FILES = 32

_MAPS = {"properties", "$defs", "definitions", "files", "dependentSchemas"}
_KEYWORDS = _MAPS | {
    "type", "required", "items", "prefixItems", "allOf", "oneOf", "anyOf", "$ref",
    "const", "enum", "if", "then", "else", "dependentRequired", "dependencies", "not",
    "contains", "$id", "$dynamicRef", "$recursiveRef", "$anchor", "$dynamicAnchor",
    "minItems", "maxItems", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
}
_CONDITIONALS = {"if", "then", "else", "dependentRequired", "dependentSchemas", "dependencies", "not", "contains"}
_NUMERIC = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}


def _reference(root: Any, reference: str) -> Any:
    if reference == "#":
        return root
    if not reference.startswith("#/"):
        return None
    node = root
    for component in unquote(reference[2:]).split("/"):
        if re.search(r"~(?![01])", component):
            return None
        name = component.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and name in node:
            node = node[name]
        elif isinstance(node, list) and re.fullmatch(r"0|[1-9][0-9]*", name) and len(name) < 8 and int(name) < len(node):
            node = node[int(name)]
        else:
            return None
    return node


def _sensitive_nodes(root: Any) -> set[int]:
    """Follow bounded local references so shared numeric constraints stay private."""
    pending = [(root, False)]
    visited: set[tuple[int, bool]] = set()
    sensitive_ids: set[int] = set()
    unknown_sensitive_reference = False
    sensitive_reference = False
    nested_resource = False
    while pending:
        node, sensitive = pending.pop()
        if not isinstance(node, (dict, list)) or (id(node), sensitive) in visited:
            continue
        visited.add((id(node), sensitive))
        if sensitive:
            sensitive_ids.add(id(node))
        if isinstance(node, list):
            pending.extend((child, sensitive) for child in node)
            continue
        nested_resource |= node is not root and "$id" in node
        if sensitive:
            for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                reference = node.get(key)
                if isinstance(reference, str):
                    sensitive_reference = True
                    target = _reference(root, reference)
                    if target is None:
                        unknown_sensitive_reference = True
                    else:
                        pending.append((target, True))
        for key, child in node.items():
            if key in _MAPS and isinstance(child, dict):
                pending.extend((schema, sensitive or should_redact_key(name, ""))
                               for name, schema in child.items())
            elif key not in {"const", "enum"}:
                pending.append((child, sensitive))
    if unknown_sensitive_reference or nested_resource and sensitive_reference:
        return {identity for identity, _ in visited}
    return sensitive_ids


def _hide_numeric_constraints(original: Any, sanitized: Any, sensitive: set[int]) -> None:
    if isinstance(original, dict) and isinstance(sanitized, dict):
        if id(original) in sensitive:
            for key in _NUMERIC:
                sanitized.pop(key, None)
        for key, value in sanitized.items():
            if key in original:
                _hide_numeric_constraints(original[key], value, sensitive)
    elif isinstance(original, list) and isinstance(sanitized, list):
        for before, after in zip(original, sanitized):
            _hide_numeric_constraints(before, after, sensitive)


@dataclass(frozen=True)
class ExampleSkeleton:
    configuration: dict[str, Any]
    required_files: tuple[str, ...]
    incomplete: bool
    notes: tuple[str, ...]


def _size(configuration: Any, files: Any, incomplete: bool, notes: Any) -> int:
    return len(json.dumps({"configuration": configuration, "required_files": files,
                           "incomplete": incomplete, "notes": notes},
                          ensure_ascii=False, separators=(",", ":")).encode())


class _Builder:
    def __init__(self) -> None:
        self.incomplete = False
        self.notes: list[str] = []
        self.nodes = 0
        self.fields = 0
        self.projected_incomplete = False
        self.root: Any = {}
        self.hidden_literals: set[int] = set()

    def note(self, message: str, *, incomplete: bool = True) -> None:
        self.incomplete |= incomplete
        if message not in self.notes and len(self.notes) < 4:
            self.notes.append(message)

    def project(self, value: Any, depth: int = 0, *, names: bool = False,
                literal: bool = False, ancestors: tuple[int, ...] = ()) -> Any:
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH or id(value) in ancestors:
            self.projected_incomplete = True
            self.note("Schema traversal limit reached; inspect remaining describe fields.")
            return {}
        if isinstance(value, Mapping):
            result = {}
            count = 0
            for key, child in islice(value.items(), MAX_FIELDS + 1):
                count += 1
                if count > MAX_FIELDS:
                    self.projected_incomplete = True
                    self.note("Schema field limit reached; inspect remaining describe fields.")
                    break
                if not isinstance(key, str) or len(key.encode()) > 256 or redact_text(key) != key:
                    self.projected_incomplete = True
                    self.note("A schema identifier cannot fit safely in this example.")
                    continue
                if names or literal or key in _KEYWORDS:
                    schema_keyword = not names and not literal
                    result[key] = self.project(child, depth + 1, names=schema_keyword and key in _MAPS,
                                               literal=literal or schema_keyword and key in {"const", "enum"},
                                               ancestors=(*ancestors, id(value)))
            return result
        if isinstance(value, list):
            if len(value) > MAX_FIELDS:
                self.projected_incomplete = True
                self.note("Schema item limit reached; inspect remaining describe fields.")
            return [self.project(item, depth + 1, literal=literal, ancestors=(*ancestors, id(value)))
                    for item in value[:MAX_FIELDS]]
        if isinstance(value, str) and len(value.encode()) > 256:
            self.projected_incomplete = True
            self.note("A schema value exceeds the example limit.")
            return None
        if type(value) is int and value.bit_length() > 256:
            self.projected_incomplete = True
            self.note("A schema value exceeds the example limit.")
            return None
        return value

    def strip_literals(self, node: Any, *, names: bool = False) -> None:
        if isinstance(node, dict):
            for key, child in list(node.items()):
                if not names and key in {"const", "enum", *_NUMERIC}:
                    del node[key]
                else:
                    self.strip_literals(child, names=not names and key in _MAPS)
        elif isinstance(node, list):
            for child in node:
                self.strip_literals(child)

    def resolve(self, reference: str) -> Any:
        node = _reference(self.root, reference)
        if node is None:
            self.note("A reference cannot be resolved locally; inspect its describe fields.")
            return {}
        return node

    def mark_hidden_literals(self, original: Any, sanitized: Any, *, names: bool = False) -> None:
        if isinstance(original, dict) and isinstance(sanitized, dict):
            if not names and any(key in original and (key not in sanitized or original[key] != sanitized[key])
                                 for key in {"const", "enum", *_NUMERIC}):
                self.hidden_literals.add(id(sanitized))
            for key, value in sanitized.items():
                if key in original and (names or key not in {"const", "enum"}):
                    self.mark_hidden_literals(original[key], value, names=not names and key in _MAPS)
        elif isinstance(original, list) and isinstance(sanitized, list):
            for before, after in zip(original, sanitized):
                self.mark_hidden_literals(before, after)

    def merge(self, left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
        merged = dict(left)
        for key, value in right.items():
            old = merged.get(key)
            if key in {"properties", "files"} and isinstance(old, Mapping) and isinstance(value, Mapping):
                properties = dict(old)
                for name, schema in value.items():
                    if name in properties and properties[name] != schema:
                        previous = properties[name]
                        if key == "files" and isinstance(previous, Mapping) and isinstance(schema, Mapping):
                            properties[name] = {**previous, **schema,
                                                "required": bool(previous.get("required") or schema.get("required"))}
                        else:
                            properties[name] = {"allOf": [previous, schema]}
                    else:
                        properties[name] = schema
                merged[key] = properties
            elif key == "required" and isinstance(old, list) and isinstance(value, list):
                merged[key] = list(dict.fromkeys([*old, *value]))
            elif key == "const" and key in merged and old != value:
                self.note("Branch constraints conflict; choose configuration from describe.")
            elif key == "enum" and isinstance(old, list) and isinstance(value, list):
                merged[key] = [item for item in old if item in value]
                if not merged[key]:
                    self.note("Branch constraints conflict; choose configuration from describe.")
            elif key in {"minimum", "exclusiveMinimum"} and type(old) in {int, float} and type(value) in {int, float}:
                merged[key] = max(old, value)
            elif key in {"maximum", "exclusiveMaximum"} and type(old) in {int, float} and type(value) in {int, float}:
                merged[key] = min(old, value)
            else:
                merged[key] = value
        return merged

    def expand(self, schema: Any, refs: tuple[str, ...] = (), depth: int = 0) -> tuple[dict[str, Any], tuple[str, ...]]:
        self.nodes += 1
        if self.nodes > MAX_NODES * 2 or depth > MAX_DEPTH:
            self.note("Schema expansion limit reached; inspect remaining describe fields.")
            return {}, refs
        if schema is True:
            return {}, refs
        if not isinstance(schema, Mapping):
            self.note("A schema branch needs manual configuration.")
            return {}, refs
        if id(schema) in self.hidden_literals:
            self.note("Supply sensitive values explicitly; some schema literals are hidden.")
        if _CONDITIONALS.intersection(schema):
            self.note("Conditional constraints need review in describe before preparation.")
        if {"$id", "$dynamicRef", "$recursiveRef"}.intersection(schema):
            self.note("Scoped or dynamic references need review in describe.")
        result = {key: value for key, value in schema.items() if key not in {"$ref", "allOf", "oneOf", "anyOf"}}
        reference = schema.get("$ref")
        if isinstance(reference, str):
            if reference in refs:
                self.note("Recursive fields need manual configuration.")
            else:
                refs = (*refs, reference)
                target, refs = self.expand(self.resolve(reference), refs, depth + 1)
                result = self.merge(target, result)
        for branch in schema.get("allOf", []) if isinstance(schema.get("allOf"), list) else []:
            expanded, _ = self.expand(branch, refs, depth + 1)
            result = self.merge(result, expanded)
        for keyword in ("oneOf", "anyOf"):
            branches = schema.get(keyword)
            if isinstance(branches, list) and branches:
                self.note(f"The first {keyword} branch is shown; inspect alternatives before choosing.", incomplete=False)
                branch, _ = self.expand(branches[0], refs, depth + 1)
                result = self.merge(result, branch)
        return result, refs

    def number(self, schema: Mapping[str, Any], *, integer: bool) -> int | float:
        def bound(key):
            value = schema.get(key)
            return value if type(value) in {int, float} and math.isfinite(value) else None

        minimum, maximum = bound("minimum"), bound("maximum")
        exclusive_minimum, exclusive_maximum = bound("exclusiveMinimum"), bound("exclusiveMaximum")
        if exclusive_minimum is not None:
            lower = math.floor(exclusive_minimum) + 1 if integer else math.nextafter(exclusive_minimum, math.inf)
            minimum = lower if minimum is None else max(minimum, lower)
        if exclusive_maximum is not None:
            upper = math.ceil(exclusive_maximum) - 1 if integer else math.nextafter(exclusive_maximum, -math.inf)
            maximum = upper if maximum is None else min(maximum, upper)
        if integer:
            minimum = math.ceil(minimum) if minimum is not None else None
            maximum = math.floor(maximum) if maximum is not None else None
        value = minimum if minimum is not None else min(1, maximum) if maximum is not None else 1
        multiple = bound("multipleOf")
        if multiple is not None and multiple > 0:
            value = math.ceil(value / multiple) * multiple
        if ((maximum is not None and value > maximum) or not math.isfinite(value)
                or integer and value != math.floor(value)):
            self.note("Numeric constraints need a value selected from describe.")
            return 1
        return int(value) if integer else value

    def value(self, schema: Any, name: str, refs: tuple[str, ...] = (), depth: int = 0) -> Any:
        if depth > MAX_DEPTH:
            self.note("Nested fields exceed the example depth; inspect describe.")
            return {}
        schema, refs = self.expand(schema, refs, depth)
        sensitive = should_redact_key(name, "")
        if not sensitive:
            literal = schema.get("const") if "const" in schema else None
            present = "const" in schema
            if not present and isinstance(schema.get("enum"), list) and schema["enum"]:
                literal, present = schema["enum"][0], True
            if present:
                if "[REDACTED]" not in json.dumps(literal, ensure_ascii=False):
                    return literal
                self.note("A sensitive literal was omitted; supply a value from describe.")
        value_type = schema.get("type")
        if isinstance(value_type, list):
            value_type = next((kind for kind in value_type if kind != "null"), "null")
        if value_type is not None and not isinstance(value_type, str):
            self.note("A schema type needs manual configuration.")
            value_type = None
        if value_type == "object" or isinstance(schema.get("properties"), Mapping):
            properties = schema.get("properties") or {}
            required = schema.get("required") if isinstance(schema.get("required"), list) else []
            result = {}
            for child in required:
                if not isinstance(child, str) or child in result:
                    continue
                if len(child.encode()) > 256 or redact_text(child) != child:
                    self.note("A required field name cannot fit safely; inspect describe.")
                    continue
                self.fields += 1
                if self.fields > MAX_FIELDS:
                    self.note("Required fields exceed the example limit; inspect describe.")
                    break
                result[child] = self.value(properties.get(child, {}), child, refs, depth + 1)
            return result
        if value_type == "array":
            minimum = schema.get("minItems", 1)
            count = max(1, minimum) if type(minimum) is int else 1
            if schema.get("maxItems") == 0:
                count = 0
            if count > 16:
                self.note("Required array items exceed the example limit; inspect describe.")
            prefixes = schema.get("prefixItems") if isinstance(schema.get("prefixItems"), list) else []
            return [self.value(prefixes[index] if index < len(prefixes) else schema.get("items", {}),
                               "item", refs, depth + 1) for index in range(min(count, 16))]
        if value_type == "boolean":
            return True
        if value_type in {"integer", "number"}:
            return self.number(schema, integer=value_type == "integer")
        if value_type == "null":
            return None
        token = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()[:64] or "VALUE"
        return f"REPLACE_WITH_{token}"


def build_example(schema: Mapping[str, Any]) -> ExampleSkeleton:
    """Return an editable object skeleton within a 2 KiB serialized budget.

    Local references and required object fields are expanded. A first branch is
    explicitly selected for oneOf/anyOf. Unsupported constraints, unresolved
    references, or bounded omissions are reported rather than disguised.
    """
    # The tools package imports command handlers, which themselves use this
    # builder. Delay the shared schema sanitizer until this module is initialized.
    from tuoni_mcp.tools._json_detail import redact_schema

    builder = _Builder()
    projected = builder.project(schema)
    if builder.projected_incomplete:
        # A dropped sensitive reference could share a definition with an ordinary
        # field. Never retain schema literals when that privacy context is lost.
        builder.strip_literals(projected)
    builder.root = redact_schema(projected)
    _hide_numeric_constraints(projected, builder.root, _sensitive_nodes(projected))
    builder.mark_hidden_literals(projected, builder.root)
    configuration = builder.value(builder.root, "configuration")
    if not isinstance(configuration, dict):
        builder.note("The configuration root must be supplied as an object.")
        configuration = {}
    expanded, _ = builder.expand(builder.root)
    required_files = []
    files = expanded.get("files")
    for name, specification in files.items() if isinstance(files, Mapping) else []:
        if not isinstance(specification, Mapping) or not specification.get("required"):
            continue
        if len(name) > 128:
            builder.note("An attachment field cannot fit the input contract; inspect describe.")
            continue
        if len(required_files) == MAX_FILES:
            builder.note("Required attachments exceed the example limit; inspect describe.")
            break
        required_files.append(name)
    while _size(configuration, required_files, builder.incomplete, builder.notes) > MAX_EXAMPLE_BYTES:
        builder.note("The complete example exceeds its byte budget; inspect remaining describe fields.")
        if configuration:
            configuration.pop(next(reversed(configuration)))
        elif required_files:
            required_files.pop()
        else:
            break
    return ExampleSkeleton(configuration, tuple(required_files), builder.incomplete, tuple(builder.notes))
