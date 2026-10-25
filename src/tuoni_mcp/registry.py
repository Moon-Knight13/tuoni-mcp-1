from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from functools import cached_property
from types import ModuleType
from typing import Any, ClassVar, Protocol, get_type_hints

from mcp.server.fastmcp import Context
from mcp.types import CallToolResult, Tool, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, model_validator

from tuoni_mcp.command_intents import is_template_query
from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext, authentication_secrets, ensure_fresh_token, mutation_once_only
from tuoni_mcp.errors import classify, classify_rejection
from tuoni_mcp.mutations import MutationJournal, mutation_scope
from tuoni_mcp.protocol import (
    DETAIL_RESULT_BUDGET,
    ERROR_RESULT_BUDGET,
    LIST_RESULT_BUDGET,
    MAX_RESULT_BUDGET,
    dispatch_continuations,
    enforce_result_budget,
    error_result,
    exposed_call,
    exposed_tool_prefix,
    native_name,
    should_redact_key,
    success_result,
    validation_schema_call,
    would_fit_success,
)
from tuoni_mcp.pagination import (
    CursorStore,
    CursorError,
    cursor_scope,
    decode_cursor as decode_page_cursor,
    encode_cursor as encode_page_cursor,
    page_coverage,
    redact as bounded_redact,
)


class Effect(str, Enum):
    READ = "read"
    PREPARE = "prepare"
    EXECUTE = "execute"
    MUTATE = "mutate"


class ToolInput(BaseModel):
    """Base class for first-party tool arguments."""

    model_config = ConfigDict(extra="forbid")


def _input_alias_schema(schema: dict[str, Any], model: type[AliasedToolInput]) -> None:
    groups: dict[str, list[str]] = {}
    for alias, canonical in model.input_aliases.items():
        schema["properties"][alias] = {
            key: value for key, value in schema["properties"][canonical].items()
            if key not in {"description", "title", "default"}
        }
        groups.setdefault(canonical, [canonical]).append(alias)
    required = schema.get("required", [])
    for canonical, names in groups.items():
        if canonical in required:
            required.remove(canonical)
            choice = {"anyOf": [{"required": [name]} for name in names]}
            if "anyOf" not in schema:
                schema.update(choice)
            else:
                schema.setdefault("allOf", []).append(choice)
    if not required:
        schema.pop("required", None)


class AliasedToolInput(ToolInput):
    """Explicit compatibility aliases; conflicts still fail validation."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_input_alias_schema)
    input_aliases: ClassVar[dict[str, str]] = {}

    @model_validator(mode="before")
    @classmethod
    def coalesce_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        for alias, canonical in cls.input_aliases.items():
            if alias in result:
                if canonical in result and result[canonical] != result[alias]:
                    raise ValueError("Conflicting argument aliases")
                result[canonical] = result.pop(alias)
        return result


@dataclass(frozen=True)
class InvocationContext:
    context: Context
    server: ServerContext


ToolHandler = Callable[[InvocationContext, BaseModel], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSuccess:
    data: Any
    next_cursor: str | None = None
    next_call: dict[str, Any] | None = None
    # Static, project-owned schemas contain sensitive-looking *field names* but
    # no runtime values. They must remain exact for dispatch clients.
    redact_output: bool = True


class ToolFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        hint: str = "",
        next_call: dict[str, Any] | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.hint = hint
        self.next_call = next_call
        self.outcome = outcome


def annotations_for(effect: Effect) -> ToolAnnotations:
    if effect is Effect.READ:
        return ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    if effect is Effect.PREPARE:
        return ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        )
    if effect is Effect.EXECUTE:
        return ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        )
    return ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )


_SCHEMA_MAP_KEYS = {"$defs", "definitions", "properties", "patternProperties", "dependentSchemas"}
_SCHEMA_DATA_KEYS = {"default", "const", "enum", "examples"}


def _strip_schema_titles(value: Any, *, parent_key: str | None = None) -> Any:
    """Drop presentation-only schema titles without deleting a field named title."""
    if isinstance(value, dict):
        return {
            key: item if key in _SCHEMA_DATA_KEYS and parent_key not in _SCHEMA_MAP_KEYS
            else _strip_schema_titles(item, parent_key=key)
            for key, item in value.items()
            if key != "title" or parent_key in _SCHEMA_MAP_KEYS
        }
    if isinstance(value, list):
        return [_strip_schema_titles(item, parent_key=parent_key) for item in value]
    return value


def _collapse_nullable(value: Any, *, parent_key: str | None = None) -> Any:
    """Drop Pydantic's ``anyOf: [T, null]`` wrapper around every optional field.

    Optional fields can be omitted; advertising a null default costs context
    and conflicts with the compact non-null schema. Keep literal data intact.

    Only the advertised schema changes. Validation still runs against the real
    Pydantic model, which continues to accept an explicit null, so the server
    stays more permissive than it advertises rather than less.
    """
    if isinstance(value, dict):
        node = {
            key: item if key in _SCHEMA_DATA_KEYS and parent_key not in _SCHEMA_MAP_KEYS
            else _collapse_nullable(item, parent_key=key)
            for key, item in value.items()
        }
        branches = node.get("anyOf")
        if isinstance(branches, list):
            others = [item for item in branches if item != {"type": "null"}]
            if len(others) == len(branches) - 1 and others:
                if len(others) == 1 and isinstance(others[0], dict):
                    merged = dict(others[0])
                    del node["anyOf"]
                    # A field-level description or default is more specific than
                    # anything the surviving branch carries, so it wins.
                    merged.update(node)
                    if merged.get("default", ...) is None:
                        del merged["default"]
                    return merged
                node["anyOf"] = others
        if node.get("default", ...) is None and node.get("type") != "null":
            del node["default"]
        return node
    if isinstance(value, list):
        return [_collapse_nullable(item, parent_key=parent_key) for item in value]
    return value


def _compact_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Shorten local definition names without losing any schema constraints."""
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return schema
    preferred = {
        "CommandAttachmentInput": "Attachment",
        "SelfExecutionInput": "Self",
        "NewExecutionInput": "New",
        "ExistingExecutionInput": "Existing",
    }
    names = {name: preferred.get(name, name) for name in definitions}
    if len(set(names.values())) != len(names):
        return schema
    references = {f"#/$defs/{old}": f"#/$defs/{new}" for old, new in names.items()}

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    references.get(child, child)
                    if key == "$ref" and isinstance(child, str)
                    else {
                        name: references.get(ref, ref) if isinstance(ref, str) else walk(ref)
                        for name, ref in child.items()
                    }
                    if key == "mapping" and isinstance(child, dict)
                    else child
                    if key in {"default", "const", "enum", "examples"}
                    else walk(child)
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk({**schema, "$defs": {names[name]: item for name, item in definitions.items()}})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    profiles: frozenset[Profile]
    effect: Effect = Effect.READ
    budget: int = DETAIL_RESULT_BUDGET
    annotations: ToolAnnotations | None = None
    usage_note: str = ""
    validation_hint: str = ""
    keywords: tuple[str, ...] = ()
    dispatch_visible: bool = True

    def __post_init__(self) -> None:
        if not self.name or not re.fullmatch(r"[A-Za-z0-9_.-]+", self.name):
            raise ValueError(f"Invalid MCP tool name: {self.name!r}")
        if self.budget <= 0:
            raise ValueError(f"Tool {self.name} has an invalid result budget")
        if not self.profiles:
            # Adapter tools are ephemeral and deliberately not profile-owned.
            return
        if any(not isinstance(profile, Profile) for profile in self.profiles):
            raise TypeError(f"Tool {self.name} has a non-Profile profile value")

    @cached_property
    def _input_schema(self) -> dict[str, Any]:
        return _compact_schema_refs(
            _collapse_nullable(_strip_schema_titles(self.input_model.model_json_schema()))
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        # Dispatch schema discovery is frequent. Generate once, but never let a
        # caller mutate the cached schema or another client's advertised tool.
        return copy.deepcopy(self._input_schema)

    @property
    def tool_annotations(self) -> ToolAnnotations:
        return self.annotations or annotations_for(self.effect)

    def wire_tool(self) -> Tool:
        # outputSchema is intentionally omitted. Each call returns one compact JSON
        # TextContent block and the registry enforces its byte budget.
        description = self.description
        if self.usage_note:
            description = f"{description} {self.usage_note}"
        return Tool(
            name=self.name,
            description=description,
            inputSchema=self.input_schema,
            annotations=self.tool_annotations,
        )


class MutationPreparationBackend(Protocol):
    async def prepare(
        self,
        invocation: InvocationContext,
        spec: ToolSpec,
        arguments: BaseModel,
    ) -> Any: ...


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: OrderedDict[str, ToolSpec] = OrderedDict()
        self.cursors = CursorStore()
        self.preparation_backend: MutationPreparationBackend | None = None

    def add(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._specs:
            raise ValueError(f"Duplicate tool name in registry: {spec.name}")
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def select(
        self,
        profiles: Collection[Profile],
        *,
        read_only: bool,
    ) -> tuple[ToolSpec, ...]:
        selected = [
            spec
            for spec in self._specs.values()
            if spec.profiles.intersection(profiles)
            and (not read_only or spec.effect is Effect.READ)
        ]
        return tuple(selected)

    @staticmethod
    def wire_tools(specs: Sequence[ToolSpec]) -> list[Tool]:
        return [spec.wire_tool() for spec in specs]

    @staticmethod
    def _validation_hint(exc: ValidationError, schema: Mapping[str, Any]) -> str:
        safe_messages = {
            "missing": "field is required",
            "extra_forbidden": "field is not allowed",
            "string_too_short": "string is too short",
            "string_too_long": "string is too long",
            "greater_than_equal": "number is below the minimum",
            "less_than_equal": "number is above the maximum",
            "literal_error": "value is not one of the allowed literals",
            "enum": "value is not in the allowed enum",
            "int_type": "use an integer; booleans and quoted IDs are not allowed",
            "string_type": "use a string",
            "bool_type": "use a boolean",
            "list_type": "use an array",
            "dict_type": "use an object",
            "model_attributes_type": "use an object",
        }
        numeric_bounds = {
            "greater_than_equal": ("ge", "at least"),
            "greater_than": ("gt", "greater than"),
            "less_than_equal": ("le", "at most"),
            "less_than": ("lt", "less than"),
        }

        def numeric(value: Any) -> bool:
            return (type(value) is int and value.bit_length() <= 128
                    or type(value) is float and math.isfinite(value))

        def resolve(field):
            ref = field.get("$ref", "")
            if ref.startswith("#/$defs/"):
                return schema.get("$defs", {}).get(ref.removeprefix("#/$defs/"), {})
            return field

        def locate(path):
            field, names = schema, []
            for part in path:
                field = resolve(field)
                branches = field.get("discriminator", {}).get("mapping", {})
                if part in branches:
                    field = resolve({"$ref": branches[part]})
                    continue
                names.append(str(part))
                field = (field.get("items", {}) if isinstance(part, int)
                         else field.get("properties", {}).get(part, {}))
            return resolve(field), ".".join(names) or "arguments"

        details: list[str] = []
        for item in exc.errors(include_input=False, include_url=False)[:3]:
            path = item.get("loc") or ()
            field, location = locate(path)
            kind = str(item.get("type") or "invalid")
            detail = safe_messages.get(kind, "invalid value")
            discriminator = field.get("discriminator", {})
            tag = discriminator.get("propertyName")
            branches = discriminator.get("mapping", {})
            if tag and kind in {"union_tag_not_found", "union_tag_invalid", "model_attributes_type"}:
                # Derive corrections from the advertised schema, never from
                # rejected inputs or Pydantic's context (which can echo secrets).
                detail = f"use an object with required key {tag}: " + ", ".join(branches)
                for name, ref in branches.items():
                    required = [key for key in resolve({"$ref": ref}).get("required", []) if key != tag]
                    if required:
                        detail += f"; {name} requires " + ", ".join(required)
            if kind in numeric_bounds and not should_redact_key(location, None):
                key, relation = numeric_bounds[kind]
                bound = (item.get("ctx") or {}).get(key)
                if numeric(bound):
                    detail = f"must be {relation} {bound}"
                    # Only advertised, top-level fields can supply an omit/default
                    # correction. Never echo rejected input or evaluate a factory.
                    top_field = field if len(path) == 1 else {}
                    low, high = top_field.get("minimum"), top_field.get("maximum")
                    if (numeric(low) and numeric(high) and kind in {"greater_than_equal", "less_than_equal"}
                            and not {"exclusiveMinimum", "exclusiveMaximum"}.intersection(field)):
                        detail = f"must be between {low} and {high}"
                    default = top_field.get("default")
                    if numeric(default) and path[0] not in schema.get("required", []):
                        detail += f"; omit to use {default}"
            details.append(f"{location}: {detail}")
        return "; ".join(details)

    @staticmethod
    def validate(spec: ToolSpec, arguments: Mapping[str, Any] | None) -> BaseModel:
        try:
            return spec.input_model.model_validate(dict(arguments or {}))
        except ValidationError as exc:
            hint = ToolRegistry._validation_hint(exc, spec._input_schema)
            if spec.validation_hint:
                hint = f"{hint}; {spec.validation_hint}" if hint else spec.validation_hint
            raise ToolFailure(
                "INVALID_ARGUMENTS",
                "Invalid arguments for this tool.",
                hint=hint,
                next_call=validation_schema_call(spec.name),
            ) from exc

    async def invoke_spec(
        self,
        spec: ToolSpec,
        arguments: Mapping[str, Any] | None,
        invocation: InvocationContext,
    ) -> CallToolResult:
        configured = getattr(getattr(invocation.server, "cfg", None), "tool_prefix", "")
        prefix = configured if isinstance(configured, str) else ""
        if (spec.effect is Effect.READ and isinstance(arguments, Mapping)
                and set(arguments) == {"name", "arguments"}
                and not {"name", "arguments"} <= set(spec.input_model.model_fields)
                and arguments.get("name") in (spec.name, prefix + spec.name)
                and isinstance(arguments.get("arguments"), dict)):
            # A copied continuation is a transport envelope, not tool inputs.
            # Unwrap one exact read call; writes and legitimate routing models
            # retain their own strict argument validation.
            arguments = arguments["arguments"]
        previous_secrets = authentication_secrets(invocation.server)
        with exposed_tool_prefix(prefix), cursor_scope(self.cursors):
            result = await self._invoke_within_prefix(spec, arguments, invocation)
        if not result.isError:
            return result
        secrets = sorted(set((*previous_secrets, *authentication_secrets(invocation.server))), key=len, reverse=True)

        def scrub(value):
            if isinstance(value, str):
                for secret in secrets:
                    value = value.replace(secret, "[REDACTED]")
                return value
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, dict):
                return {scrub(key): scrub(item) for key, item in value.items()}
            return value

        def scrub_arguments(value, model):
            if not isinstance(value, dict):
                return scrub(value)
            fields = model.model_fields
            trusted_keys = {*fields, *(field.alias for field in fields.values() if field.alias),
                            *getattr(model, "input_aliases", {})}
            return {key if key in trusted_keys else scrub(key): scrub(item) for key, item in value.items()}

        def scrub_continuation(value):
            if not isinstance(value, dict):
                return scrub(value)
            output = {key if key in {"name", "arguments"} else scrub(key): scrub(item)
                      for key, item in value.items()}
            name = value.get("name")
            if not isinstance(name, str):
                return output
            stripped = name.removeprefix(prefix) if prefix else name
            target = self.get(stripped) or self.get(name)
            routers = {"tuoni_call": CallToolInput, "tuoni_tool_schema": ToolSchemaInput,
                       "tuoni_find_tool": FindToolInput}
            model = target.input_model if target is not None else (
                routers.get(stripped) if getattr(invocation.server.cfg, "mode", None) is MCPMode.DISPATCH else None
            )
            if model is None:
                return output
            # These names and keys come from our catalog, not upstream error
            # text. A password such as "tuoni" must not corrupt callable routes.
            output["name"] = name
            arguments = value.get("arguments")
            output["arguments"] = scrub_arguments(arguments, model)
            if model in {CallToolInput, ToolSchemaInput} and isinstance(arguments, dict):
                inner_name = arguments.get("name")
                inner = self.get(inner_name) if isinstance(inner_name, str) else None
                if inner is not None or (
                    model is ToolSchemaInput and isinstance(inner_name, str) and inner_name in routers
                ):
                    output["arguments"]["name"] = arguments["name"]
                    if inner is not None and model is CallToolInput and "arguments" in arguments:
                        output["arguments"]["arguments"] = scrub_arguments(arguments["arguments"], inner.input_model)
            return output

        def scrub_error(value):
            if not isinstance(value, dict):
                return scrub(value)
            keys = {"code", "message", "retryable", "hint", "next"}
            output = {key if key in keys else scrub(key): scrub(item) for key, item in value.items()}
            if "next" in value:
                output["next"] = scrub_continuation(value["next"])
            return output

        result = result.model_copy(deep=True)
        for content in result.content:
            if content.type == "text":
                try:
                    payload = json.loads(content.text)
                except ValueError:
                    content.text = scrub(content.text)
                else:
                    content.text = json.dumps(scrub_error(payload), ensure_ascii=False, separators=(",", ":"))
        if result.structuredContent is not None:
            result.structuredContent = scrub_error(result.structuredContent)
        return enforce_result_budget(result, budget=ERROR_RESULT_BUDGET)

    async def _invoke_within_prefix(
        self,
        spec: ToolSpec,
        arguments: Mapping[str, Any] | None,
        invocation: InvocationContext,
    ) -> CallToolResult:
        validated = None
        journal = MutationJournal()
        mutation_started = False
        try:
            validated = self.validate(spec, arguments)
            if spec.effect is Effect.MUTATE:
                # Authenticate before entering the once-only boundary. Any auth or
                # transport failure after this point is surfaced without replaying
                # a potentially accepted POST/PUT/PATCH/DELETE.
                await ensure_fresh_token(invocation.server)
                with mutation_once_only(), mutation_scope(journal):
                    mutation_started = True
                    value = await spec.handler(invocation, validated)
                if isinstance(value, Mapping):
                    value = {**value, "outcome": journal.snapshot(succeeded=True)}
            else:
                value = await spec.handler(invocation, validated)
            if isinstance(value, CallToolResult):
                limit = ERROR_RESULT_BUDGET if value.isError else spec.budget
                return enforce_result_budget(value, budget=limit)
            if isinstance(value, ToolSuccess):
                return success_result(
                    value.data,
                    next_cursor=value.next_cursor,
                    next_call=value.next_call,
                    budget=spec.budget,
                    redact_output=value.redact_output,
                )
            if isinstance(value, Mapping) and value.get("error"):
                return error_result(
                    "TUONI_ERROR",
                    str(value.get("error")),
                    retryable=False,
                    hint="Correct the request before retrying.",
                )
            return success_result(value, budget=spec.budget)
        except ToolFailure as exc:
            outcome = exc.outcome
            if spec.effect is Effect.MUTATE and (journal.accepted or journal.rejected or journal.unknown):
                outcome = journal.snapshot(started=mutation_started)
                if outcome["state"] in {"partial", "unknown"}:
                    exc = ToolFailure(
                        "PARTIAL_OUTCOME" if outcome["state"] == "partial" else "AMBIGUOUS_OUTCOME",
                        "The operation did not return a complete receipt.",
                        hint="Inspect current server state; do not replay this operation.", outcome=outcome,
                    )
            next_call = exc.next_call
            if spec.effect is Effect.MUTATE and outcome is not None and outcome["state"] != "not_started":
                next_call = mutation_inspection(self, invocation, spec.name)
            if (next_call is None and validated is not None and spec.effect is Effect.READ
                    and exc.code in {"INVALID_CURSOR", "CURSOR_EXPIRED", "CURSOR_SCOPE_CHANGED", "CURSOR_STALE",
                                     "SETTING_CHANGED", "DETAIL_CHANGED"}):
                # A restart is an exact read, preserving the requested filters
                # and limits but discarding the invalid position. Dispatch and
                # client prefixes are rendered by the shared envelope builder.
                next_call = {"name": spec.name, "arguments": validated.model_dump(
                    mode="json", exclude_none=True, exclude={"cursor", "offset_chars"},
                )}
                if spec.name == "tuoni_events_poll":
                    next_call["arguments"]["start"] = "earliest"
            return error_result(
                exc.code,
                exc.message,
                retryable=exc.retryable,
                hint=exc.hint,
                next_call=next_call,
                outcome=outcome,
            )
        except Exception as exc:  # tool boundary: always return an MCP tool error
            # Reads and preparations never change remote state, so a transient
            # failure is safe to repeat. Anything that submits an operation is
            # ambiguous once it has started and must never be advertised as
            # retryable, whatever the underlying transport reported.
            classified = classify(
                exc,
                tool_name=spec.name,
                safe_to_retry=spec.effect in (Effect.READ, Effect.PREPARE),
            )
            observed = getattr(getattr(invocation.server, "c2", None), "observes_mutations", False) is True
            outcome = journal.snapshot() if spec.effect is Effect.MUTATE and observed else None
            if outcome is not None and outcome["state"] == "rejected":
                classified = classify_rejection(exc, tool_name=spec.name, outcome=outcome)
            code, message, hint = classified.code, classified.message, classified.hint
            if outcome is not None and outcome["state"] in {"partial", "unknown"}:
                code = "PARTIAL_OUTCOME" if outcome["state"] == "partial" else "AMBIGUOUS_OUTCOME"
                message = "Earlier writes were accepted; the operation did not finish." if code == "PARTIAL_OUTCOME" else "The operation may have reached Tuoni."
                hint = "Inspect current server state before further changes; do not replay this operation."
            return error_result(
                code,
                message,
                retryable=classified.retryable,
                hint=hint,
                outcome=outcome,
                next_call=mutation_inspection(self, invocation, spec.name) if outcome is not None
                    and outcome["state"] != "not_started" else None,
            )


def _fit_detail(value: Any, tool_name: str) -> Any:
    """Fit optional detail records into the response budget.

    Apply progressively tighter structural bounds to oversized configurations
    and schemas while preserving a useful summary for the caller.
    """
    if not isinstance(value, (Mapping, list)):
        return value
    if would_fit_success(value, budget=DETAIL_RESULT_BUDGET):
        return value
    for max_depth, max_items, max_string in ((6, 48, 512), (5, 24, 256), (4, 12, 128), (3, 6, 64)):
        bounded = bounded_redact(
            value,
            max_depth=max_depth,
            max_items=max_items,
            max_string_bytes=max_string,
        )
        if would_fit_success(bounded, budget=DETAIL_RESULT_BUDGET):
            return bounded
    raise ToolFailure(
        "RESPONSE_TOO_LARGE",
        f"{tool_name} returned a record too large to bound.",
        hint="Use a narrower list or search tool to identify a smaller record.",
    )


def mutation_inspection(registry: ToolRegistry, invocation: InvocationContext, tool_name: str) -> dict[str, Any] | None:
    """Return an enabled bounded inventory read after an incomplete mutation."""
    readers = {
        "job": "jobs_list", "alias": "aliases_list", "file": "files_list",
        "host": "hosts_list", "service": "services_list", "credential": "credentials_list",
        "listener": "listeners_list", "payload": "payloads_list", "user": "users_list",
        "setting": "settings_get_all", "settings": "settings_get_all", "agent": "agents_list",
    }
    family = tool_name.removeprefix("tuoni_").split("_", 1)[0]
    name = "tuoni_" + readers.get(family, "capabilities")
    enabled = {spec.name for spec in registry.select(invocation.server.cfg.profiles, read_only=True)}
    if name not in enabled:
        return None
    arguments = {"include_inactive": True} if family == "job" else {}
    return {"name": name, "arguments": arguments}

class _DecoratedNameCollector:
    def __init__(self) -> None:
        self.names: set[str] = set()

    def tool(self, name: str | None = None, **_: Any):
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.names.add(name or fn.__name__)
            return fn

        return decorator


def _decorated_input_model(
    fn: Callable[..., Any],
    tool_name: str,
    *,
    paginated: bool = False,
) -> tuple[type[BaseModel], str | None]:
    signature = inspect.signature(fn)
    hints = get_type_hints(fn, include_extras=True)
    fields: dict[str, tuple[Any, Any]] = {}
    context_name: str | None = None
    for parameter in signature.parameters.values():
        annotation = hints.get(parameter.name, Any)
        if parameter.name in {"ctx", "context"}:
            context_name = parameter.name
            continue
        default = parameter.default
        if default is inspect.Parameter.empty:
            default = ...
        fields[parameter.name] = (annotation, default)
    if paginated:
        if "cursor" in fields or "limit" in fields:
            raise ValueError(f"Legacy list tool {tool_name} already defines cursor or limit")
        fields["cursor"] = (str | None, Field(default=None, max_length=2048))
        fields["limit"] = (int, Field(default=20, ge=1, le=50))
    model_name = "".join(part.title() for part in re.split(r"[^A-Za-z0-9]+", tool_name)) + "Input"
    model = create_model(model_name, __base__=ToolInput, **fields)
    return model, context_name


class _DecoratedRegistryCollector:
    def __init__(
        self,
        registry: ToolRegistry,
        profiles: frozenset[Profile],
        read_names: Collection[str],
        force_mutating: Collection[str],
    ) -> None:
        self.registry = registry
        self.profiles = profiles
        self.read_names = set(read_names)
        self.force_mutating = set(force_mutating)

    def tool(
        self,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        preserve_list_fields: tuple[str, ...] = (),
        preserve_response_fields: tuple[str, ...] = (),
        **_: Any,
    ):
        del title

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or fn.__name__
            effect = (
                Effect.READ
                if tool_name in self.read_names and tool_name not in self.force_mutating
                else Effect.MUTATE
            )
            list_paginated = effect is Effect.READ and tool_name.endswith("_list")
            mapping_paginated = (
                effect is Effect.READ and tool_name == "tuoni_settings_get_all"
            )
            paginated = list_paginated or mapping_paginated
            input_model, context_name = _decorated_input_model(
                fn,
                tool_name,
                paginated=paginated,
            )

            async def handler(invocation: InvocationContext, arguments: BaseModel) -> Any:
                kwargs = arguments.model_dump(mode="python")
                cursor = kwargs.pop("cursor", None) if paginated else None
                limit = int(kwargs.pop("limit", 20)) if paginated else 20
                cursor_tool = f"profile-list:{tool_name}"
                continuation_arguments = arguments.model_dump(
                    mode="python",
                    exclude={"cursor"},
                )
                upstream_more = False

                def continuation_call(cursor_value: str | None) -> dict[str, Any] | None:
                    if cursor_value is None and not upstream_more:
                        return None
                    following_arguments = {**continuation_arguments}
                    if cursor_value is not None:
                        following_arguments["cursor"] = cursor_value
                    else:
                        following_arguments["page"] = kwargs["page"] + 1
                    target_call = {
                        "name": tool_name,
                        "arguments": following_arguments,
                    }
                    if invocation.server.cfg.mode is MCPMode.DISPATCH:
                        return {
                            "name": "tuoni_call",
                            "arguments": {
                                "name": tool_name,
                                "arguments": target_call["arguments"],
                            },
                        }
                    return target_call

                scope = hashlib.sha256(
                    json.dumps(
                        kwargs,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()
                start = 0
                if cursor is not None:
                    try:
                        decoded = decode_page_cursor(cursor, tool=cursor_tool)
                        if type(decoded.get("limit")) is not int:
                            raise ValueError("list cursor limit must be an integer")
                        if decoded.get("scope") != scope or decoded["limit"] != limit:
                            raise CursorError("CURSOR_SCOPE_CHANGED", "List filters, page, or limit changed while continuing the cursor.")
                        start = decoded["offset"]
                        if type(start) is not int or start < 0:
                            raise ValueError("list cursor offset must be a non-negative integer")
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ToolFailure(
                            getattr(exc, "code", "INVALID_CURSOR"),
                            str(exc) if isinstance(exc, CursorError) else "The list cursor is invalid for these arguments.",
                            hint="Restart the list without a cursor.",
                        ) from exc
                if context_name is not None:
                    kwargs[context_name] = invocation.context
                result = fn(**kwargs)
                if inspect.isawaitable(result):
                    result = await result
                if not paginated or not isinstance(result, Mapping):
                    return _fit_detail(result, tool_name)

                if mapping_paginated:
                    list_field = "settings"
                    source_items = []
                    for section, section_value in result.items():
                        if isinstance(section_value, Mapping):
                            source_items.extend(
                                {
                                    "section": bounded_redact(section, max_string_bytes=96),
                                    "key": bounded_redact(key, max_string_bytes=192),
                                    "value": bounded_redact(
                                        value,
                                        max_depth=5,
                                        max_items=32,
                                        max_string_bytes=512,
                                    ),
                                }
                                for key, value in section_value.items()
                            )
                        else:
                            source_items.append(
                                {
                                    "section": bounded_redact(section, max_string_bytes=96),
                                    "value": bounded_redact(
                                        section_value,
                                        max_depth=5,
                                        max_items=32,
                                        max_string_bytes=512,
                                    ),
                                }
                            )
                    static: dict[str, Any] = {}
                else:
                    list_fields = [
                        key for key, value in result.items() if isinstance(value, list)
                    ]
                    if len(list_fields) != 1:
                        raise ToolFailure(
                            "INVALID_LIST_RESPONSE",
                            f"{tool_name} did not return one structurally pageable list.",
                        )
                    list_field = list_fields[0]
                    source_items = result[list_field]
                    static = {
                        str(key): bounded_redact(
                            value,
                            max_depth=4,
                            max_items=32,
                            max_string_bytes=256,
                        )
                        for key, value in result.items()
                        if key != list_field
                    }
                    for field in preserve_response_fields:
                        if field in static:
                            static[field] = result[field]
                if not 0 <= start <= len(source_items):
                    raise ToolFailure(
                        "INVALID_CURSOR",
                        "The list cursor is outside the current result set.",
                        hint="Restart the list without a cursor.",
                    )
                # These API list endpoints expose numbered upstream pages. Once
                # the local page is exhausted, continue to the next API page.
                upstream_more = (
                    "page" in kwargs and "page_size" in kwargs
                    and len(source_items) == kwargs["page_size"]
                )
                items = source_items

                def page_data(selected_items: list[Any]) -> dict[str, Any]:
                    numbered = "page" in kwargs and "page_size" in kwargs
                    page_start = kwargs["page"] * kwargs["page_size"] if numbered else 0
                    total = None if upstream_more else page_start + len(items)
                    return {
                        **static, list_field: selected_items,
                        "coverage": page_coverage(
                            offset=page_start + start, returned=len(selected_items), total=total,
                            has_more=start + len(selected_items) < len(items) or upstream_more,
                        ),
                    }

                selected: list[Any] = []
                for index in range(start, min(start + limit, len(items))):
                    bounded_item = bounded_redact(
                        items[index], max_depth=6, max_items=48, max_string_bytes=512
                    )
                    # Trusted summary providers may declare validated identity
                    # fields/call descriptors that must remain executable. The
                    # whole final envelope is still measured below; oversized
                    # records fail explicitly rather than returning altered IDs.
                    if isinstance(bounded_item, dict) and isinstance(items[index], Mapping):
                        for field in preserve_list_fields:
                            if field in items[index]:
                                bounded_item[field] = items[index][field]
                    candidate = [*selected, bounded_item]
                    next_index = index + 1
                    candidate_cursor = (
                        encode_page_cursor(
                            {
                                "tool": cursor_tool,
                                "offset": next_index,
                                "scope": scope,
                                "limit": limit,
                            }
                        )
                        if next_index < len(items)
                        else None
                    )
                    candidate_call = continuation_call(candidate_cursor)
                    if not would_fit_success(
                        page_data(candidate),
                        next_cursor=candidate_cursor,
                        next_call=candidate_call,
                        budget=LIST_RESULT_BUDGET,
                    ):
                        break
                    selected = candidate

                next_index = start + len(selected)
                next_cursor = (
                    encode_page_cursor(
                        {
                            "tool": cursor_tool,
                            "offset": next_index,
                            "scope": scope,
                            "limit": limit,
                        }
                    )
                    if next_index < len(items)
                    else None
                )
                next_call = continuation_call(next_cursor)
                if not selected and start < len(items):
                    raise ToolFailure(
                        "RESPONSE_TOO_LARGE",
                        "One bounded list item exceeds the 4 KiB response budget.",
                        hint="Use an exact get tool for that item.",
                    )
                return ToolSuccess(
                    page_data(selected),
                    next_cursor=next_cursor,
                    next_call=next_call,
                )

            self.registry.add(
                ToolSpec(
                    name=tool_name,
                    description=description or inspect.getdoc(fn) or tool_name,
                    input_model=input_model,
                    handler=handler,
                    profiles=self.profiles,
                    effect=effect,
                    budget=LIST_RESULT_BUDGET if paginated else DETAIL_RESULT_BUDGET,
                    annotations=annotations,
                    usage_note=(
                        "Follow next until null. coverage describes this response only; "
                        "complete means the whole inventory is in this response, and total:null means unknown."
                        if paginated
                        else ""
                    ),
                )
            )
            return fn

        return decorator


def collect_decorated_module(
    registry: ToolRegistry,
    module: ModuleType,
    cfg: Settings,
    *,
    profiles: Collection[Profile],
    force_mutating: Collection[str] = (),
) -> None:
    """Capture a module's public decorators as ToolSpecs.

    The module functions remain handlers, but no FastMCP Tool object or private
    manager is created or retained. Read-only registration is probed only to classify
    effects; the startup-static registry performs the actual filtering.
    """
    read_probe = _DecoratedNameCollector()
    module.register(read_probe, replace(cfg, read_only=True))
    collector = _DecoratedRegistryCollector(
        registry,
        frozenset(profiles),
        read_probe.names,
        force_mutating,
    )
    module.register(collector, replace(cfg, read_only=False))


class FindToolInput(ToolInput):
    query: str = Field(min_length=1, max_length=200)
    cursor: str | None = None
    limit: int = Field(default=8, ge=1, le=100)


class ToolSchemaInput(AliasedToolInput):
    input_aliases = {"tool_name": "name"}
    name: str = Field(min_length=1, max_length=200)


class CallToolInput(ToolInput):
    name: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ExecuteOperationInput(ToolInput):
    preparation_id: str = Field(min_length=1, max_length=200)


class _DispatchCursors:
    """Short, query-bound handles for one startup-fixed discovery catalog."""

    placeholder = "p_" + "x" * 12

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.records: OrderedDict[str, tuple[str, int, float]] = OrderedDict()

    def encode(self, offset: int, query: str) -> str:
        now = self.clock()
        for token in [key for key, (_, _, expiry) in self.records.items() if expiry <= now]:
            del self.records[token]
        while len(self.records) >= 128:
            self.records.popitem(last=False)
        token = "p_" + secrets.token_urlsafe(9)
        while token in self.records:
            token = "p_" + secrets.token_urlsafe(9)
        self.records[token] = (query, offset, now + 300)
        return token

    def decode(self, cursor: str | None, query: str) -> int:
        if cursor is None:
            return 0
        record = self.records.get(cursor) if len(cursor) <= 64 else None
        if record is not None:
            if record[2] <= self.clock():
                raise ToolFailure("CURSOR_EXPIRED", "The discovery cursor expired.",
                                  hint="Restart without a cursor. Follow the returned next call unchanged.")
            if record[0] != query:
                raise ToolFailure("CURSOR_SCOPE_CHANGED", "The search query changed; this cursor belongs to the earlier query.",
                                  hint="For a new query omit cursor. To continue the earlier query copy its exact next call.")
            return record[1]
        raise ToolFailure(
            "INVALID_CURSOR", "The discovery cursor is invalid or no longer retained by this server.",
            hint="Restart the search without a cursor. Copy the next arguments unchanged.",
        )


_DISCOVERY_WORD = re.compile(r"[a-z0-9]+")
_DISCOVERY_FILLER = frozenset({"a", "an", "the", "please", "show", "me", "can", "you", "by", "on", "for", "to"})
_DISCOVERY_PLURALS = {
    "agents": "agent", "commands": "command", "files": "file", "hosts": "host",
    "jobs": "job", "listeners": "listener", "outputs": "output", "results": "result",
    "sessions": "session", "templates": "template", "tools": "tool", "users": "user",
    "hostnames": "hostname", "workstations": "workstation",
    "execution": "execute", "executing": "execute", "executions": "execute",
    "preparation": "prepare", "preparing": "prepare",
    "validation": "validate", "validating": "validate",
}


def _discovery_words(text: str) -> frozenset[str]:
    return frozenset(
        _DISCOVERY_PLURALS.get(word, word)
        for word in _DISCOVERY_WORD.findall(text.casefold())
        if word not in _DISCOVERY_FILLER
    )


def build_dispatch_specs(
    registry: ToolRegistry,
    target_specs: Sequence[ToolSpec],
    *,
    read_only: bool,
) -> tuple[ToolSpec, ...]:
    targets = {
        spec.name: spec
        for spec in target_specs
        if spec.dispatch_visible and spec.effect is not Effect.EXECUTE
    }
    # Execution is the one separately visible destructive dispatch capability.
    # Its handler/store is shared even when selected profiles do not include the
    # agent-ops profile that owns the native exposure.
    execute_target = registry.get("tuoni_operation_execute")
    # Schema lookup also describes the visible entrypoints. Invocation remains
    # restricted to targets: routers and the executor never recurse through call.
    schema_targets = dict(targets)
    cursors = _DispatchCursors()

    def canonical_name(name: str, candidates: Mapping[str, ToolSpec]) -> str:
        if name in candidates:
            return name
        stripped = native_name(name)
        return stripped if stripped in candidates else name

    def unknown_tool(name: str) -> ToolFailure:
        name = canonical_name(name, schema_targets)
        next_call = (
            {"name": "tuoni_tool_schema", "arguments": {"name": name}}
            if name in schema_targets
            else {"name": "tuoni_find_tool", "arguments": {"query": name[:80]}}
        )
        return ToolFailure(
            "TOOL_NOT_FOUND",
            "No callable operation has that exact name.",
            hint="Follow next to discover an exact name or inspect the visible entrypoint schema.",
            next_call=next_call,
        )
    # The selected catalog is fixed at startup. Index it once, not on every
    # discovery call; profile/read-only exclusions cannot enter the index.
    search_index = {
        name: (
            " ".join((spec.name, spec.description, spec.usage_note, *spec.keywords)).casefold(),
            _discovery_words(spec.name),
            _discovery_words(" ".join(spec.keywords)),
            _discovery_words(" ".join((spec.name, spec.description, spec.usage_note, *spec.keywords))),
            frozenset(_DISCOVERY_WORD.findall(name.casefold())),
        )
        for name, spec in targets.items()
    }

    async def find_tool(_: InvocationContext, arguments: BaseModel) -> ToolSuccess:
        assert isinstance(arguments, FindToolInput)
        needle = arguments.query.casefold().strip()
        words = _discovery_words(needle)
        literal_words = frozenset(_DISCOVERY_WORD.findall(needle)) - _DISCOVERY_FILLER

        def score(spec: ToolSpec) -> tuple[int, int, int, int, str]:
            name = spec.name.casefold()
            haystack, name_words, keywords, all_words, literal_name_words = search_index[spec.name]
            if name == needle:
                rank = 0
            elif needle and name.startswith(needle):
                rank = 1
            elif needle and needle in name:
                rank = 2
            elif needle and needle in haystack:
                rank = 3
            elif words and words <= all_words:
                # A phrase such as "list agents" need not occur verbatim in
                # "List active agents". Require every meaningful word rather
                # than guessing from one matching term in an unrelated request.
                return (
                    4, -len(literal_words & literal_name_words),
                    -len(words & name_words), -len(words & keywords), spec.name,
                )
            else:
                rank = 99
            return rank, 0, 0, 0, spec.name

        ranked = [(score(spec), spec) for spec in targets.values()]
        matches = [spec for rank, spec in sorted(ranked, key=lambda entry: entry[0]) if rank[0] < 99]
        # Names such as jobs/download exist in both domains. Preserve an
        # enabled operation's whole-word match; incidental substrings (ls in
        # credentials) must not hide an exact agent command query.
        operation_match = any(words and words <= search_index[spec.name][1] for spec in matches)
        command_query = (is_template_query(needle) and "tuoni_commands_search" in targets
                         and not operation_match)
        if command_query:
            matches = [targets["tuoni_commands_search"]]
        offset = cursors.decode(arguments.cursor, arguments.query)
        if offset > len(matches):
            raise ToolFailure(
                "INVALID_CURSOR",
                "The dispatch cursor is past the end of this result set.",
                hint="Restart the search without a cursor.",
            )

        items: list[dict[str, Any]] = []
        index = offset
        while index < len(matches) and len(items) < arguments.limit:
            spec = matches[index]
            candidate = {
                "name": spec.name,
                "summary": spec.description[:240],
                "effect": spec.effect.value,
                "schema": exposed_call({"name": "tuoni_tool_schema", "arguments": {"name": spec.name}}),
            }
            proposed = [*items, candidate]
            candidate_cursor = cursors.placeholder
            candidate_next = {
                "name": "tuoni_find_tool",
                "arguments": {
                    "query": arguments.query,
                    "cursor": candidate_cursor,
                    "limit": arguments.limit,
                },
            }
            schema = spec.input_schema
            if len(json.dumps(schema, ensure_ascii=False).encode()) <= 768:
                candidate["input_schema"] = schema
            else:
                candidate["required"] = [name for name, field in spec.input_model.model_fields.items() if field.is_required()]
            if not would_fit_success(
                {"tools": proposed},
                next_cursor=candidate_cursor,
                next_call=candidate_next,
                budget=2 * 1024,
                redact_output=False,
            ):
                candidate.pop("input_schema", None)
                candidate.pop("required", None)
                if items and not would_fit_success(
                    {"tools": proposed}, next_cursor=candidate_cursor,
                    next_call=candidate_next, budget=2 * 1024, redact_output=False,
                ):
                    break
                if not items:
                    candidate["summary"] = spec.description[:120]
            items = proposed
            index += 1

        has_more = index < len(matches)
        next_cursor = cursors.encode(index, arguments.query) if has_more else None
        next_call = (
            {
                "name": "tuoni_find_tool",
                "arguments": {
                    "query": arguments.query,
                    "cursor": next_cursor,
                    "limit": arguments.limit,
                },
            }
            if has_more
            else None
        )
        data: dict[str, Any] = {"tools": items}
        if command_query:
            data["note"] = "This is an agent command query. Follow next; select an agent if requested, then describe a returned template."
            next_call = {"name": "tuoni_call", "arguments": {
                "name": "tuoni_commands_search", "arguments": {"query": arguments.query},
            }}
        elif not matches and needle != "tuoni" and not words.intersection({"not", "never", "no"}):
            data["note"] = "No purpose matched. Follow next to browse enabled MCP tools instead of guessing names."
            next_call = {"name": "tuoni_find_tool", "arguments": {"query": "tuoni"}}
        return ToolSuccess(
            data,
            next_cursor=next_cursor,
            next_call=bounded_redact(next_call),
            redact_output=False,
        )

    async def tool_schema(_: InvocationContext, arguments: BaseModel) -> Any:
        assert isinstance(arguments, ToolSchemaInput)
        spec = schema_targets.get(canonical_name(arguments.name, schema_targets))
        if spec is None:
            raise unknown_tool(arguments.name)
        return ToolSuccess(
            {
                "name": spec.name,
                "description": spec.description,
                "effect": spec.effect.value,
                "input_schema": spec.input_schema,
                "usage_note": spec.usage_note,
            },
            redact_output=False,
        )

    def wrap_dispatch_continuation(
        result: CallToolResult,
        *,
        target_budget: int,
    ) -> CallToolResult:
        """Rewrite a hidden native continuation into a callable dispatcher call."""
        if result.isError or len(result.content) != 1:
            return result

        text = getattr(result.content[0], "text", None)
        if not isinstance(text, str):
            return result
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return result
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            return result
        continuation = payload.get("next")
        if not isinstance(continuation, Mapping):
            return result
        continuation_name = continuation.get("name")
        continuation_arguments = continuation.get("arguments")
        if not isinstance(continuation_name, str):
            return result
        # The target built its continuation in the client's vocabulary already;
        # dispatch routes by the server-side name, so undo that here. Fall back
        # to the name as given, because a native name can itself begin with the
        # configured prefix.
        stripped = native_name(continuation_name)
        continuation_name = stripped if stripped in targets else continuation_name
        if continuation_name not in targets or not isinstance(continuation_arguments, Mapping):
            return result

        wrapped_next = {
            "name": "tuoni_call",
            "arguments": {
                "name": continuation_name,
                "arguments": dict(continuation_arguments),
            },
        }
        # Re-serialize and re-measure after adding the dispatch envelope. The
        # target's own budget still applies rather than the adapter allowance.
        return success_result(
            payload.get("data"),
            next_cursor=payload.get("next_cursor"),
            next_call=wrapped_next,
            budget=target_budget,
            redact_output=False,
        )

    async def call_tool(invocation: InvocationContext, arguments: BaseModel) -> Any:
        assert isinstance(arguments, CallToolInput)
        spec = targets.get(arguments.name)
        actual = arguments.arguments
        name = canonical_name(arguments.name, schema_targets)
        if spec is None:
            # Clients sometimes copy a whole returned call into the dispatcher.
            # Flatten one read-only envelope; never invoke a router recursively
            # or normalize preparations, mutations, execution, or unknown names.
            read_name = name
            if name == "tuoni_call" and set(actual) <= {"name", "arguments"}:
                inner_name = actual.get("name")
                inner_args = actual.get("arguments", {})
                if isinstance(inner_name, str) and isinstance(inner_args, dict):
                    read_name = canonical_name(inner_name, targets)
                    candidate = targets.get(read_name)
                    if candidate is not None and candidate.effect is Effect.READ:
                        actual = inner_args
            candidate = targets.get(read_name)
            if candidate is not None and candidate.effect is Effect.READ:
                spec = candidate
        if spec is None:
            raise unknown_tool(arguments.name)
        if (spec.effect is Effect.READ and "name" not in spec.input_model.model_fields
                and actual.get("name") in (spec.name, canonical_name(arguments.name, targets))):
            actual = {key: value for key, value in actual.items() if key != "name"}
        with dispatch_continuations(targets):
            if spec.effect is Effect.MUTATE:
                validated = registry.validate(spec, actual)
                if registry.preparation_backend is None:
                    raise ToolFailure(
                        "PREPARATION_UNAVAILABLE",
                        "This mutation cannot run directly through dispatch mode.",
                        hint="Use native mode until a preparation backend is configured.",
                    )
                return await registry.preparation_backend.prepare(invocation, spec, validated)
            result = await registry.invoke_spec(spec, actual, invocation)
        return wrap_dispatch_continuation(
            result,
            target_budget=spec.budget,
        )

    adapters = [
        ToolSpec(
            name="tuoni_find_tool",
            description="Find MCP tools by purpose. Matches include input_schema or an exact schema call.",
            input_model=FindToolInput,
            handler=find_tool,
            profiles=frozenset(),
            effect=Effect.READ,
            budget=2 * 1024,
        ),
        ToolSpec(
            name="tuoni_tool_schema",
            description="Get an MCP tool's input schema. Agent command templates use tuoni_command_describe.",
            input_model=ToolSchemaInput,
            handler=tool_schema,
            profiles=frozenset(),
            effect=Effect.READ,
            budget=DETAIL_RESULT_BUDGET,
            validation_hint="Use name or tool_name; if both are provided, their values must agree.",
        ),
        ToolSpec(
            name="tuoni_call",
            description="Call a discovered MCP name with its shown input_schema; otherwise load schema first. Mutations are only prepared.",
            input_model=CallToolInput,
            handler=call_tool,
            profiles=frozenset(),
            effect=Effect.READ if read_only else Effect.PREPARE,
            budget=MAX_RESULT_BUDGET,
        ),
    ]

    if not read_only:
        if execute_target is not None:
            async def execute_tool(
                invocation: InvocationContext,
                arguments: BaseModel,
            ) -> CallToolResult:
                with dispatch_continuations(targets):
                    result = await registry.invoke_spec(
                        execute_target,
                        arguments.model_dump(mode="python"),
                        invocation,
                    )
                return wrap_dispatch_continuation(
                    result,
                    target_budget=execute_target.budget,
                )

            adapters.append(
                replace(
                    execute_target,
                    handler=execute_tool,
                    profiles=frozenset(),
                )
            )
        else:

            async def unavailable_execute(_: InvocationContext, __: BaseModel) -> Any:
                raise ToolFailure(
                    "PREPARATION_UNAVAILABLE",
                    "No operation executor is registered.",
                    hint="Enable a profile that supplies preparation execution.",
                )

            adapters.append(
                ToolSpec(
                    name="tuoni_operation_execute",
                    description="Execute one opaque preparation ID; receipts are replay-safe.",
                    input_model=ExecuteOperationInput,
                    handler=unavailable_execute,
                    profiles=frozenset(),
                    effect=Effect.EXECUTE,
                )
            )
    schema_targets.update((spec.name, spec) for spec in adapters)
    return tuple(adapters)
