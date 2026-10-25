from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import stat
import weakref
from collections.abc import Mapping
from contextlib import nullcontext
from difflib import SequenceMatcher
from itertools import islice
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from urllib.parse import unquote, urlsplit

import anyio
from jsonschema.validators import validator_for
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import BeforeValidator, Field, WithJsonSchema, model_validator
from referencing import Registry

from tuoni_mcp.command_intents import matched_templates
from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import call_without_retry, ensure_fresh_token, mutation_once_only, to_thread
from tuoni_mcp.errors import classify, classify_rejection
from tuoni_mcp.mutations import MutationJournal, mutation_scope
from tuoni_mcp.pagination import (
    bounded_text,
    decode_cursor,
    encode_cursor,
    fit_text_window,
    redact,
    success_payload,
)
from tuoni_mcp.paths import url_path_segment
from tuoni_mcp.preparations import PreparedAttachment, PreparationStore
from tuoni_mcp.protocol import (
    DETAIL_RESULT_BUDGET,
    exposed_name,
    exposed_call,
    LIST_RESULT_BUDGET,
    MAX_RESULT_BUDGET,
    redact as redact_result,
    would_fit_success,
)
from tuoni_mcp.registry import (
    AliasedToolInput,
    Effect,
    InvocationContext,
    MutationPreparationBackend,
    mutation_inspection,
    ToolFailure,
    ToolInput,
    ToolRegistry,
    ToolSpec,
    ToolSuccess,
)
from tuoni_mcp.result_cache import cache_for as result_cache_for, result_text
from tuoni_mcp.result_wait import InitialReadTimeout, callback_poll_seconds, read_until_terminal
from tuoni_mcp.schema_examples import ExampleSkeleton, build_example
from tuoni_mcp.staging import prepare_upload, prepared_upload
from tuoni_mcp.template_cache import cache_for, template_fingerprint, template_identifiers
from tuoni_mcp.tools._helpers import call
from tuoni_mcp.tools._json_detail import redact_schema

CommandConfiguration = Annotated[
    dict[str, Any],
    Field(
        description="Exact describe-listed fields for non-shell templates; never generic args.",
    ),
]
ShellCommand = Annotated[
    str,
    Field(
        min_length=1,
        max_length=65_536,
        description=(
            "Flat shell text when describe's example offers this field."
        ),
    ),
]

CommandId = Annotated[int, Field(ge=0, strict=True, description="Exact returned command ID.")]


def _read_command_id(value: Any) -> Any:
    if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,17}", value):
        return int(value)
    return value


ReadCommandId = Annotated[
    int, BeforeValidator(_read_command_id), Field(ge=0, strict=True),
    WithJsonSchema({"anyOf": [
        {"type": "integer", "minimum": 0},
        {"type": "string", "pattern": r"^(0|[1-9][0-9]{0,17})$", "not": {"pattern": "[^0-9]"}},
    ]}),
]
AgentGuid = Annotated[
    str,
    Field(min_length=1, max_length=256, description="Exact ID from agents_list."),
]
TemplateName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        description="Copy a name or ID returned by commands_search.",
    ),
]


class CommandsSearchInput(ToolInput):
    agent_guid: AgentGuid | None = None
    query: Annotated[str, Field(max_length=256)] = ""
    cursor: str | None = None
    limit: Annotated[int, Field(ge=1, le=20)] = 8


class CommandDescribeInput(AliasedToolInput):
    input_aliases = {"command_id": "template"}
    agent_guid: AgentGuid
    template: TemplateName
    cursor: str | None = None
    limit: Annotated[int, Field(ge=1, le=20)] = 8


class CommandAttachmentInput(ToolInput):
    field: Annotated[
        str,
        Field(min_length=1, max_length=128, description="Exact attachment field from describe."),
    ]
    file_id: Annotated[
        str | None,
        Field(max_length=256, description="Existing C2 artifact ID; do not combine with inline data."),
    ] = None
    filename: Annotated[
        str | None,
        Field(max_length=256, description="Required filename for a small inline attachment."),
    ] = None
    content_base64: Annotated[
        str | None,
        Field(
            max_length=8192,
            description="Inline base64; max 4 KiB decoded total.",
        ),
    ] = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> "CommandAttachmentInput":
        inline = self.content_base64 is not None or self.filename is not None
        if self.file_id is not None and inline:
            raise ValueError("attachment must use either file_id or inline filename/content_base64")
        if self.file_id is None and (self.filename is None or self.content_base64 is None):
            raise ValueError("inline attachment requires filename and content_base64")
        return self


ExecUnitType = Literal["SHELLCODE_NATIVE", "DOTNET_EXE", "DOTNET_DLL", "NATIVE_LIB"]


class SelfExecutionInput(ToolInput):
    exec_type: Literal["SELF"] = Field(
        alias="execType",
    )
    exec_unit_type: ExecUnitType | None = Field(
        default=None,
        alias="execUnitType",
        description="Optional describe-listed format.",
    )


class NewExecutionInput(ToolInput):
    exec_type: Literal["NEW"] = Field(
        alias="execType",
    )
    executable: Annotated[str, Field(min_length=1, max_length=1024)]
    suspended: bool | None = None
    username: Annotated[str | None, Field(min_length=1, max_length=512)] = None
    password: Annotated[str | None, Field(min_length=1, max_length=1024)] = None
    ppid: Annotated[int | None, Field(ge=0, strict=True)] = None
    exec_unit_type: ExecUnitType | None = Field(
        default=None,
        alias="execUnitType",
        description="Optional describe-listed format.",
    )

    @model_validator(mode="after")
    def credentials_are_paired(self) -> "NewExecutionInput":
        if (self.username is None) != (self.password is None):
            raise ValueError("username and password must be supplied together")
        return self


class ExistingExecutionInput(ToolInput):
    exec_type: Literal["EXISTING"] = Field(
        alias="execType",
    )
    pid: Annotated[int, Field(ge=1, strict=True)]
    exec_unit_type: ExecUnitType | None = Field(
        default=None,
        alias="execUnitType",
        description="Optional describe-listed format.",
    )


ExecutionInput = Annotated[
    SelfExecutionInput | NewExecutionInput | ExistingExecutionInput,
    Field(discriminator="exec_type"),
]


def _execution_examples() -> list[dict[str, Any]]:
    return [example.model_dump(by_alias=True, exclude_none=True) for example in (
        SelfExecutionInput(execType="SELF"),
        NewExecutionInput(execType="NEW", executable="<executable path>"),
        ExistingExecutionInput(execType="EXISTING", pid=1234),
    )]


class CommandPrepareInput(ToolInput):
    agent_guid: AgentGuid
    template: TemplateName
    configuration: CommandConfiguration | None = None
    shell_command: ShellCommand | None = None
    attachments: list[CommandAttachmentInput] = Field(default_factory=list, max_length=32)
    execution: ExecutionInput | None = Field(
        default=None,
        description="Optional plugin execution object with execType SELF, NEW+executable, or EXISTING+pid. Omit for embedded/native commands.",
    )

    @model_validator(mode="after")
    def exactly_one_configuration_form(self) -> "CommandPrepareInput":
        if (self.configuration is None) == (self.shell_command is None):
            raise ValueError("use exactly one of configuration or shell_command")
        return self


class CommandUpdatePrepareInput(ToolInput):
    command_id: CommandId
    configuration: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Exact describe-listed update fields; interactive input is commonly "
                "{'stdin':['text']}. Never guess create fields."
            )
        ),
    ]
    attachments: list[CommandAttachmentInput] = Field(default_factory=list, max_length=32)
    persistence_type: Literal["TRANSIENT", "PERSISTENT"] = "TRANSIENT"


class CommandStopPrepareInput(ToolInput):
    command_id: CommandId


class OperationExecuteInput(ToolInput):
    preparation_id: Annotated[
        str,
        Field(
            min_length=16,
            max_length=128,
            description="Copy the complete preparation_id from prepare next. Never alter it.",
            examples=["AbCdEfGhIjKlMnOpQrStUvWxYz012345"],
        ),
    ]


class CommandGetInput(ToolInput):
    command_id: ReadCommandId
    wait_seconds: Annotated[
        int, Field(ge=0, le=30, strict=True, description="Status/manifest only: total read wait."),
    ] = 0
    part_index: Annotated[
        int | None,
        Field(
            ge=0,
            description="Text only: exact manifest part_index. Omit for status/manifest.",
        ),
    ] = None
    result_version: Annotated[
        str | None,
        Field(pattern=r"^[0-9a-f]{64}$", description="Text snapshot: copy from manifest or next."),
    ] = None
    offset_chars: Annotated[
        int,
        Field(
            ge=0,
            description="Text only; requires part_index.",
        ),
    ] = 0
    max_chars: Annotated[
        int,
        Field(
            ge=1,
            le=16_384,
            description="Text characters; requires part_index.",
        ),
    ] = 4096
    cursor: Annotated[
        str | None,
        Field(description="Manifest only; copy next_cursor."),
    ] = None
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=50,
            description="Manifest part count; omit with part_index.",
        ),
    ] = 20


class CommandFileDownloadInput(ToolInput):
    command_id: CommandId
    part_index: Annotated[
        int,
        Field(ge=0, description="Exact file part_index from command_get manifest."),
    ]


_TERMINAL_STATUSES = {"COMPLETE", "FAILED", "CANCELED"}
_UPDATABLE_STATUSES = {"SENT", "ONGOING"}
_MAX_INLINE_ATTACHMENT_BYTES = 4 * 1024
_MAX_INLINE_ATTACHMENT_BASE64_CHARS = 4 * ((_MAX_INLINE_ATTACHMENT_BYTES + 2) // 3)
_MAX_DESCRIBE_SCHEMA_ITEMS = 1024
_MAX_DESCRIBE_SCHEMA_DEPTH = 10
_MAX_DESCRIBE_SCHEMA_CHILDREN = 256
_MAX_DESCRIBE_COMPOSITION_BRANCHES = 12


def _store_for(server: Any) -> PreparationStore:
    store = getattr(server, "preparation_store", None)
    if not isinstance(store, PreparationStore):
        store = PreparationStore()
        setattr(server, "preparation_store", store)
    return store


async def _finish_state(transition: Any) -> None:
    """Finish an in-memory safety transition even if the request task is cancelled."""

    async def protected() -> None:
        with anyio.CancelScope(shield=True):
            await transition

    task = asyncio.create_task(protected())
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
    task.result()
    if cancelled is not None:
        raise cancelled


async def _read(invocation: InvocationContext, path: str) -> Any:
    return await call(invocation.context, invocation.server.c2.request_get, path)


def _preferred_template_reference(
    template: Mapping[str, Any],
    candidates: list[dict[str, Any]] | None = None,
) -> str:
    """Return the shortest stable live identity a small model can copy reliably."""
    name = template.get("name")
    if name and (
        candidates is None
        or sum(str(item.get("name") or "").casefold() == str(name).casefold() for item in candidates) == 1
    ):
        return bounded_text(name, 256)
    if template.get("id"):
        return bounded_text(template.get("id"), 256)
    for key in ("fullyQualifiedName", "qualifiedName"):
        if template.get(key):
            return bounded_text(template.get(key), 256)
    return ""


def _not_found_template_hint(
    requested: str,
    candidates: list[dict[str, Any]],
) -> str:
    base = (
        "Do not repeat the rejected value. Search commands again and prefer the returned exact "
        "short name."
    )
    # An intent word ("identity", "process list") is a common wrong guess at a
    # template name. Name the real commands that serve it instead of only
    # telling the model to search again.
    available = {str(item.get("name") or "").casefold() for item in candidates}
    suggested = sorted(matched_templates(requested) & available)
    if suggested:
        return (
            "Do not repeat the rejected value. This agent serves that intent with: "
            + ", ".join(suggested[:5])
            + ". Describe one of those exact names before preparing."
        )
    uuid_like = len(requested) >= 24 and requested.count("-") >= 3
    if not uuid_like:
        return base
    ranked: list[tuple[float, dict[str, Any]]] = []
    for candidate in candidates:
        candidate_id = str(candidate.get("id") or "")
        if not candidate_id:
            continue
        ranked.append((SequenceMatcher(None, requested.casefold(), candidate_id.casefold()).ratio(), candidate))
    if not ranked:
        return base
    score, candidate = max(ranked, key=lambda item: item[0])
    reference = _preferred_template_reference(candidate, candidates)
    if score < 0.75 or not reference:
        return base
    return (
        f"Do not repeat the rejected value. Retry once with the exact template reference "
        f"using {{\"template\":{json.dumps(reference, ensure_ascii=False)}}}."
    )


def _exact_match(
    matches: list[dict[str, Any]],
    requested: str,
    *,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not matches:
        raise ToolFailure(
            "COMMAND_TEMPLATE_NOT_FOUND",
            "The command template is not available to this agent.",
            hint=_not_found_template_hint(requested, candidates or []),
        )
    if len(matches) > 1:
        references = ", ".join(
            repr(_preferred_template_reference(item, matches)) for item in matches[:5]
        )
        raise ToolFailure(
            "AMBIGUOUS_COMMAND_TEMPLATE",
            f"Template {requested!r} matches more than one agent-compatible command.",
            hint=f"Do not repeat the ambiguous name; retry with one exact reference: {references}",
        )
    return matches[0]


async def _template_lookup(
    cache: Any,
    fetch_json: Any,
    agent_guid: str,
    requested: str,
    *,
    force: bool = False,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    return await cache.lookup_template(
        fetch_json,
        agent_guid,
        requested,
        force=force,
    )


_SHELL_COMMAND_FIELDS = ("command", "cmdline", "commandline")


def _shell_command_field(template: Mapping[str, Any]) -> str | None:
    """Return the configuration field a flat ``shell_command`` fills, if any.

    Shell text is the one input a small model reliably mangles, because writing
    it into a nested JSON object means escaping its quotes and backslashes. The
    flat form avoids that, and it is safe for any template whose command string
    is the only thing it requires - `cmd`, `powershell` and `sh`, but equally
    `run`, `powerpick` and `mimikatz`.

    Deciding from the live schema rather than a hardcoded name list keeps this
    correct as plugins change, and keeps it off templates that need more than a
    command: `run-as` also requires credentials and `remote-exec-*` also requires
    a target, both of which the schema declares.

    Any template that declares an attachment at all is excluded, even when the
    schema marks it optional. `load-pe`, `load-elf` and `load-dll` publish an
    optional `executable`/`dll` file plus an unconstrained `commandline`, but the
    plugin rejects the command without one - a rule enforced in the plugin's own
    validation and invisible from here. Offering the flat form for those would
    let a model prepare a call that can only fail after submission.
    """

    schema = template.get("configurationSchema")
    if not isinstance(schema, Mapping):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return None
    field = next((name for name in _SHELL_COMMAND_FIELDS if name in properties), None)
    if field is None:
        return None
    spec = properties.get(field)
    if isinstance(spec, Mapping) and spec.get("type") not in (None, "string"):
        return None

    required = schema.get("required")
    required_names = {str(item) for item in required} if isinstance(required, list) else set()
    if not required_names <= {field}:
        return None

    files = schema.get("files")
    if isinstance(files, Mapping) and files:
        return None
    return field


def _template_rank(
    template: dict[str, Any],
    query: str,
    intent_names: frozenset[str] = frozenset(),
) -> tuple[int, str, str]:
    needle = query.casefold().strip()
    name = str(template.get("name") or "").casefold()
    qualified = str(template.get("qualifiedName") or "").casefold()
    plugin = str(template.get("pluginId") or "").casefold()
    description = str(template.get("description") or "").casefold()
    if not needle:
        score = 1
    elif needle == name:
        score = 100
    elif name.startswith(needle):
        score = 80
    elif needle in name:
        score = 60
    elif needle in qualified:
        score = 40
    elif needle in plugin:
        score = 30
    elif needle in description:
        score = 20
    else:
        score = 0
    # An intent match outranks a coincidental substring hit elsewhere in the
    # catalog, but never an exact or prefix name match the caller clearly meant.
    if name in intent_names:
        score = max(score, 70)
    return score, name, str(template.get("id") or "")


def _template_summary(template: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": bounded_text(template.get("id"), 128),
        "name": bounded_text(template.get("name"), 96),
        "plugin_id": bounded_text(template.get("pluginId"), 128),
        "scope": bounded_text(template.get("scope"), 64),
        "summary": bounded_text(template.get("description"), 192),
        "supports_update": isinstance(template.get("updateSchema"), dict),
        "supports_attachments": bool((template.get("configurationSchema") or {}).get("files")),
    }


def _schema_type(schema: Any) -> Any:
    if schema is True:
        return "any"
    if schema is False:
        return "never"
    if not isinstance(schema, Mapping):
        return None
    value = schema.get("type")
    if isinstance(value, list):
        return [bounded_text(item, 24) for item in value[:6]]
    if isinstance(value, str):
        return bounded_text(value, 48)
    if value is None:
        if "enum" in schema:
            return "enum"
        if "const" in schema:
            return "const"
        if "properties" in schema:
            return "object"
        if "items" in schema or "prefixItems" in schema:
            return "array"
        if any(key in schema for key in ("allOf", "anyOf", "oneOf", "not")):
            return "composed"
    return value


def _pointer_child(path: str, segment: Any) -> str:
    escaped = str(segment).replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}"


def _bounded_path(path: str) -> tuple[str, bool]:
    bounded = bounded_text(path, 384)
    return bounded, bounded != path


def _resolve_local_ref(root: Any, reference: str) -> tuple[Any | None, str]:
    """Resolve one local JSON Pointer without fetching external schemas."""

    if reference == "#":
        return root, "resolved"
    if not reference.startswith("#/"):
        return None, "external" if not reference.startswith("#") else "unsupported"
    try:
        fragment = unquote(reference[1:])
        node: Any = root
        for raw_segment in fragment.split("/")[1:]:
            segment = raw_segment.replace("~1", "/").replace("~0", "~")
            if isinstance(node, Mapping):
                node = node[segment]
            elif isinstance(node, list):
                node = node[int(segment)]
            else:
                return None, "unresolved"
    except (KeyError, IndexError, TypeError, ValueError):
        return None, "unresolved"
    if isinstance(node, Mapping) or isinstance(node, bool):
        return node, "resolved"
    return None, "unresolved"


def _field_item(
    kind: str,
    name: str,
    schema: Any,
    required: bool | None,
    *,
    path: str | None = None,
    schema_context: tuple[str, ...] = (),
    fallback_schema: Any = None,
) -> dict[str, Any]:
    spec = schema if isinstance(schema, Mapping) else {}
    fallback = fallback_schema if isinstance(fallback_schema, Mapping) else {}
    sensitive_field = redact({name: True}).get(name) == "[REDACTED]"
    item: dict[str, Any] = {
        "kind": kind,
        "name": bounded_text(name, 96),
        "type": _schema_type(schema),
    }
    if required is not None:
        item["required"] = required
    if path is not None:
        item["path"], truncated = _bounded_path(path)
        if truncated:
            item["path_truncated"] = True
    if schema_context:
        context = "/".join(schema_context)
        item["schema_context"] = bounded_text(context, 256)
        if item["schema_context"] != context:
            item["schema_context_truncated"] = True
    if isinstance(schema, bool):
        item["allowed"] = schema
    if item["type"] is None and fallback_schema is not None:
        item["type"] = _schema_type(fallback_schema)
    description = spec.get("description", fallback.get("description"))
    if description:
        item["description"] = bounded_text(description, 256)
    if spec.get("title") or fallback.get("title"):
        item["title"] = bounded_text(spec.get("title", fallback.get("title")), 96)
    if isinstance(spec.get("$ref"), str):
        item["ref"] = bounded_text(spec["$ref"], 192)
    if "default" in spec:
        item["default"] = (
            "[REDACTED]"
            if sensitive_field
            else redact(spec.get("default"), max_depth=1, max_items=6, max_string_bytes=64)
        )
    if "const" in spec:
        item["const"] = (
            "[REDACTED]"
            if sensitive_field
            else redact(spec.get("const"), max_depth=1, max_items=6, max_string_bytes=64)
        )
    if isinstance(spec.get("enum"), list):
        values = spec["enum"]
        if sensitive_field:
            item["enum_count"] = len(values)
        else:
            item["enum"] = [bounded_text(value, 64) for value in values[:8]]
        if len(values) > 8:
            item["enum_omitted"] = len(values) - 8
    for key in (
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "pattern",
    ):
        if key in spec:
            if sensitive_field and key == "pattern":
                item["has_pattern"] = True
                continue
            value = spec[key]
            if isinstance(value, str) or len(str(value)) > 64:
                value = bounded_text(value, 128)
            item[key] = value
    additional = spec.get("additionalProperties")
    if isinstance(additional, bool):
        item["additional_properties"] = additional
    elif isinstance(additional, Mapping):
        item["additional_properties"] = "schema"
    for key in ("allOf", "anyOf", "oneOf"):
        branches = spec.get(key)
        if isinstance(branches, list):
            item[f"{key[:-2]}_of_count"] = len(branches)
    if "not" in spec:
        item["has_not"] = True

    nested = spec.get("properties", fallback.get("properties"))
    if isinstance(nested, Mapping):
        names = list(nested)
        item["property_names"] = [bounded_text(value, 64) for value in names[:12]]
        if len(names) > 12:
            item["properties_omitted"] = len(names) - 12
    required_names = spec.get("required", fallback.get("required"))
    if isinstance(required_names, list):
        item["required_properties"] = [bounded_text(value, 64) for value in required_names[:12]]
        if len(required_names) > 12:
            item["required_properties_omitted"] = len(required_names) - 12
    dependent = spec.get("dependentRequired")
    if isinstance(dependent, Mapping):
        normalized: dict[str, list[str]] = {}
        for raw_name, raw_dependencies in list(dependent.items())[:4]:
            if isinstance(raw_dependencies, list):
                normalized[bounded_text(raw_name, 48)] = [
                    bounded_text(value, 48) for value in raw_dependencies[:6]
                ]
        if normalized:
            item["dependent_required"] = normalized
        if len(dependent) > 4:
            item["dependent_required_omitted"] = len(dependent) - 4
    return item


class _SchemaItemCollector:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.stopped = False

    def add(self, item: dict[str, Any], *, path: str = "") -> bool:
        if self.stopped:
            return False
        if len(self.items) >= _MAX_DESCRIBE_SCHEMA_ITEMS - 1:
            bounded_path, truncated = _bounded_path(path)
            notice: dict[str, Any] = {
                "kind": "schema_notice",
                "name": "item_limit",
                "path": bounded_path,
                "reason": "item_limit",
                "maximum_items": _MAX_DESCRIBE_SCHEMA_ITEMS,
            }
            if truncated:
                notice["path_truncated"] = True
            self.items.append(notice)
            self.stopped = True
            return False
        self.items.append(item)
        return True


def _schema_has_children(schema: Any) -> bool:
    if not isinstance(schema, Mapping):
        return False
    if isinstance(schema.get("$ref"), str):
        return True
    if any(
        isinstance(schema.get(key), Mapping) and schema.get(key)
        for key in ("properties", "patternProperties")
    ):
        return True
    if isinstance(schema.get("additionalProperties"), Mapping):
        return True
    if isinstance(schema.get("items"), (Mapping, list, bool)):
        return True
    if isinstance(schema.get("prefixItems"), list):
        return True
    return any(
        isinstance(schema.get(key), (Mapping, list, bool))
        for key in ("allOf", "anyOf", "oneOf", "not", "if", "then", "else", "contains")
    )


def _append_schema_items(
    collector: _SchemaItemCollector,
    *,
    base_kind: str,
    raw_schema: Any,
) -> None:
    root = raw_schema if isinstance(raw_schema, (Mapping, bool)) else {}

    def notice(
        reason: str,
        *,
        path: str,
        context: tuple[str, ...],
        omitted_count: int | None = None,
        reference: str | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "kind": f"{base_kind}_schema_notice",
            "name": reason,
            "reason": reason,
        }
        item["path"], truncated = _bounded_path(path)
        if truncated:
            item["path_truncated"] = True
        if context:
            item["schema_context"] = bounded_text("/".join(context), 256)
        if omitted_count is not None:
            item["omitted_count"] = omitted_count
        if reference is not None:
            item["ref"] = bounded_text(reference, 192)
        collector.add(item, path=path)

    def visit(
        schema: Any,
        *,
        kind: str,
        name: str,
        path: str,
        required: bool | None,
        depth: int,
        context: tuple[str, ...],
        ref_stack: tuple[str, ...],
        extras: Mapping[str, Any] | None = None,
    ) -> None:
        if collector.stopped:
            return
        fallback = None
        if isinstance(schema, Mapping) and isinstance(schema.get("$ref"), str):
            fallback, _ = _resolve_local_ref(root, schema["$ref"])
        item = _field_item(
            kind,
            name,
            schema,
            required,
            path=path,
            schema_context=context,
            fallback_schema=fallback,
        )
        if extras:
            item.update(extras)
        if not collector.add(item, path=path):
            return
        if depth >= _MAX_DESCRIBE_SCHEMA_DEPTH:
            if _schema_has_children(schema):
                notice("max_depth", path=path, context=context)
            return
        walk_children(
            schema,
            path=path,
            depth=depth,
            context=context,
            ref_stack=ref_stack,
        )

    def visit_branches(
        schema: Mapping[str, Any],
        *,
        keyword: str,
        path: str,
        depth: int,
        context: tuple[str, ...],
        ref_stack: tuple[str, ...],
    ) -> None:
        branches = schema.get(keyword)
        if not isinstance(branches, list):
            return
        for index, branch in enumerate(branches[:_MAX_DESCRIBE_COMPOSITION_BRANCHES]):
            if not isinstance(branch, (Mapping, bool)):
                continue
            label = f"{keyword}[{index}]"
            branch_context = (*context, label)
            visit(
                branch,
                kind=f"{base_kind}_schema",
                name=label,
                path=path,
                required=None,
                depth=depth + 1,
                context=branch_context,
                ref_stack=ref_stack,
                extras={"relation": keyword, "branch_index": index},
            )
        if len(branches) > _MAX_DESCRIBE_COMPOSITION_BRANCHES:
            notice(
                "branch_limit",
                path=path,
                context=(*context, keyword),
                omitted_count=len(branches) - _MAX_DESCRIBE_COMPOSITION_BRANCHES,
            )

    def walk_children(
        schema: Any,
        *,
        path: str,
        depth: int,
        context: tuple[str, ...],
        ref_stack: tuple[str, ...],
    ) -> None:
        if collector.stopped or not isinstance(schema, Mapping):
            return

        reference = schema.get("$ref")
        if isinstance(reference, str):
            target, status = _resolve_local_ref(root, reference)
            ref_context = (*context, f"$ref:{bounded_text(reference, 128)}")
            if reference in ref_stack:
                notice("ref_cycle", path=path, context=ref_context, reference=reference)
            elif target is None:
                notice(f"ref_{status}", path=path, context=ref_context, reference=reference)
            else:
                visit(
                    target,
                    kind=f"{base_kind}_schema",
                    name="$ref",
                    path=path,
                    required=None,
                    depth=depth + 1,
                    context=ref_context,
                    ref_stack=(*ref_stack, reference),
                    extras={"relation": "$ref", "ref": bounded_text(reference, 192)},
                )

        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            raw_required = schema.get("required")
            required_names = (
                {str(value) for value in raw_required}
                if isinstance(raw_required, list)
                else set()
            )
            names = sorted(properties, key=lambda value: (str(value) not in required_names, str(value)))
            for raw_name in names[:_MAX_DESCRIBE_SCHEMA_CHILDREN]:
                name = str(raw_name)
                child = properties[raw_name]
                visit(
                    child,
                    kind=f"{base_kind}_field",
                    name=name,
                    path=_pointer_child(path, name),
                    required=name in required_names,
                    depth=depth + 1,
                    context=context,
                    ref_stack=ref_stack,
                )
            if len(names) > _MAX_DESCRIBE_SCHEMA_CHILDREN:
                notice(
                    "child_limit",
                    path=path,
                    context=context,
                    omitted_count=len(names) - _MAX_DESCRIBE_SCHEMA_CHILDREN,
                )

        items_schema = schema.get("items")
        if isinstance(items_schema, list):
            for index, child in enumerate(items_schema[:_MAX_DESCRIBE_SCHEMA_CHILDREN]):
                if isinstance(child, (Mapping, bool)):
                    visit(
                        child,
                        kind=f"{base_kind}_item",
                        name=str(index),
                        path=_pointer_child(path, index),
                        required=None,
                        depth=depth + 1,
                        context=context,
                        ref_stack=ref_stack,
                        extras={"relation": "tuple_item", "item_index": index},
                    )
            if len(items_schema) > _MAX_DESCRIBE_SCHEMA_CHILDREN:
                notice(
                    "child_limit",
                    path=path,
                    context=(*context, "items"),
                    omitted_count=len(items_schema) - _MAX_DESCRIBE_SCHEMA_CHILDREN,
                )
        elif isinstance(items_schema, (Mapping, bool)):
            visit(
                items_schema,
                kind=f"{base_kind}_item",
                name="*",
                path=_pointer_child(path, "*"),
                required=None,
                depth=depth + 1,
                context=context,
                ref_stack=ref_stack,
                extras={"relation": "items"},
            )

        prefix_items = schema.get("prefixItems")
        if isinstance(prefix_items, list):
            for index, child in enumerate(prefix_items[:_MAX_DESCRIBE_SCHEMA_CHILDREN]):
                if isinstance(child, (Mapping, bool)):
                    visit(
                        child,
                        kind=f"{base_kind}_item",
                        name=str(index),
                        path=_pointer_child(path, index),
                        required=None,
                        depth=depth + 1,
                        context=context,
                        ref_stack=ref_stack,
                        extras={"relation": "prefixItems", "item_index": index},
                    )
            if len(prefix_items) > _MAX_DESCRIBE_SCHEMA_CHILDREN:
                notice(
                    "child_limit",
                    path=path,
                    context=(*context, "prefixItems"),
                    omitted_count=len(prefix_items) - _MAX_DESCRIBE_SCHEMA_CHILDREN,
                )

        additional = schema.get("additionalProperties")
        if isinstance(additional, Mapping):
            visit(
                additional,
                kind=f"{base_kind}_additional_property",
                name="*",
                path=_pointer_child(path, "*"),
                required=None,
                depth=depth + 1,
                context=(*context, "additionalProperties"),
                ref_stack=ref_stack,
                extras={"relation": "additionalProperties"},
            )

        pattern_properties = schema.get("patternProperties")
        if isinstance(pattern_properties, Mapping):
            patterns = sorted(pattern_properties, key=str)
            for raw_pattern in patterns[:_MAX_DESCRIBE_SCHEMA_CHILDREN]:
                pattern = str(raw_pattern)
                visit(
                    pattern_properties[raw_pattern],
                    kind=f"{base_kind}_pattern_property",
                    name=pattern,
                    path=_pointer_child(path, "*"),
                    required=None,
                    depth=depth + 1,
                    context=(*context, f"pattern:{bounded_text(pattern, 96)}"),
                    ref_stack=ref_stack,
                    extras={"relation": "patternProperties", "property_pattern": bounded_text(pattern, 128)},
                )
            if len(patterns) > _MAX_DESCRIBE_SCHEMA_CHILDREN:
                notice(
                    "child_limit",
                    path=path,
                    context=(*context, "patternProperties"),
                    omitted_count=len(patterns) - _MAX_DESCRIBE_SCHEMA_CHILDREN,
                )

        for keyword in ("allOf", "anyOf", "oneOf"):
            visit_branches(
                schema,
                keyword=keyword,
                path=path,
                depth=depth,
                context=context,
                ref_stack=ref_stack,
            )
        for keyword in ("not", "if", "then", "else", "contains"):
            child = schema.get(keyword)
            if isinstance(child, (Mapping, bool)):
                relation_context = (*context, keyword)
                visit(
                    child,
                    kind=f"{base_kind}_schema",
                    name=keyword,
                    path=_pointer_child(path, "*") if keyword == "contains" else path,
                    required=None,
                    depth=depth + 1,
                    context=relation_context,
                    ref_stack=ref_stack,
                    extras={"relation": keyword},
                )

    visit(
        root,
        kind=f"{base_kind}_schema",
        name="$",
        path="",
        required=None,
        depth=0,
        context=(),
        ref_stack=(),
        extras={"relation": "root"},
    )


def _describe_items(template: dict[str, Any]) -> list[dict[str, Any]]:
    collector = _SchemaItemCollector()
    for base_kind, raw_schema in (
        ("configuration", template.get("configurationSchema")),
        ("update", template.get("updateSchema")),
    ):
        if not isinstance(raw_schema, (Mapping, bool)):
            continue
        # Resolve privacy before flattening references into anonymous "$ref"
        # nodes, where a sensitive property's name is no longer available.
        safe_schema = redact_schema(raw_schema)
        if safe_schema != raw_schema:
            collector.add({
                "kind": f"{base_kind}_schema_notice",
                "name": "redacted_literals",
                "reason": "secret_literals_redacted",
                "notice": "[REDACTED] secret literals; displayed constraints may be incomplete.",
            })
        _append_schema_items(collector, base_kind=base_kind, raw_schema=safe_schema)
        schema = safe_schema if isinstance(safe_schema, Mapping) else {}
        files = schema.get("files") if isinstance(schema.get("files"), Mapping) else {}
        for name in sorted(files):
            spec = files[name] if isinstance(files[name], Mapping) else {}
            collector.add(
                _field_item(
                    f"{base_kind}_attachment",
                    str(name),
                    spec,
                    bool(spec.get("required")),
                )
            )

    items = collector.items

    examples = template.get("exampleConfigurations") or []
    if template.get("defaultConfiguration") is not None:
        examples = [{"name": "default", "configuration": template.get("defaultConfiguration")}, *examples]
    for example in examples[:8]:
        if not isinstance(example, Mapping):
            continue
        configuration = example.get("configuration")
        if isinstance(configuration, Mapping):
            configuration = {
                str(key): value
                for key, value in configuration.items()
                if key != "@files" and not str(key).startswith("@files.")
            }
        items.append(
            {
                "kind": "configuration_example",
                "name": bounded_text(example.get("name"), 96),
                "configuration": redact(configuration or {}, max_depth=1, max_items=4, max_string_bytes=64),
            }
        )
    return items


def _prepare_example(
    agent_guid: str,
    template: dict[str, Any],
    candidates: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], ExampleSkeleton]:
    raw_schema = template.get("configurationSchema")
    skeleton = build_example(raw_schema if isinstance(raw_schema, Mapping) else {})
    shell_field = _shell_command_field(template)
    out: dict[str, Any] = {
        "agent_guid": bounded_text(agent_guid, 256),
        "template": _preferred_template_reference(template, candidates),
    }
    if shell_field:
        out["shell_command"] = str(skeleton.configuration.get(shell_field) or "COMMAND_TEXT_GOES_HERE")
    else:
        out["configuration"] = skeleton.configuration
    if skeleton.required_files:
        out["attachments"] = [
            {"field": name, "filename": "REPLACE_WITH_FILE_FILENAME",
             "content_base64": "REPLACE_WITH_SMALL_BASE64_CONTENT"}
            for name in skeleton.required_files
        ]
    return out, skeleton


def _validation_errors(schema: Any, configuration: dict[str, Any]) -> list[str]:
    if not isinstance(schema, Mapping):
        return []
    standard_schema = {key: value for key, value in schema.items() if key != "files"}
    try:
        validator_class = validator_for(standard_schema)
        validator_class.check_schema(standard_schema)
        # An explicit empty registry permits embedded references, but never
        # performs jsonschema's deprecated automatic URL/file retrieval.
        validator = validator_class(standard_schema, registry=Registry())
        errors = sorted(
            islice(validator.iter_errors(configuration), 8),
            key=lambda item: tuple(
                (0, value)
                if isinstance(value, int) and not isinstance(value, bool)
                else (1, str(value))
                for value in item.absolute_path
            ),
        )
    except Exception as exc:
        raise ToolFailure(
            "INVALID_TEMPLATE_SCHEMA",
            "The live command template contains an invalid configuration schema.",
            hint="Ask the Tuoni administrator to repair the selected command template.",
        ) from exc
    formatted = []
    for error in errors[:8]:
        path = "configuration"
        for value in error.absolute_path:
            if isinstance(value, int):
                path += f"[{value}]"
            else:
                # Paths name schema fields, but keep arbitrary instance keys out of errors.
                segment = str(value)
                if not segment.isidentifier() or len(segment) > 64:
                    path += ".field"
                else:
                    path += f".{segment}"
        if error.validator == "required" and isinstance(error.validator_value, list):
            present = set(error.instance) if isinstance(error.instance, Mapping) else set()
            missing = [
                str(name)
                for name in error.validator_value
                if isinstance(name, str) and name not in present and name.isidentifier() and len(name) <= 64
            ]
            detail = "missing required field" + ("s" if len(missing) != 1 else "")
            if missing:
                detail += ": " + ", ".join(missing[:8])
        elif error.validator == "additionalProperties":
            detail = "contains unsupported fields"
        else:
            validator = error.validator if error.validator in {
                "type",
                "enum",
                "const",
                "pattern",
                "format",
                "minimum",
                "maximum",
                "exclusiveMinimum",
                "exclusiveMaximum",
                "multipleOf",
                "minLength",
                "maxLength",
                "minItems",
                "maxItems",
                "uniqueItems",
                "minProperties",
                "maxProperties",
                "dependentRequired",
                "allOf",
                "anyOf",
                "oneOf",
                "not",
                "if",
            } else "schema"
            detail = f"does not satisfy {validator} constraint"
        formatted.append(f"{path}: {detail}")
    return formatted


def _validate_configuration(
    template: dict[str, Any],
    configuration: dict[str, Any],
    attachment_fields: set[str],
    *,
    update: bool = False,
) -> None:
    schema_key = "updateSchema" if update else "configurationSchema"
    schema = template.get(schema_key)
    if update and not isinstance(schema, Mapping):
        raise ToolFailure(
            "COMMAND_NOT_UPDATABLE",
            "This command template does not publish an update schema.",
            hint="Choose an ongoing command whose describe result includes update fields.",
        )
    schema = schema if isinstance(schema, Mapping) else {}
    misplaced = sorted(
        str(key) for key in configuration if key == "@files" or str(key).startswith("@files.")
    )
    if misplaced:
        raise ToolFailure(
            "MISPLACED_ATTACHMENTS",
            "Attachment fields must not be placed inside configuration.",
            hint="Use top-level attachments entries with field plus file_id or filename/content_base64.",
        )
    errors = _validation_errors(schema, configuration)
    shell_field = None if update else _shell_command_field(template)
    if shell_field is not None:
        command = configuration.get(shell_field)
        stdin = configuration.get("stdin")
        if not (isinstance(command, str) and command.strip()) and not (
            isinstance(stdin, list) and any(str(value).strip() for value in stdin)
        ):
            errors.append(f"configuration: non-empty {shell_field} or stdin is required")

    file_schema = schema.get("files") if isinstance(schema.get("files"), Mapping) else {}
    unknown_files = sorted(attachment_fields - {str(name) for name in file_schema})
    required_files = {
        str(name)
        for name, spec in file_schema.items()
        if isinstance(spec, Mapping) and spec.get("required") is True
    }
    missing_files = sorted(required_files - attachment_fields)
    if unknown_files:
        errors.append("unknown attachment fields: " + ", ".join(unknown_files))
    if missing_files:
        errors.append("missing required attachment fields: " + ", ".join(missing_files))
    if errors:
        # The hint is capped at 360 characters. Lead with the correction, because
        # a list of individual violations can fill the whole budget and truncate
        # away the field names the model needs to build a valid configuration.
        names = [str(name) for name in (schema.get("properties") or {})]
        allowed = ", ".join(names[:12]) + (f" (+{len(names) - 12} more)" if len(names) > 12 else "")
        parts: list[str] = []
        if shell_field is not None or _shell_command_field(template) is not None:
            parts.append("retry with top-level shell_command for shell text")
        elif allowed:
            parts.append(f"allowed configuration fields: {allowed}")
        parts.extend(errors[:3])
        raise ToolFailure(
            "INVALID_COMMAND_CONFIGURATION",
            "Command configuration was rejected; nothing was sent.",
            hint="; ".join(parts),
        )


def _execution_applicable(template: dict[str, Any]) -> bool:
    # Native aliases retain the embedded command's plugin identity. An empty
    # exec-unit catalog alone does not imply an embedded command (e.g. spawn).
    return template.get("pluginId") != "shelldot.commands.native"


def _validate_execution(template: dict[str, Any], execution: dict[str, Any] | None) -> None:
    if execution is None:
        return
    if not _execution_applicable(template):
        raise ToolFailure(
            "EXECUTION_NOT_APPLICABLE",
            "Execution settings do not apply to embedded/native commands.",
            hint="Omit execution; this command runs inside the agent.",
        )
    exec_type = execution.get("execType")
    if exec_type not in {"SELF", "NEW", "EXISTING"}:
        raise ToolFailure(
            "INVALID_EXECUTION",
            "execution.execType must be SELF, NEW, or EXISTING.",
        )

    exec_unit_type = execution.get("execUnitType")
    supported = [str(value) for value in template.get("supportedExecUnitTypes") or []]
    if exec_unit_type is not None and exec_unit_type not in supported:
        supported_hint = ", ".join(supported) if supported else "none advertised"
        raise ToolFailure(
            "INVALID_EXECUTION",
            f"ExecUnit format {exec_unit_type!r} is not supported by this template.",
            hint=(
                "Omit execution.execUnitType to let C2 choose a compatible format; "
                f"template formats: {supported_hint}."
            ),
        )

    if exec_type == "NEW" and not (
        isinstance(execution.get("executable"), str) and execution["executable"].strip()
    ):
        raise ToolFailure("INVALID_EXECUTION", "NEW execution requires execution.executable.")
    if exec_type == "NEW" and ((execution.get("username") is None) != (execution.get("password") is None)):
        raise ToolFailure(
            "INVALID_EXECUTION",
            "NEW execution requires username and password to be supplied together.",
        )
    pid = execution.get("pid")
    if exec_type == "EXISTING" and (not isinstance(pid, int) or isinstance(pid, bool) or pid < 1):
        raise ToolFailure("INVALID_EXECUTION", "EXISTING execution requires a positive integer execution.pid.")


def _file_fingerprint(metadata: Mapping[str, Any]) -> str:
    material = {
        key: metadata.get(key)
        for key in ("fileId", "originalFileName", "size", "downloadHref")
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


async def _prepare_attachments(
    invocation: InvocationContext,
    attachments: list[CommandAttachmentInput],
) -> tuple[PreparedAttachment, ...]:
    fields = [item.field for item in attachments]
    if len(fields) != len(set(fields)):
        raise ToolFailure("DUPLICATE_ATTACHMENT_FIELD", "Each attachment field may appear only once.")
    prepared: list[PreparedAttachment] = []
    inline_total = 0
    for item in attachments:
        if item.file_id is not None:
            metadata = await _read(
                invocation,
                f"/api/v1/file/{url_path_segment(item.file_id)}",
            )
            if not isinstance(metadata, Mapping):
                raise ToolFailure("FILE_NOT_FOUND", f"C2 file {item.file_id!r} was not found.")
            content = await _read_c2_file_content(invocation, metadata, item.file_id)
            prepared.append(
                PreparedAttachment(
                    field=item.field,
                    filename=bounded_text(metadata.get("originalFileName") or item.field, 256),
                    file_id=item.file_id,
                    file_fingerprint=_file_fingerprint(metadata),
                    content_fingerprint=hashlib.sha256(content).hexdigest(),
                )
            )
            continue
        try:
            normalized = "".join((item.content_base64 or "").split())
            if len(normalized) > _MAX_INLINE_ATTACHMENT_BASE64_CHARS:
                raise ToolFailure(
                    "INLINE_ATTACHMENT_TOO_LARGE",
                    "Inline attachment data exceeds the 4 KiB decoded agent-ops limit.",
                    hint="Upload larger content through artifacts, then prepare with its file_id.",
                )
            content = base64.b64decode(normalized, validate=True)
        except ToolFailure:
            raise
        except (binascii.Error, ValueError) as exc:
            raise ToolFailure(
                "INVALID_BASE64",
                f"Inline attachment {item.field!r} is not valid base64.",
            ) from exc
        inline_total += len(content)
        if inline_total > _MAX_INLINE_ATTACHMENT_BYTES:
            raise ToolFailure(
                "INLINE_ATTACHMENT_TOO_LARGE",
                "Inline attachment data exceeds the 4 KiB decoded agent-ops limit.",
                hint="Upload larger content through the artifacts profile, then prepare with its file_id.",
            )
        prepared.append(
            PreparedAttachment(field=item.field, filename=item.filename, content=content)
        )
    return tuple(prepared)


def _safe_url(value: Any) -> str | None:
    if not value:
        return None
    parsed = urlsplit(str(value))
    # Never echo user info, query credentials, or fragments. Same-server result
    # links remain usable as paths.
    path = parsed.path or "/"
    return bounded_text(path, 384)


def _download_path(value: str) -> str:
    """Convert server-provided absolute download URLs for TuoniC2's base-URL client."""
    parsed = urlsplit(value)
    path = "/" + (parsed.path or "").lstrip("/")
    if parsed.query:
        path += "?" + parsed.query
    return path


def _safe_result_filename(value: Any, command_id: int, part_index: int) -> str:
    raw = str(value or f"result-{command_id}-{part_index}.bin").replace("\\", "/")
    leaf = raw.rsplit("/", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", leaf).strip("._-")
    if not cleaned:
        cleaned = f"result-{command_id}-{part_index}.bin"
    return cleaned[:128]


def _save_result_download(
    root: Path,
    *,
    command_id: int,
    part_index: int,
    filename: str,
    content: bytes,
) -> tuple[Path, str, bool]:
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root = root.resolve(strict=True)
    except OSError as exc:
        raise ToolFailure(
            "DOWNLOAD_DIRECTORY_UNAVAILABLE",
            "The configured local download directory is unavailable.",
            hint="Check TUONI_MCP_DOWNLOAD_DIR and its container bind mount.",
        ) from exc
    if not root.is_dir():
        raise ToolFailure("DOWNLOAD_DIRECTORY_UNAVAILABLE", "The configured download path is not a directory.")

    digest = hashlib.sha256(content).hexdigest()
    safe_name = _safe_result_filename(filename, command_id, part_index)
    destination = root / f"command-{command_id}-part-{part_index}-{digest[:12]}-{safe_name}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError:
        try:
            if destination.is_symlink():
                raise OSError("Existing artifact is a symbolic link")
            read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(destination, read_flags), "rb") as existing:
                info = os.fstat(existing.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != len(content):
                    raise ValueError("file type or size changed")
                actual = hashlib.sha256()
                for chunk in iter(lambda: existing.read(64 * 1024), b""):
                    actual.update(chunk)
                if actual.hexdigest() != digest:
                    raise ValueError("file content changed")
        except (OSError, ValueError) as exc:
            raise ToolFailure(
                "DOWNLOAD_PATH_COLLISION",
                "The local download path is occupied by a different or modified file.",
                hint="Move the conflicting file out of the download directory, then retry.",
            ) from exc
        return destination, digest, True
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination, digest, False


def _result_file_download_call(
    command_id: int,
    part_index: int,
    *,
    dispatch: bool = False,
) -> dict[str, Any]:
    """Build the download call for a file part, in the caller's own vocabulary.

    This one is embedded inside ``data`` rather than returned as ``next``, so
    neither the envelope's prefix rewriting nor the dispatch wrapper reaches it.
    Both have to be applied here, or the skill's instruction to "use its returned
    download continuation" names a tool the client cannot call.
    """
    arguments = {"command_id": command_id, "part_index": part_index}
    if dispatch:
        return {
            "name": exposed_name("tuoni_call"),
            "arguments": {"name": "tuoni_command_file_download", "arguments": arguments},
        }
    return {"name": exposed_name("tuoni_command_file_download"), "arguments": arguments}


async def _read_c2_file_content(
    invocation: InvocationContext,
    metadata: Mapping[str, Any],
    file_id: str,
) -> bytes:
    href = metadata.get("downloadHref")
    if not isinstance(href, str) or not href:
        raise ToolFailure(
            "FILE_DOWNLOAD_UNAVAILABLE",
            f"Referenced C2 file {file_id!r} has no download link.",
        )
    bounded_download = getattr(invocation.server.c2, "request_get_bounded_bytes", None)
    if callable(bounded_download):
        content = await call(invocation.context, bounded_download, _download_path(href), 2 * 1024 * 1024)
    else:
        content = await call(
            invocation.context, invocation.server.c2.request_get, _download_path(href), False, True,
        )
    if not isinstance(content, (bytes, bytearray)):
        raise ToolFailure(
            "FILE_DOWNLOAD_FAILED",
            f"Referenced C2 file {file_id!r} did not return bytes.",
        )
    return bytes(content)


def _text_version(part: Mapping[str, Any], index: int, command_id: int | None) -> str:
    """Identify the complete visible part, including its position and name."""
    identity = [command_id, index, bounded_text(part.get("name"), 128), result_text(part.get("value"))]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _part_summary(
    part: Mapping[str, Any],
    index: int,
    *,
    command_id: int | None = None,
    download_enabled: bool = False,
    dispatch: bool = False,
) -> dict[str, Any]:
    part_type = bounded_text(part.get("type"), 32)
    out: dict[str, Any] = {"part_index": index, "type": part_type, "chunkable": False}
    if part_type == "text":
        value = result_text(part.get("value"))
        out.update({"name": bounded_text(part.get("name"), 128), "total_chars": len(value), "chunkable": True,
                    "result_version": _text_version(part, index, command_id)})
    elif part_type == "file":
        out.update(
            {
                "filename": bounded_text(part.get("filename"), 192),
                "resource_path": _safe_url(part.get("href")),
                "resource_kind": "authenticated_http",
                "not_mcp_resource": True,
            }
        )
        if download_enabled and command_id is not None:
            out["download"] = _result_file_download_call(command_id, index, dispatch=dispatch)
    elif part_type == "binary":
        raw = part.get("result")
        out.update(
            {
                "name": bounded_text(part.get("name"), 128),
                "encoded_chars": len(raw) if isinstance(raw, str) else 0,
            }
        )
    elif part_type == "omitted":
        out.update(
            {
                "name": bounded_text(part.get("name"), 128),
                "real_type": bounded_text(part.get("realType"), 32),
                "size": bounded_text(part.get("size"), 64),
            }
        )
    return out


def _first_result_followup_call(
    command_id: int,
    summaries: list[dict[str, Any]],
    *,
    download_enabled: bool,
) -> dict[str, Any] | None:
    for summary in summaries:
        if summary.get("type") != "text" or not summary.get("chunkable"):
            continue
        return {
            "name": "tuoni_command_get",
            "arguments": {
                "command_id": command_id,
                "part_index": summary["part_index"],
                "result_version": summary["result_version"],
                "offset_chars": 0,
                "max_chars": 4096,
            },
        }
    if download_enabled:
        for summary in summaries:
            if summary.get("type") == "file" and "download" in summary:
                # Deliberately rebuilt rather than reusing summary["download"],
                # which is already rendered in the client's vocabulary. The
                # envelope applies the prefix, and dispatch rewrites the target,
                # so this has to hand them the plain server-side call.
                return {
                    "name": "tuoni_command_file_download",
                    "arguments": {
                        "command_id": command_id,
                        "part_index": summary["part_index"],
                    },
                }
    return None


async def _create_preparation(
    invocation: InvocationContext,
    kind: Literal["command.create", "command.update", "command.stop", "tool.call"],
    *,
    payload: dict[str, Any],
    attachments: tuple[PreparedAttachment, ...] = (),
    summary: dict[str, Any],
    state_fingerprint: str | None = None,
) -> ToolSuccess:
    # token_urlsafe(24), used by PreparationStore, always emits 32 ASCII
    # characters. Measure the exact final envelope before retaining private
    # request state; otherwise an unreturnable summary would leave an opaque,
    # unreachable preparation consuming capacity until its TTL.
    preview_id = "P" * 32
    preview_data = {
        "preparation_id": preview_id,
        "operation": kind,
        "expires_in_seconds": 300,
        "summary": summary,
    }
    preview_next = {
        "name": "tuoni_operation_execute",
        "arguments": {"preparation_id": preview_id},
    }
    if not would_fit_success(
        preview_data,
        next_call=preview_next,
        budget=DETAIL_RESULT_BUDGET,
    ):
        raise ToolFailure(
            "PREPARATION_SUMMARY_TOO_LARGE",
            "The reviewed preparation summary exceeds the 6 KiB response limit.",
            hint="Reduce configuration fields or attachments and prepare again; nothing was queued or retained.",
        )
    try:
        record = await _store_for(invocation.server).create(
            kind,
            payload=payload,
            attachments=attachments,
            summary=summary,
            state_fingerprint=state_fingerprint,
        )
    except ValueError as exc:
        raise ToolFailure(
            "PREPARATION_CAPACITY",
            str(exc),
            hint="Execute or let old preparations expire before creating another.",
        ) from exc
    data = {
        "preparation_id": record.preparation_id,
        "operation": record.kind,
        "expires_in_seconds": 300,
        "summary": summary,
    }
    return ToolSuccess(
        data,
        next_call={
            "name": "tuoni_operation_execute",
            "arguments": {"preparation_id": record.preparation_id},
        },
    )


def _sensitive_dynamic_name(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return redact({value: True}).get(value) == "[REDACTED]"


def _redact_named_values(value: Any) -> Any:
    """Redact bounded ``name``/``value`` credential pairs recursively."""
    if isinstance(value, Mapping):
        result = {str(key): _redact_named_values(item) for key, item in value.items()}
        if "value" in result and any(
            _sensitive_dynamic_name(value.get(key)) for key in ("name", "field", "key")
        ):
            result["value"] = "[REDACTED]"
        return result
    if isinstance(value, list):
        return [_redact_named_values(item) for item in value]
    return value


def _review_preview(value: Any, *, max_bytes: int = 2048) -> Any:
    """Return a structurally bounded, credential-redacted review value.

    The tiers preserve ordinary scalar and small configuration values. Oversized
    collections are reduced as whole JSON values, never by slicing serialized JSON.
    ``redact_result`` additionally removes credentials embedded in free-form text.
    """
    tiers = (
        (4, 16, 512),
        (4, 12, 256),
        (3, 8, 160),
        (3, 6, 96),
        (2, 4, 64),
        (1, 2, 48),
    )
    for max_depth, max_items, max_string_bytes in tiers:
        candidate = redact(
            value,
            max_depth=max_depth,
            max_items=max_items,
            max_string_bytes=max_string_bytes,
        )
        candidate = redact_result(_redact_named_values(candidate))
        encoded = json.dumps(
            candidate,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        if len(encoded) <= max_bytes:
            return candidate

    if isinstance(value, Mapping):
        return {"truncated": True, "kind": "object", "item_count": len(value)}
    if isinstance(value, list):
        return {"truncated": True, "kind": "array", "item_count": len(value)}
    return bounded_text(redact_result(value), min(max_bytes, 48))


def _execution_inspection(registry: ToolRegistry, invocation: InvocationContext, record) -> dict[str, Any] | None:
    """Choose an enabled, bounded read before a terminal record drops its inputs."""
    if record.kind == "command.create":
        return {"name": "tuoni_commands_list", "arguments": {"agent_guid": record.payload["agent_guid"]}}
    if record.kind in {"command.update", "command.stop"}:
        return {"name": "tuoni_command_get", "arguments": {"command_id": record.payload["command_id"]}}
    return mutation_inspection(registry, invocation, str(record.payload.get("tool_name") or ""))


class _GenericMutationBackend(MutationPreparationBackend):
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def prepare(
        self,
        invocation: InvocationContext,
        spec: ToolSpec,
        arguments: ToolInput,
    ) -> ToolSuccess:
        if spec.effect is not Effect.MUTATE:
            raise ToolFailure("NOT_PREPARABLE", "Only mutation tools use generic preparations.")
        raw_arguments = arguments.model_dump(mode="python")
        argument_summary = _review_preview(raw_arguments)
        upload_source = await to_thread(prepare_upload, invocation.server.cfg, spec.name, raw_arguments,
                                        _timeout=invocation.server.cfg.request_timeout)
        payload = {"tool_name": spec.name, "arguments": raw_arguments}
        summary = {"tool_name": spec.name, "arguments": argument_summary}
        if upload_source is not None:
            payload["upload_source"] = upload_source
            summary["upload_source"] = {key: value for key, value in upload_source.items() if key != "fingerprint"}
        if spec.name == "tuoni_payload_create":
            from tuoni_mcp.tools.payloads import check_payload_listener_compatibility

            summary["compatibility"] = await check_payload_listener_compatibility(
                invocation.context, raw_arguments["template"], raw_arguments["listener_id"],
            )
        return await _create_preparation(
            invocation,
            "tool.call",
            payload=payload,
            summary=summary,
        )


def _command_receipt(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "command_id": raw.get("id"),
        "template_id": bounded_text(raw.get("commandTemplateId"), 128),
        "template_name": bounded_text(raw.get("commandTemplateName"), 96),
        "status": bounded_text(raw.get("status"), 32),
        "created": bounded_text(raw.get("created"), 64),
        "sent": bounded_text(raw.get("sent"), 64),
    }


async def _materialize_attachments(
    invocation: InvocationContext,
    attachments: tuple[PreparedAttachment, ...],
) -> dict[str, list[Any]] | None:
    if not attachments:
        return None
    out: dict[str, list[Any]] = {}
    for attachment in attachments:
        if attachment.file_id is None:
            out[attachment.field] = [attachment.filename or attachment.field, attachment.content or b""]
            continue
        metadata = await _read(
            invocation,
            f"/api/v1/file/{url_path_segment(attachment.file_id)}",
        )
        if not isinstance(metadata, Mapping):
            raise ToolFailure(
                "PREPARATION_DRIFT",
                f"Referenced C2 file {attachment.file_id!r} no longer exists.",
                hint="Create a new preparation with a current artifact.",
            )
        if _file_fingerprint(metadata) != attachment.file_fingerprint:
            raise ToolFailure(
                "PREPARATION_DRIFT",
                f"Referenced C2 file {attachment.file_id!r} changed after preparation.",
                hint="Review the new file and create a new preparation.",
            )
        content = await _read_c2_file_content(invocation, metadata, attachment.file_id)
        if hashlib.sha256(content).hexdigest() != attachment.content_fingerprint:
            raise ToolFailure(
                "PREPARATION_DRIFT",
                f"Referenced C2 file {attachment.file_id!r} content changed after preparation.",
                hint="Review the new file and create a new preparation.",
            )
        out[attachment.field] = [attachment.filename or attachment.field, content]
    return out


def _normalize_generic_result(value: Any) -> Any:
    if isinstance(value, ToolSuccess):
        normalized = {
            "data": redact(value.data),
            "next_cursor": value.next_cursor,
            "next": redact(value.next_call),
        }
    elif isinstance(value, CallToolResult):
        content: list[Any] = []
        for block in value.content[:4]:
            if getattr(block, "type", None) == "text":
                text = bounded_text(getattr(block, "text", ""), 1024)
                try:
                    content.append(redact(json.loads(text)))
                except json.JSONDecodeError:
                    content.append(text)
            else:
                content.append({"type": getattr(block, "type", "unknown")})
        normalized = {"is_error": bool(value.isError), "content": content}
    else:
        normalized = redact(value)
    # Mutation handlers adapt client capabilities and may return arbitrary
    # server payloads. Keep the definite receipt useful and replayable without
    # allowing one response to bypass the 8 KiB MCP or 2 MiB store ceilings.
    return _review_preview(normalized, max_bytes=4 * 1024)


def register_specs(registry: ToolRegistry, cfg: Settings) -> None:
    """Register agent-ops command tools and the artifacts result downloader."""

    result_download_enabled = Profile.ARTIFACTS in cfg.profiles
    dispatch_mode = cfg.mode is MCPMode.DISPATCH

    async def fetch(invocation: InvocationContext, path: str) -> Any:
        return await _read(invocation, path)

    async def commands_search(invocation: InvocationContext, model: ToolInput) -> ToolSuccess:
        arguments = cast(CommandsSearchInput, model)
        if arguments.agent_guid is None:
            return ToolSuccess(
                {"agent_required": True,
                 "note": "Select an agent, then repeat this query with its exact agent_guid.",
                 "query": arguments.query},
                next_call={"name": "tuoni_agents_list", "arguments": {}},
            )
        cache = cache_for(invocation.server)

        async def cached_fetch(path: str) -> Any:
            return await fetch(invocation, path)

        agent, templates = await cache.compatible_templates(cached_fetch, arguments.agent_guid)
        if agent is None:
            raise ToolFailure("AGENT_NOT_FOUND", "Agent was not found.", hint="Refresh the agent list.")
        intent_names = matched_templates(arguments.query)
        ranked = [(_template_rank(item, arguments.query, intent_names), item) for item in templates]
        ranked = [entry for entry in ranked if entry[0][0] > 0]
        ranked.sort(key=lambda entry: (-entry[0][0], entry[0][1], entry[0][2]))
        summaries = []
        for _, template in ranked:
            summaries.append({
                **_template_summary(template),
                "describe": exposed_call({
                    "name": "tuoni_command_describe",
                    "arguments": {
                        "agent_guid": arguments.agent_guid,
                        "template": _preferred_template_reference(template, templates),
                    },
                }),
            })

        start = 0
        if arguments.cursor:
            try:
                cursor = decode_cursor(arguments.cursor, tool="tuoni_commands_search")
            except ValueError as exc:
                raise ToolFailure(getattr(exc, "code", "INVALID_CURSOR"), str(exc), hint="Restart the search without a cursor.") from exc
            expected = {
                "agent_guid": arguments.agent_guid,
                "query": arguments.query,
                "limit": arguments.limit,
            }
            if any(cursor.get(key) != value for key, value in expected.items()):
                raise ToolFailure(
                    "CURSOR_SCOPE_CHANGED",
                    "Search scope changed while continuing a cursor.",
                    hint="Use the exact next call returned by the previous search page.",
                )
            try:
                start = cursor["offset"]
                if type(start) is not int or start < 0:
                    raise ValueError("invalid cursor offset")
            except (KeyError, TypeError, ValueError) as exc:
                raise ToolFailure("INVALID_CURSOR", "Search cursor has no valid offset.") from exc
        if not 0 <= start <= len(summaries):
            raise ToolFailure("INVALID_CURSOR", "Search cursor offset is outside the current catalog.")

        selected: list[dict[str, Any]] = []
        for index in range(start, min(start + arguments.limit, len(summaries))):
            candidate = [*selected, summaries[index]]
            next_index = index + 1
            cursor = (
                encode_cursor(
                    {
                        "tool": "tuoni_commands_search",
                        "offset": next_index,
                        "agent_guid": arguments.agent_guid,
                        "query": arguments.query,
                        "limit": arguments.limit,
                    }
                )
                if next_index < len(summaries)
                else None
            )
            next_call = (
                {
                    "name": "tuoni_commands_search",
                    "arguments": {
                        "agent_guid": arguments.agent_guid,
                        "query": arguments.query,
                        "limit": arguments.limit,
                        "cursor": cursor,
                    },
                }
                if cursor
                else None
            )
            data = {"agent_guid": arguments.agent_guid, "commands": candidate, "total_matches": len(summaries)}
            if not would_fit_success(
                data,
                next_cursor=cursor,
                next_call=next_call,
                budget=LIST_RESULT_BUDGET,
            ):
                break
            selected = candidate
        next_index = start + len(selected)
        next_cursor = (
            encode_cursor(
                {
                    "tool": "tuoni_commands_search",
                    "offset": next_index,
                    "agent_guid": arguments.agent_guid,
                    "query": arguments.query,
                    "limit": arguments.limit,
                }
            )
            if next_index < len(summaries)
            else None
        )
        next_call = (
            {
                "name": "tuoni_commands_search",
                "arguments": {
                    "agent_guid": arguments.agent_guid,
                    "query": arguments.query,
                    "limit": arguments.limit,
                    "cursor": next_cursor,
                },
            }
            if next_cursor
            else None
        )
        if not selected and start < len(summaries):
            raise ToolFailure("RESPONSE_TOO_LARGE", "One command summary exceeds the 4 KiB search budget.")
        return ToolSuccess(
            {"agent_guid": arguments.agent_guid, "commands": selected, "total_matches": len(summaries)},
            next_cursor=next_cursor,
            next_call=next_call,
        )

    async def command_describe(invocation: InvocationContext, model: ToolInput) -> ToolSuccess:
        arguments = cast(CommandDescribeInput, model)
        cache = cache_for(invocation.server)

        async def cached_fetch(path: str) -> Any:
            return await fetch(invocation, path)

        agent, matches, candidates = await _template_lookup(
            cache,
            cached_fetch,
            arguments.agent_guid,
            arguments.template,
        )
        if agent is None:
            raise ToolFailure("AGENT_NOT_FOUND", "Agent was not found.", hint="Refresh the agent list.")
        template = _exact_match(
            matches,
            arguments.template,
            candidates=candidates,
        )
        items = _describe_items(template)
        start = 0
        if arguments.cursor:
            try:
                cursor = decode_cursor(arguments.cursor, tool="tuoni_command_describe")
            except ValueError as exc:
                raise ToolFailure(getattr(exc, "code", "INVALID_CURSOR"), str(exc), hint="Restart describe without a cursor.") from exc
            if (
                cursor.get("agent_guid") != arguments.agent_guid
                or cursor.get("template_id") != template.get("id")
                or cursor.get("limit") != arguments.limit
            ):
                raise ToolFailure("CURSOR_SCOPE_CHANGED", "Describe scope changed while continuing a cursor.")
            try:
                start = cursor["offset"]
                if type(start) is not int or start < 0:
                    raise ValueError("invalid cursor offset")
            except (KeyError, TypeError, ValueError) as exc:
                raise ToolFailure("INVALID_CURSOR", "Describe cursor has no valid offset.") from exc
        if not 0 <= start <= len(items):
            raise ToolFailure("INVALID_CURSOR", "Describe cursor offset is outside the current schema.")

        execution_applicable = _execution_applicable(template)
        identity = {
            "id": bounded_text(template.get("id"), 128),
            "name": bounded_text(template.get("name"), 96),
            "preferred_reference": _preferred_template_reference(template, candidates),
            "plugin_id": bounded_text(template.get("pluginId"), 128),
            "scope": bounded_text(template.get("scope"), 64),
            "description": bounded_text(template.get("description"), 384),
            "execution_applicable": execution_applicable,
            "execution_modes": ["SELF", "NEW", "EXISTING"] if execution_applicable else [],
            "supported_exec_unit_types": [
                bounded_text(value, 48) for value in (template.get("supportedExecUnitTypes") or [])[:12]
            ],
            "field_path_format": "JSON Pointer; * denotes an array item",
        }
        example = None
        example_metadata: dict[str, Any] = {}
        if start == 0:
            if execution_applicable:
                example_metadata.update(
                    execution_examples=_execution_examples(),
                    execution_note="Copy one example into execution; replace executable or pid with the selected target's value.",
                )
            example, skeleton = _prepare_example(arguments.agent_guid, template, candidates)
            if skeleton.incomplete:
                example_metadata["prepare_example_incomplete"] = True
            if skeleton.notes:
                example_metadata["prepare_example_notes"] = list(skeleton.notes)
            preview = {"agent_guid": arguments.agent_guid, "template": identity, "fields": [],
                       "prepare_arguments_example": example, **example_metadata}
            if not would_fit_success(preview, budget=DETAIL_RESULT_BUDGET - 1024):
                example = None
                example_metadata.update({
                    "prepare_example_incomplete": True,
                    "prepare_example_notes": ["A complete example exceeds the budget; inspect all describe fields before preparing."],
                })
        selected: list[dict[str, Any]] = []
        for index in range(start, min(start + arguments.limit, len(items))):
            candidate = [*selected, items[index]]
            next_index = index + 1
            cursor = (
                encode_cursor(
                    {
                        "tool": "tuoni_command_describe",
                        "offset": next_index,
                        "agent_guid": arguments.agent_guid,
                        "template_id": template.get("id"),
                        "limit": arguments.limit,
                    }
                )
                if next_index < len(items)
                else None
            )
            next_call = (
                {
                    "name": "tuoni_command_describe",
                    "arguments": {
                        "agent_guid": arguments.agent_guid,
                        "template": _preferred_template_reference(template, candidates),
                        "limit": arguments.limit,
                        "cursor": cursor,
                    },
                }
                if cursor
                else None
            )
            data = {"agent_guid": arguments.agent_guid, "template": identity, "fields": candidate, **example_metadata}
            if example is not None:
                data["prepare_arguments_example"] = example
            if not would_fit_success(
                data,
                next_cursor=cursor,
                next_call=next_call,
                budget=DETAIL_RESULT_BUDGET,
            ):
                break
            selected = candidate
        next_index = start + len(selected)
        next_cursor = (
            encode_cursor(
                {
                    "tool": "tuoni_command_describe",
                    "offset": next_index,
                    "agent_guid": arguments.agent_guid,
                    "template_id": template.get("id"),
                    "limit": arguments.limit,
                }
            )
            if next_index < len(items)
            else None
        )
        next_call = (
            {
                "name": "tuoni_command_describe",
                "arguments": {
                    "agent_guid": arguments.agent_guid,
                    "template": _preferred_template_reference(template, candidates),
                    "limit": arguments.limit,
                    "cursor": next_cursor,
                },
            }
            if next_cursor
            else None
        )
        if not selected and start < len(items):
            raise ToolFailure("RESPONSE_TOO_LARGE", "One normalized schema field exceeds the describe budget.")
        data = {"agent_guid": arguments.agent_guid, "template": identity, "fields": selected, **example_metadata}
        if example is not None:
            data["prepare_arguments_example"] = example
        return ToolSuccess(data, next_cursor=next_cursor, next_call=next_call)

    async def command_prepare(invocation: InvocationContext, model: ToolInput) -> ToolSuccess:
        arguments = cast(CommandPrepareInput, model)
        cache = cache_for(invocation.server)

        async def cached_fetch(path: str) -> Any:
            return await fetch(invocation, path)

        agent, matches, candidates = await _template_lookup(
            cache,
            cached_fetch,
            arguments.agent_guid,
            arguments.template,
        )
        if agent is None:
            raise ToolFailure("AGENT_NOT_FOUND", "Agent was not found.", hint="Refresh the agent list.")
        if not agent.get("active") or str(agent.get("status") or "") == "BLOCKED":
            raise ToolFailure("AGENT_UNAVAILABLE", "The selected agent is not active.")
        template = _exact_match(
            matches,
            arguments.template,
            candidates=candidates,
        )
        template_id = template.get("id")
        if not isinstance(template_id, str) or not template_id:
            raise ToolFailure(
                "INVALID_TEMPLATE_IDENTITY",
                "The live command template has no executable ID.",
                hint="Refresh command search; ask the Tuoni administrator to repair the template if it persists.",
            )
        template_name = str(template.get("name") or "").casefold()
        if arguments.shell_command is not None:
            shell_field = _shell_command_field(template)
            if shell_field is None:
                raise ToolFailure(
                    "INVALID_ARGUMENTS",
                    f"Template {template_name!r} needs more than a command string.",
                    hint="Describe this template and send its exact configuration object instead.",
                )
            configuration = {shell_field: arguments.shell_command}
        else:
            assert arguments.configuration is not None
            configuration = arguments.configuration
        fields = {item.field for item in arguments.attachments}
        execution = (
            arguments.execution.model_dump(mode="python", by_alias=True, exclude_none=True)
            if arguments.execution is not None
            else None
        )
        _validate_configuration(template, configuration, fields)
        _validate_execution(template, execution)
        prepared_files = await _prepare_attachments(invocation, arguments.attachments)
        summary = {
            "agent_guid": arguments.agent_guid,
            "template": {
                "id": bounded_text(template_id, 128),
                "name": bounded_text(template.get("name"), 96),
            },
            "configuration_keys": sorted(configuration),
            "configuration_preview": _review_preview(configuration),
            "attachments": [item.summary() for item in prepared_files],
            "execution": redact(execution),
        }
        return await _create_preparation(
            invocation,
            "command.create",
            payload={
                "agent_guid": arguments.agent_guid,
                "template_id": template_id,
                "configuration": configuration,
                "execution": execution,
            },
            attachments=prepared_files,
            summary=summary,
            state_fingerprint=template_fingerprint(template),
        )

    async def command_update_prepare(invocation: InvocationContext, model: ToolInput) -> ToolSuccess:
        arguments = cast(CommandUpdatePrepareInput, model)
        raw = await _read(invocation, f"/api/v1/commands/{arguments.command_id}")
        if not isinstance(raw, Mapping):
            raise ToolFailure("COMMAND_NOT_FOUND", "Command was not found.")
        status = str(raw.get("status") or "")
        if status not in _UPDATABLE_STATUSES:
            raise ToolFailure(
                "COMMAND_NOT_UPDATABLE",
                f"Command status {status or 'UNKNOWN'} does not accept interactive updates.",
            )
        template_id = str(raw.get("commandTemplateId") or "")
        cache = cache_for(invocation.server)

        async def cached_fetch(path: str) -> Any:
            return await fetch(invocation, path)

        templates = await cache.global_templates(cached_fetch)
        matches = [
            item
            for item in templates
            if template_id.casefold() in template_identifiers(item)
        ]
        template = _exact_match(matches, template_id)
        prepared_files = await _prepare_attachments(invocation, arguments.attachments)
        fields = {item.field for item in prepared_files}
        _validate_configuration(template, arguments.configuration, fields, update=True)
        summary = {
            "command_id": arguments.command_id,
            "current_status": status,
            "template": {
                "id": bounded_text(template.get("id"), 128),
                "name": bounded_text(template.get("name"), 96),
            },
            "configuration_keys": sorted(arguments.configuration),
            "configuration_preview": _review_preview(arguments.configuration),
            "attachments": [item.summary() for item in prepared_files],
            "persistence_type": arguments.persistence_type,
        }
        return await _create_preparation(
            invocation,
            "command.update",
            payload={
                "command_id": arguments.command_id,
                "template_id": template_id,
                "configuration": arguments.configuration,
                "persistence_type": arguments.persistence_type,
            },
            attachments=prepared_files,
            summary=summary,
            state_fingerprint=template_fingerprint(template),
        )

    async def command_stop_prepare(invocation: InvocationContext, model: ToolInput) -> ToolSuccess:
        arguments = cast(CommandStopPrepareInput, model)
        raw = await _read(invocation, f"/api/v1/commands/{arguments.command_id}")
        if not isinstance(raw, Mapping):
            raise ToolFailure("COMMAND_NOT_FOUND", "Command was not found.")
        status = str(raw.get("status") or "")
        if status in _TERMINAL_STATUSES:
            raise ToolFailure("COMMAND_ALREADY_TERMINAL", f"Command is already {status}.")
        return await _create_preparation(
            invocation,
            "command.stop",
            payload={"command_id": arguments.command_id},
            summary={"command_id": arguments.command_id, "current_status": status},
        )

    async def operation_execute(invocation: InvocationContext, model: ToolInput) -> Any:
        arguments = cast(OperationExecuteInput, model)
        store = _store_for(invocation.server)
        not_found = ToolFailure(
            "PREPARATION_NOT_FOUND",
            "Preparation does not exist or has expired.",
            hint="If execution was attempted, inspect server state and do not resubmit. Otherwise prepare again. Restarts invalidate IDs.",
        )
        record = await store.get(arguments.preparation_id)
        if record is None:
            raise not_found
        async with record.lock:
            if record.state == "executed":
                next_call = None
                if record.receipt and record.receipt.get("command_id") is not None:
                    next_call = {
                        "name": "tuoni_command_get",
                        "arguments": {"command_id": record.receipt["command_id"]},
                    }
                return ToolSuccess(record.receipt or {}, next_call=next_call)
            if record.state in {"ambiguous", "rejected"}:
                error = record.ambiguous_error or {}
                raise ToolFailure(
                    str(error.get("code") or "AMBIGUOUS_OUTCOME"),
                    str(error.get("message") or "The operation may have reached Tuoni."),
                    hint=str(error.get("hint") or "Inspect current server state. Never execute this preparation again."),
                    next_call=error.get("next"), outcome=error.get("outcome"),
                )
            if record.state == "executing":
                error = {
                    "message": "A previous execution attempt did not record a definite outcome."
                }
                await _finish_state(store.mark_ambiguous(record, error))
                raise ToolFailure(
                    "AMBIGUOUS_OUTCOME",
                    error["message"],
                    hint="Inspect current server state. Never execute this preparation again.",
                )
            try:
                await store.begin(record)
            except KeyError as exc:
                # Waiting for another caller's execution lock can cross the
                # preparation deadline after get() found a still-live record.
                raise not_found from exc
            with mutation_scope(MutationJournal()) as journal:
                mutation_started = False
                try:
                    if record.kind == "command.create":
                        cache = cache_for(invocation.server)

                        async def fresh_fetch(path: str) -> Any:
                            return await fetch(invocation, path)

                        agent, matches, candidates = await _template_lookup(
                            cache,
                            fresh_fetch,
                            str(record.payload["agent_guid"]),
                            str(record.payload["template_id"]),
                            force=True,
                        )
                        if agent is None or not agent.get("active") or str(agent.get("status") or "") == "BLOCKED":
                            raise ToolFailure("PREPARATION_DRIFT", "Agent is no longer active.")
                        template = _exact_match(
                            matches,
                            str(record.payload["template_id"]),
                            candidates=candidates,
                        )
                        if template_fingerprint(template) != record.state_fingerprint:
                            raise ToolFailure("PREPARATION_DRIFT", "Command template changed after preparation.")
                        fields = {item.field for item in record.attachments}
                        _validate_configuration(template, record.payload["configuration"], fields)
                        _validate_execution(template, record.payload.get("execution"))
                        files = await _materialize_attachments(invocation, record.attachments)
                        body = {
                            "template": record.payload["template_id"],
                            "configuration": record.payload["configuration"],
                        }
                        if record.payload.get("execution") is not None:
                            body["execConf"] = record.payload["execution"]
                        try:
                            await ensure_fresh_token(invocation.server)
                        except Exception as exc:
                            raise ToolFailure("AUTHENTICATION_FAILED", "Could not authenticate before execution.") from exc
                        mutation_started = True
                        raw = await call_without_retry(
                            invocation.server,
                            invocation.server.c2.request_post,
                            f"/api/v1/agents/{url_path_segment(record.payload['agent_guid'])}/commands",
                            body,
                            files,
                        )
                        if not isinstance(raw, Mapping) or type(raw.get("id")) is not int or raw["id"] < 0:
                            raise RuntimeError("Tuoni did not return a valid integer command id after submission")
                        receipt = {"operation": record.kind, **_command_receipt(raw)}
                    elif record.kind == "command.update":
                        raw_command = await _read(invocation, f"/api/v1/commands/{record.payload['command_id']}")
                        if not isinstance(raw_command, Mapping) or str(raw_command.get("status") or "") not in _UPDATABLE_STATUSES:
                            raise ToolFailure("PREPARATION_DRIFT", "Command no longer accepts updates.")
                        cache = cache_for(invocation.server)

                        async def fresh_fetch(path: str) -> Any:
                            return await fetch(invocation, path)

                        templates = await cache.global_templates(fresh_fetch, force=True)
                        matches = [
                            item
                            for item in templates
                            if str(record.payload["template_id"]).casefold() in template_identifiers(item)
                        ]
                        template = _exact_match(matches, str(record.payload["template_id"]))
                        if template_fingerprint(template) != record.state_fingerprint:
                            raise ToolFailure("PREPARATION_DRIFT", "Command update schema changed after preparation.")
                        fields = {item.field for item in record.attachments}
                        _validate_configuration(template, record.payload["configuration"], fields, update=True)
                        files = await _materialize_attachments(invocation, record.attachments)
                        body = {
                            "updateConfiguration": record.payload["configuration"],
                            "configurationFiles": [],
                            "persistenceType": record.payload["persistence_type"],
                        }
                        try:
                            await ensure_fresh_token(invocation.server)
                        except Exception as exc:
                            raise ToolFailure("AUTHENTICATION_FAILED", "Could not authenticate before execution.") from exc
                        mutation_started = True
                        await call_without_retry(
                            invocation.server,
                            invocation.server.c2.request_post,
                            f"/api/v1/commands/{record.payload['command_id']}/update",
                            body,
                            files,
                        )
                        receipt = {
                            "operation": record.kind,
                            "command_id": record.payload["command_id"],
                            "updated": True,
                        }
                    elif record.kind == "command.stop":
                        raw_command = await _read(invocation, f"/api/v1/commands/{record.payload['command_id']}")
                        if not isinstance(raw_command, Mapping):
                            raise ToolFailure("PREPARATION_DRIFT", "Command no longer exists.")
                        if str(raw_command.get("status") or "") in _TERMINAL_STATUSES:
                            raise ToolFailure("PREPARATION_DRIFT", "Command became terminal after preparation.")
                        try:
                            await ensure_fresh_token(invocation.server)
                        except Exception as exc:
                            raise ToolFailure("AUTHENTICATION_FAILED", "Could not authenticate before execution.") from exc
                        mutation_started = True
                        await call_without_retry(
                            invocation.server,
                            invocation.server.c2.request_put,
                            f"/api/v1/commands/{record.payload['command_id']}/stop",
                        )
                        receipt = {
                            "operation": record.kind,
                            "command_id": record.payload["command_id"],
                            "stopped": True,
                        }
                    else:
                        spec = registry.get(str(record.payload.get("tool_name") or ""))
                        if spec is None or spec.effect is not Effect.MUTATE:
                            raise ToolFailure("PREPARATION_DRIFT", "Prepared tool is no longer available.")
                        validated = spec.input_model.model_validate(record.payload.get("arguments") or {})
                        compatibility_context = nullcontext()
                        if spec.name == "tuoni_payload_create":
                            from tuoni_mcp.tools.payloads import (
                                check_payload_listener_compatibility,
                                payload_compatibility_prechecked,
                            )

                            await check_payload_listener_compatibility(
                                invocation.context, validated.template, validated.listener_id,
                            )
                            compatibility_context = payload_compatibility_prechecked(
                                invocation.context, validated.template, validated.listener_id,
                            )
                        async with prepared_upload(invocation.server.cfg, spec.name, record.payload["arguments"],
                                                   record.payload.get("upload_source")):
                            try:
                                await ensure_fresh_token(invocation.server)
                            except Exception as exc:
                                raise ToolFailure("AUTHENTICATION_FAILED", "Could not authenticate before execution.") from exc
                            mutation_started = True
                            with compatibility_context, mutation_once_only():
                                value = await spec.handler(invocation, validated)
                        if isinstance(value, CallToolResult) and value.isError:
                            raise RuntimeError("Prepared tool returned an MCP error after execution began")
                        if isinstance(value, Mapping) and value.get("error"):
                            raise RuntimeError("Prepared tool returned an error after execution began")
                        receipt = {
                            "operation": record.kind,
                            "tool_name": spec.name,
                            "result": _normalize_generic_result(value),
                        }
                except BaseException as exc:
                    # A cancelled worker may finish a read later. Closing the
                    # journal prevents it from starting another HTTP write.
                    journal.close()
                    if mutation_started:
                        outcome = journal.snapshot(started=getattr(invocation.server.c2, "observes_mutations", False) is not True)
                        state = outcome["state"]
                        if state == "not_started":
                            await _finish_state(store.restore_prepared(record))
                            raise
                        if state == "rejected":
                            classified = classify_rejection(exc, tool_name="tuoni_operation_execute", outcome=outcome)
                            code = exc.code if isinstance(exc, ToolFailure) else classified.code
                            message = exc.message if isinstance(exc, ToolFailure) else classified.message
                            hint = "The write was rejected. Inspect permissions or current state before preparing new work."
                        elif state == "partial":
                            code = "PARTIAL_OUTCOME"
                            message = "Earlier writes were accepted; the operation did not finish."
                            hint = "Inspect current state and reconcile accepted writes. Never execute this preparation again."
                        else:
                            code = "AMBIGUOUS_OUTCOME"
                            message = "The operation may have reached Tuoni, but no definite receipt was returned."
                            hint = "Inspect current server state. Never execute this preparation again."
                        inspection = _execution_inspection(registry, invocation, record)
                        error = {"code": code, "message": message, "hint": hint,
                                 "outcome": outcome, "next": inspection}
                        await _finish_state(store.mark_ambiguous(record, error, rejected=state == "rejected"))
                        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                            raise
                        raise ToolFailure(code, message, hint=hint, next_call=inspection, outcome=outcome) from exc
                    await _finish_state(store.restore_prepared(record))
                    raise
                receipt["outcome"] = journal.snapshot(succeeded=True)
                await _finish_state(store.complete(record, receipt))
                next_call = None
                if receipt.get("command_id") is not None:
                    next_call = {
                        "name": "tuoni_command_get",
                        "arguments": {"command_id": receipt["command_id"]},
                    }
                return ToolSuccess(receipt, next_call=next_call)

    async def command_file_download(invocation: InvocationContext, model: ToolInput) -> ToolSuccess:
        arguments = cast(CommandFileDownloadInput, model)
        download_dir = invocation.server.cfg.download_dir
        if download_dir is None:
            raise ToolFailure(
                "DOWNLOAD_DIRECTORY_NOT_CONFIGURED",
                "Local result-file downloads are not configured.",
                hint="Set TUONI_MCP_DOWNLOAD_DIR to an absolute bind-mounted directory.",
            )
        raw = await _read(invocation, f"/api/v1/commands/{arguments.command_id}")
        if not isinstance(raw, Mapping):
            raise ToolFailure("COMMAND_NOT_FOUND", "Command was not found.")
        status = str(raw.get("status") or "").upper()
        if status not in _TERMINAL_STATUSES:
            raise ToolFailure(
                "COMMAND_NOT_COMPLETE",
                f"Command status is {status or 'UNKNOWN'}; no result file is available yet.",
                retryable=True,
                hint="Poll command status, then copy a file part_index from the completed manifest.",
            )
        result = raw.get("result")
        parts = result.get("childResults") if isinstance(result, Mapping) else None
        if not isinstance(parts, list):
            raise ToolFailure("COMMAND_RESULT_UNAVAILABLE", "The command has no result parts.")
        if arguments.part_index >= len(parts):
            raise ToolFailure(
                "INVALID_PART_INDEX",
                f"part_index {arguments.part_index} is outside {len(parts)} result parts.",
            )
        part = parts[arguments.part_index]
        if not isinstance(part, Mapping) or part.get("type") != "file":
            raise ToolFailure(
                "RESULT_PART_NOT_FILE",
                "The selected result part is not a downloadable file.",
                hint="Return to the manifest and copy a part_index whose type is file.",
            )
        href = part.get("href")
        if not isinstance(href, str) or not href:
            raise ToolFailure("FILE_DOWNLOAD_UNAVAILABLE", "The selected file part has no download endpoint.")
        content = await call(
            invocation.context,
            invocation.server.c2.request_get,
            _download_path(href),
            False,
            True,
        )
        if not isinstance(content, (bytes, bytearray)):
            raise ToolFailure("FILE_DOWNLOAD_FAILED", "The result endpoint did not return file bytes.")
        body = bytes(content)
        if len(body) > invocation.server.cfg.download_max_bytes:
            raise ToolFailure(
                "FILE_TOO_LARGE",
                "The result file exceeds the configured local download limit.",
                hint="Raise TUONI_MCP_DOWNLOAD_MAX_BYTES deliberately, then retry once.",
            )
        filename = _safe_result_filename(
            part.get("filename"),
            arguments.command_id,
            arguments.part_index,
        )
        locks = getattr(invocation.server, "result_download_locks", None)
        if locks is None:
            locks = weakref.WeakValueDictionary()
            invocation.server.result_download_locks = locks
        key = (arguments.command_id, arguments.part_index)
        lock = locks.setdefault(key, asyncio.Lock())
        # File writes run off the event loop. Serialize writes to the same part
        # so another request cannot inspect a newly created but incomplete file.
        async with lock:
            destination, digest, already_present = await to_thread(
                _save_result_download,
                Path(download_dir),
                command_id=arguments.command_id,
                part_index=arguments.part_index,
                filename=filename,
                content=body,
            )
        media_type, _ = mimetypes.guess_type(filename)
        return ToolSuccess(
            {
                "command_id": arguments.command_id,
                "part_index": arguments.part_index,
                "filename": filename,
                "local_path": str(destination),
                "size_bytes": len(body),
                "sha256": digest,
                "media_type": media_type or "application/octet-stream",
                "already_present": already_present,
                "view_with": "OpenCode read tool using the exact local_path.",
            }
        )

    async def command_get(invocation: InvocationContext, model: ToolInput) -> ToolSuccess | CallToolResult:
        arguments = cast(CommandGetInput, model)
        explicit = arguments.model_fields_set
        if arguments.wait_seconds and arguments.part_index is not None:
            raise ToolFailure(
                "INVALID_ARGUMENTS", "wait_seconds is only valid for status or manifest reads.",
                hint="Wait without part_index, then copy a returned text-window continuation.",
            )
        if arguments.part_index is None:
            invalid = sorted(explicit.intersection({"offset_chars", "max_chars", "result_version"}))
            if invalid:
                raise ToolFailure(
                    "INVALID_ARGUMENTS",
                    "Text-window arguments require part_index.",
                    hint=(
                        "Do not repeat this call. First get the completed manifest, then copy one "
                        "result.parts[].part_index and call the same tool with part_index, "
                        "offset_chars=0, and max_chars=4096."
                    ),
                )
        else:
            invalid = sorted(explicit.intersection({"limit"}))
            if "cursor" in explicit and arguments.cursor is not None:
                invalid.append("cursor")
            if invalid:
                raise ToolFailure(
                    "INVALID_ARGUMENTS",
                    "Manifest pagination arguments cannot be used with part_index.",
                    hint=(
                        "Do not repeat this call. To read text, send only command_id, part_index, "
                        "offset_chars, and max_chars."
                    ),
                )
        followup = {
            "name": "tuoni_command_get",
            "arguments": {"command_id": arguments.command_id},
        }

        def result_changed() -> ToolFailure:
            return ToolFailure(
                "RESULT_CHANGED", "The selected result part changed while reading its text windows.",
                hint="Discard earlier windows and follow next to read a fresh manifest; keep this command ID.",
                next_call=followup,
            )

        async def fetch():
            return await result_cache_for(invocation.server).get(
                arguments.command_id,
                lambda: _read(invocation, f"/api/v1/commands/{arguments.command_id}"),
            )

        async def fetch_next_callback(raw_command: Mapping[str, Any]) -> Any:
            agent_guid = raw_command.get("agentGuid")
            if not isinstance(agent_guid, str) or not 1 <= len(agent_guid) <= 256:
                return None

            async def read_agent(path: str) -> Any:
                return await _read(invocation, path)

            return await cache_for(invocation.server).agent(read_agent, agent_guid)

        try:
            observation = await read_until_terminal(
                fetch, arguments.wait_seconds, fetch_next_callback=fetch_next_callback,
            )
        except Exception as exc:
            if not arguments.wait_seconds:
                raise
            if isinstance(exc, InitialReadTimeout):
                raise ToolFailure(
                    "RESULT_WAIT_TIMEOUT", f"Wait expired before command {arguments.command_id} was read.",
                    retryable=True, hint="Read this same command ID again; do not submit a replacement.",
                    next_call=followup,
                ) from exc
            failure = classify(exc, tool_name="tuoni_command_get", safe_to_retry=True)
            raise ToolFailure(
                failure.code, f"Command {arguments.command_id}: {failure.message}",
                retryable=failure.retryable, hint=failure.hint, next_call=followup,
            ) from exc
        raw = observation.value
        if not isinstance(raw, Mapping):
            raise ToolFailure(
                "COMMAND_NOT_FOUND", f"Command {arguments.command_id} was not found.",
                hint="Use tuoni_commands_list to select an existing command ID.",
            )
        command = {
            "command_id": raw.get("id", arguments.command_id),
            "template_id": bounded_text(raw.get("commandTemplateId"), 128),
            "template_name": bounded_text(raw.get("commandTemplateName"), 96),
            "status": bounded_text(raw.get("status"), 32),
            "created": bounded_text(raw.get("created"), 64),
            "sent": bounded_text(raw.get("sent"), 64),
        }
        command = {key: value for key, value in command.items() if value not in (None, "")}
        if observation.timed_out:
            command["wait_timed_out"] = True
        poll_call = None
        if str(raw.get("status") or "").upper() not in _TERMINAL_STATUSES:
            hint = observation.poll_after_seconds
            agent_guid = raw.get("agentGuid")
            if hint is None and str(raw.get("status") or "").upper() in {"CREATED", "SENT"} and isinstance(agent_guid, str):
                hint = callback_poll_seconds(cache_for(invocation.server).cached_agent(agent_guid))
            command["poll_after_seconds"] = hint or 1
            poll_call = {
                "name": "tuoni_command_get",
                "arguments": {"command_id": arguments.command_id},
            }
            if arguments.wait_seconds:
                poll_call["arguments"]["wait_seconds"] = arguments.wait_seconds
        if raw.get("errorMessage"):
            command["error_message"] = bounded_text(raw.get("errorMessage"), 512)
        result = raw.get("result")
        if not isinstance(result, Mapping):
            if arguments.result_version is not None:
                raise result_changed()
            return ToolSuccess({**command, "result": None}, next_call=poll_call)
        parts = result.get("childResults")
        if not isinstance(parts, list):
            parts = []
        result_base = {
            "status": bounded_text(result.get("status"), 32),
            "error_message": bounded_text(result.get("errorMessage"), 512),
            "received": bounded_text(result.get("received"), 64),
            "part_count": len(parts),
        }
        result_base = {key: value for key, value in result_base.items() if value not in (None, "")}

        if arguments.part_index is not None:
            if arguments.cursor is not None:
                raise ToolFailure("INVALID_ARGUMENTS", "cursor is only valid for manifest pages.")
            if arguments.part_index >= len(parts):
                if arguments.result_version is not None:
                    raise result_changed()
                raise ToolFailure(
                    "INVALID_PART_INDEX",
                    f"part_index {arguments.part_index} is outside {len(parts)} result parts.",
                )
            part = parts[arguments.part_index]
            if not isinstance(part, Mapping):
                if arguments.result_version is not None:
                    raise result_changed()
                raise ToolFailure("INVALID_RESULT_PART", "Tuoni returned an invalid result part.")
            version = _text_version(part, arguments.part_index, arguments.command_id) if part.get("type") == "text" else None
            if arguments.result_version is not None and arguments.result_version != version:
                raise result_changed()
            if part.get("type") != "text":
                return ToolSuccess(
                    {
                        **command,
                        "result": {
                            **result_base,
                            "part": _part_summary(
                                part,
                                arguments.part_index,
                                command_id=arguments.command_id,
                                download_enabled=result_download_enabled,
                                dispatch=dispatch_mode,
                            ),
                        },
                    },
                    next_call=poll_call,
                )
            # Sanitize the complete part before applying offsets. Redacting each
            # returned window independently would let a caller split a secret
            # marker across tiny requests and reconstruct the original value.
            value = result_text(part.get("value"))
            if arguments.offset_chars > len(value):
                raise ToolFailure(
                    "INVALID_OFFSET",
                    f"offset_chars {arguments.offset_chars} exceeds the text length {len(value)}.",
                    hint="Restart at offset_chars 0 or use next_offset_chars from the previous response.",
                )

            def make_payload(text: str, end: int, has_more: bool) -> dict[str, Any]:
                next_call = (
                    {
                        "name": "tuoni_command_get",
                        "arguments": {
                            "command_id": arguments.command_id,
                            "part_index": arguments.part_index,
                            "result_version": version,
                            "offset_chars": end,
                            "max_chars": arguments.max_chars,
                        },
                    }
                    if has_more
                    else poll_call
                )
                data = {
                    **command,
                    "result": {
                        **result_base,
                        "part": {
                            "part_index": arguments.part_index,
                            "type": "text",
                            "name": bounded_text(part.get("name"), 128),
                            "result_version": version,
                            "offset_chars": arguments.offset_chars,
                            "returned_chars": end - arguments.offset_chars,
                            "total_chars": len(value),
                            "value": text,
                            "has_more": has_more,
                            "next_offset_chars": end if has_more else None,
                        },
                    },
                }
                # The registry redacts the complete success envelope before its
                # final byte check. Measure that exact representation here too:
                # textual redaction can expand a value (for example body=x), so
                # fitting the raw text could otherwise yield RESPONSE_TOO_LARGE.
                return success_payload(
                    redact_result(data),
                    next_call=redact_result(next_call),
                )

            try:
                _, end, has_more, _ = fit_text_window(
                    value,
                    offset_chars=arguments.offset_chars,
                    requested_chars=arguments.max_chars,
                    budget=MAX_RESULT_BUDGET,
                    make_payload=make_payload,
                )
            except ValueError as exc:
                raise ToolFailure(
                    "RESPONSE_TOO_LARGE",
                    "Fixed command-result metadata exceeds the 8 KiB result budget.",
                    hint="Inspect the body-free manifest or select a different result part.",
                ) from exc
            text = value[arguments.offset_chars:end]
            next_call = (
                {
                    "name": "tuoni_command_get",
                    "arguments": {
                        "command_id": arguments.command_id,
                        "part_index": arguments.part_index,
                        "result_version": version,
                        "offset_chars": end,
                        "max_chars": arguments.max_chars,
                    },
                }
                if has_more
                else poll_call
            )
            data = {
                **command,
                "result": {
                    **result_base,
                    "part": {
                        "part_index": arguments.part_index,
                        "type": "text",
                        "name": bounded_text(part.get("name"), 128),
                        "result_version": version,
                        "offset_chars": arguments.offset_chars,
                        "returned_chars": end - arguments.offset_chars,
                        "total_chars": len(value),
                        "value": text,
                        "has_more": has_more,
                        "next_offset_chars": end if has_more else None,
                    },
                },
            }
            return ToolSuccess(data, next_call=next_call)

        start = 0
        if arguments.cursor:
            try:
                cursor = decode_cursor(arguments.cursor, tool="tuoni_command_get")
            except ValueError as exc:
                raise ToolFailure(getattr(exc, "code", "INVALID_CURSOR"), str(exc), hint="Restart the manifest without a cursor.") from exc
            if cursor.get("command_id") != arguments.command_id or cursor.get("limit") != arguments.limit:
                raise ToolFailure("CURSOR_SCOPE_CHANGED", "Manifest scope changed while continuing a cursor.")
            try:
                start = cursor["offset"]
                if type(start) is not int or start < 0:
                    raise ValueError("invalid cursor offset")
            except (KeyError, TypeError, ValueError) as exc:
                raise ToolFailure("INVALID_CURSOR", "Manifest cursor has no valid offset.") from exc
        summaries = [
            _part_summary(
                part,
                index,
                command_id=arguments.command_id,
                download_enabled=result_download_enabled,
                dispatch=dispatch_mode,
            )
            for index, part in enumerate(parts)
            if isinstance(part, Mapping)
        ]
        if not 0 <= start <= len(summaries):
            raise ToolFailure("INVALID_CURSOR", "Manifest cursor offset is outside the result.")
        selected: list[dict[str, Any]] = []
        for index in range(start, min(start + arguments.limit, len(summaries))):
            candidate = [*selected, summaries[index]]
            next_index = index + 1
            cursor = (
                encode_cursor(
                    {
                        "tool": "tuoni_command_get",
                        "command_id": arguments.command_id,
                        "offset": next_index,
                        "limit": arguments.limit,
                    }
                )
                if next_index < len(summaries)
                else None
            )
            next_call = (
                {
                    "name": "tuoni_command_get",
                    "arguments": {
                        "command_id": arguments.command_id,
                        "limit": arguments.limit,
                        "cursor": cursor,
                    },
                }
                if cursor
                else (
                    poll_call
                    or _first_result_followup_call(
                        arguments.command_id,
                        summaries,
                        download_enabled=result_download_enabled,
                    )
                )
            )
            data = {**command, "result": {**result_base, "parts": candidate}}
            if not would_fit_success(
                data,
                next_cursor=cursor,
                next_call=next_call,
                budget=MAX_RESULT_BUDGET,
            ):
                break
            selected = candidate
        next_index = start + len(selected)
        next_cursor = (
            encode_cursor(
                {
                    "tool": "tuoni_command_get",
                    "command_id": arguments.command_id,
                    "offset": next_index,
                    "limit": arguments.limit,
                }
            )
            if next_index < len(summaries)
            else None
        )
        next_call = (
            {
                "name": "tuoni_command_get",
                "arguments": {"command_id": arguments.command_id, "limit": arguments.limit, "cursor": next_cursor},
            }
            if next_cursor
            else (
                poll_call
                or _first_result_followup_call(
                    arguments.command_id,
                    summaries,
                    download_enabled=result_download_enabled,
                )
            )
        )
        if not selected and start < len(summaries):
            raise ToolFailure("RESPONSE_TOO_LARGE", "One result-part summary exceeds the manifest budget.")
        return ToolSuccess(
            {**command, "result": {**result_base, "parts": selected}},
            next_cursor=next_cursor,
            next_call=next_call,
        )

    agent_ops = frozenset({Profile.AGENT_OPS})
    specs = (
        ToolSpec(
            name="tuoni_commands_search",
            description=(
                "Find an agent's command templates by intent or exact name. "
                "Omit agent_guid to select an agent first."
            ),
            input_model=CommandsSearchInput,
            handler=commands_search,
            profiles=agent_ops,
            effect=Effect.READ,
            budget=LIST_RESULT_BUDGET,
            usage_note="Returns short names; describe one before preparing it.",
            keywords=(
                "commands", "templates", "search", "list", "shell", "identity", "whoami",
                "process", "enumerate", "capabilities", "available", "supported", "run",
                "what can this agent do",
            ),
        ),
        ToolSpec(
            name="tuoni_command_describe",
            description=(
                "Describe a template returned by commands_search, including fields and a complete prepare example."
            ),
            input_model=CommandDescribeInput,
            handler=command_describe,
            profiles=agent_ops,
            effect=Effect.READ,
            budget=DETAIL_RESULT_BUDGET,
            usage_note="Copy one returned name or ID into template; MCP tool names are not command templates.",
            keywords=("command", "schema", "arguments", "attachments"),
        ),
        ToolSpec(
            name="tuoni_command_prepare",
            description=(
                "Validate a command and hold it. Nothing reaches the agent until you execute it."
            ),
            input_model=CommandPrepareInput,
            handler=command_prepare,
            profiles=agent_ops,
            effect=Effect.PREPARE,
            budget=DETAIL_RESULT_BUDGET,
            usage_note=(
                "Copy every prepare_arguments_example field, including template. Fill shell_command "
                "if shown; otherwise use its exact configuration fields. Execute the returned ID once."
            ),
            validation_hint=(
                "Copy prepare_arguments_example; provide exactly one of shell_command or configuration."
            ),
            keywords=("command", "prepare", "validate", "send"),
        ),
        ToolSpec(
            name="tuoni_command_update_prepare",
            description=(
                "Validate an update for a command that is still running, such as more stdin."
            ),
            input_model=CommandUpdatePrepareInput,
            handler=command_update_prepare,
            profiles=agent_ops,
            effect=Effect.PREPARE,
            budget=DETAIL_RESULT_BUDGET,
            usage_note="Nothing is sent until you execute the returned preparation id.",
            keywords=("command", "update", "stdin", "interactive", "input", "respond"),
        ),
        ToolSpec(
            name="tuoni_command_stop_prepare",
            description="Validate stopping or cancelling one command that has not finished.",
            input_model=CommandStopPrepareInput,
            handler=command_stop_prepare,
            profiles=agent_ops,
            effect=Effect.PREPARE,
            budget=DETAIL_RESULT_BUDGET,
            usage_note="Nothing changes until you execute the returned preparation id.",
            keywords=("command", "stop", "cancel", "abort", "kill command"),
        ),
        ToolSpec(
            name="tuoni_operation_execute",
            description="Execute one reviewed operation using its complete opaque preparation ID exactly once.",
            input_model=OperationExecuteInput,
            handler=operation_execute,
            profiles=agent_ops,
            effect=Effect.EXECUTE,
            budget=MAX_RESULT_BUDGET,
            usage_note="Receipt replay is safe; never create a second command after an ambiguous outcome.",
            keywords=("execute", "preparation", "mutation"),
        ),
        ToolSpec(
            name="tuoni_command_get",
            description="Poll a command's status, list its result parts, or read one text window.",
            input_model=CommandGetInput,
            handler=command_get,
            profiles=agent_ops,
            effect=Effect.READ,
            budget=MAX_RESULT_BUDGET,
            usage_note=(
                "Omit part_index for status and the part list. Add part_index with offset_chars "
                "and max_chars to read text. Follow next exactly."
            ),
            keywords=(
                "command", "result", "status", "output", "poll", "read", "wait", "finished",
            ),
        ),
    )
    for spec in specs:
        registry.add(spec)
    registry.add(
        ToolSpec(
            name="tuoni_command_file_download",
            description="Save one command-result file to the configured local download directory.",
            input_model=CommandFileDownloadInput,
            handler=command_file_download,
            profiles=frozenset({Profile.ARTIFACTS}),
            effect=Effect.READ,
            budget=DETAIL_RESULT_BUDGET,
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
            usage_note="Copy command_id and file part_index from command_get; then read local_path.",
            keywords=("command", "result", "file", "download", "save", "view"),
        )
    )
    registry.preparation_backend = _GenericMutationBackend(registry)
