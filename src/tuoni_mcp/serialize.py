"""Field selection for the optional tool profiles.

The default agent-ops tools build their own bounded summaries in
``tools/agents.py`` and ``tools/commands.py``. These helpers serve the additive
profiles (access, artifacts, discovery, administration, authoring), which still
hand a whole record to the registry's byte-budgeted envelope.
"""

from __future__ import annotations

from typing import Any

from tuoni import (
    TuoniAlias,
    TuoniCommandPlugin,
    TuoniCommandTemplate,
    TuoniDataCredential,
    TuoniDataHost,
    TuoniDataService,
    TuoniFile,
    TuoniJob,
    TuoniListener,
    TuoniListenerPlugin,
    TuoniPayload,
    TuoniPayloadPlugin,
    TuoniUser,
)


def listener_to_dict(lst: TuoniListener) -> dict[str, Any]:
    return {
        "listener_id": lst.listener_id,
        "name": lst.name,
        "info": lst.info,
        "status": lst.status,
        "plugin": lst.plugin,
        "configuration": lst.configuration,
        "configuration_files": getattr(lst, "configuration_files", []),
    }


def listener_plugin_to_dict(p: TuoniListenerPlugin) -> dict[str, Any]:
    return {
        "plugin_id": p.plugin_id,
        "name": p.name,
        "vendor": p.vendor,
        "description": p.description,
        "configuration_schema": p.conf_schema,
        "configuration_examples": p.conf_examples,
    }


def payload_to_dict(p: TuoniPayload) -> dict[str, Any]:
    return {
        "payload_id": p.payload_id,
        "name": p.name,
        "template_id": p.template_id,
        "configuration": p.configuration,
        "configuration_files": p.configuration_files,
        "listeners": getattr(p, "listeners", []),
        "os": getattr(p, "os", None),
        "architecture": getattr(p, "architecture", None),
        "status": getattr(p, "status", None),
        "encrypted_communication": p.encrypted_communication,
    }


def payload_plugin_to_dict(p: TuoniPayloadPlugin) -> dict[str, Any]:
    return {
        "plugin_id": p.plugin_id,
        "name": p.name,
        "vendor": p.vendor,
        "description": p.description,
        "templates": [payload_template_to_dict(t) for t in p.templates],
    }


def payload_template_to_dict(t: Any) -> dict[str, Any]:
    return {
        "id": getattr(t, "id", None),
        "name": getattr(t, "name", None),
        "configuration_schema": getattr(t, "conf_schema", None),
        "configuration_examples": getattr(t, "conf_examples", {}),
    }


def command_template_to_dict(t: TuoniCommandTemplate) -> dict[str, Any]:
    return {
        "id": t.id,
        "name": t.name,
        "plugin_id": t.plugin_id,
        "scope": t.scope,
        "qualified_name": t.qualifiedName,
        "fully_qualified_name": t.fullyQualifiedName,
        "description": t.description,
        "configuration_schema": t.conf_schema,
        "configuration_examples": t.conf_examples,
    }


def command_plugin_to_dict(p: TuoniCommandPlugin) -> dict[str, Any]:
    return {
        "plugin_id": p.plugin_id,
        "name": p.name,
        "vendor": p.vendor,
        "description": p.description,
        "commands": [command_template_to_dict(c) for c in p.commands],
    }


def alias_to_dict(a: TuoniAlias) -> dict[str, Any]:
    out = {
        "alias_id": a.alias_id,
        "name": a.name,
        "qualified_name": a.qualifiedName,
        "fully_qualified_name": a.fullyQualifiedName,
        "description": a.description,
        "fixed_configuration": a.fixedConfiguration,
        "plugin_version": a.pluginVersion,
        "base_template": a.baseTemplate,
    }
    status = getattr(a, "status", None)
    if status is not None:
        out["status"] = status
    return out


def alias_dict_from_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Serialize an alias straight from a server JSON dict (no TuoniAlias wrapper).

    Used by tools that bypass TuoniLib's wrapper (e.g. PUT on /command-alias/{id}
    which creates a brand-new alias the wrapper can't easily represent).
    """
    if raw is None:
        return {}
    return {
        "alias_id": raw.get("id"),
        "name": raw.get("name"),
        "qualified_name": raw.get("qualifiedName"),
        "fully_qualified_name": raw.get("fullyQualifiedName"),
        "description": raw.get("description"),
        "fixed_configuration": raw.get("fixedConfiguration"),
        "plugin_version": raw.get("pluginVersion"),
        "base_template": raw.get("baseTemplate"),
        "status": raw.get("status"),
    }


def file_to_dict(f: TuoniFile) -> dict[str, Any]:
    return {
        "file_id": f.fileId,
        "original_filename": f.originalFileName,
        "size": f.size,
        "file_paths": f.filePaths,
        "download_href": f.downloadHref,
    }


def user_to_dict(u: TuoniUser) -> dict[str, Any]:
    return {
        "username": u.username,
        "enabled": u.enabled,
        "authorities": u.authorities,
    }


def host_to_dict(h: TuoniDataHost) -> dict[str, Any]:
    return {
        "id": h.id,
        "address": h.address,
        "name": h.name,
        "note": h.note,
        "status": h.status,
    }


def service_to_dict(s: TuoniDataService) -> dict[str, Any]:
    return {
        "id": s.id,
        "address": s.address,
        "port": s.port,
        "protocol": s.protocol,
        "banner": s.banner,
        "note": s.note,
        "status": s.status,
    }


def credential_to_dict(c: TuoniDataCredential) -> dict[str, Any]:
    return {
        "id": c.id,
        "username": c.username,
        "password": c.password,
        "host": c.host,
        "realm": c.realm,
        "source": c.source,
        "note": c.note,
        "status": c.status,
    }


def job_to_dict(j: TuoniJob) -> dict[str, Any]:
    return {
        "job_id": j.job_id,
        "name": j.name,
        "status": j.status,
        "source": j.source,
        "supported_actions": j.supportedActions,
        "open_resources": j.openResources,
        "messages": j.messages,
    }
