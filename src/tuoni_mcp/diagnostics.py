"""Bounded configuration and optional authenticated, read-only backend diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import SSLError, Timeout
from tuoni.TuoniExceptions import ExceptionTuoniAuthentication

from tuoni_mcp import __version__
from tuoni_mcp.config import ALL_PROFILES, MCPMode, Settings
from tuoni_mcp.connection import ServerContext, authentication_secrets, call_without_retry, ensure_fresh_token
from tuoni_mcp.http_client import ResponseTooLarge
from tuoni_mcp.pagination import bounded_text
from tuoni_mcp.protocol import redact_text, would_fit_success

if TYPE_CHECKING:
    from tuoni_mcp.registry import ToolRegistry


DIAGNOSTICS_BUDGET = 4 * 1024
INFO_PATH = "/api/v2/info"
IDENTITY_PATH = "/api/v1/users/me"


def catalog_summary(registry: ToolRegistry, cfg: Settings) -> dict[str, Any]:
    """Count enabled operations, not the three/four dispatch adapter tools.

    A shared operation can belong to several profiles, so profile counts are
    independent and must not be summed to obtain the total.
    """
    selected = registry.select(cfg.profiles, read_only=cfg.read_only)
    return {
        "enabled_operations": len(selected),
        "by_profile": {
            profile.value: sum(profile in spec.profiles for spec in selected)
            for profile in sorted(cfg.profiles, key=lambda item: item.value)
        },
        "withheld_by_profile": sum(not spec.profiles.intersection(cfg.profiles) for spec in registry.specs),
        "withheld_read_only": sum(
            bool(spec.profiles.intersection(cfg.profiles)) and spec.effect.value != "read"
            for spec in registry.specs
        ) if cfg.read_only else 0,
    }


def _failure(exc: Exception) -> dict[str, str]:
    """Expose fixed remediation, never URLs, credentials, or upstream error bodies."""
    status = getattr(exc, "status_code", None)
    if status is None and isinstance(exc, ExceptionTuoniAuthentication):
        status = getattr(exc.__cause__, "status_code", None)
    if isinstance(exc, SSLError):
        code, hint = "TLS_ERROR", "Set TUONI_VERIFY_TLS to the trusted CA bundle."
    elif isinstance(exc, (TimeoutError, Timeout)):
        code, hint = "TIMEOUT", "Check backend reachability and TUONI_REQUEST_TIMEOUT."
    elif isinstance(exc, (RequestsConnectionError, ConnectionError)):
        code, hint = "UNREACHABLE", "Check the configured backend address and network."
    elif status == 403:
        code, hint = "FORBIDDEN", "This account cannot access this endpoint."
    elif status == 401 or isinstance(exc, ExceptionTuoniAuthentication):
        code, hint = "AUTHENTICATION_FAILED", "Check the configured Tuoni credentials."
    elif status in {404, 405}:
        code, hint = "ENDPOINT_UNAVAILABLE", "Check the backend version and API compatibility."
    elif status == 429:
        code, hint = "RATE_LIMITED", "Wait before checking again."
    elif isinstance(status, int) and status >= 500:
        code, hint = "SERVER_ERROR", "Check backend health, then retry the diagnostic."
    elif isinstance(exc, ResponseTooLarge):
        code, hint = "RESPONSE_TOO_LARGE", "The backend exceeded the configured response limit."
    elif isinstance(exc, (ValueError, TypeError)):
        code, hint = "INVALID_RESPONSE", "The endpoint did not return its documented response."
    elif isinstance(exc, OSError):
        code, hint = "UNREACHABLE", "Check the configured backend address and network."
    else:
        code, hint = "REQUEST_FAILED", "Check backend compatibility and server logs."
    return {"status": "error", "code": code, "hint": hint}


def _safe_text(value: str, ctx: ServerContext, limit: int) -> str:
    text = value.encode("utf-8", errors="replace").decode("utf-8")
    for secret in authentication_secrets(ctx):
        text = text.replace(secret, "[REDACTED]")
    text = "".join(character for character in redact_text(text) if ord(character) >= 32 and ord(character) != 127)
    return bounded_text(text, limit)


def _server_info(value: Any, ctx: ServerContext) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get("version"), str) or not value["version"].strip():
        raise ValueError("Invalid InfoResponse")
    return {"status": "ok", "version": _safe_text(value["version"], ctx, 64)}


def _identity(value: Any, ctx: ServerContext) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or not isinstance(value.get("username"), str)
        or not value["username"].strip()
        or not isinstance(value.get("enabled"), bool)
        or not isinstance(value.get("authorities"), list)
        or any(not isinstance(item, str) for item in value["authorities"])
    ):
        raise ValueError("Invalid UserResponse")
    authorities = value["authorities"]
    result = {
        "status": "ok" if value["enabled"] else "error",
        "username": _safe_text(value["username"], ctx, 96),
        "enabled": value["enabled"],
        "authorities": [_safe_text(item, ctx, 64) for item in authorities[:12]],
        "authorities_omitted": max(0, len(authorities) - 12),
    }
    if not value["enabled"]:
        result["code"] = "ACCOUNT_DISABLED"
    return result


async def collect_diagnostics(
    ctx: ServerContext,
    *,
    check_connection: bool = False,
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a report; ``ok`` is false when a requested backend check fails.

    Offline success confirms configuration only. It does not claim connectivity.
    Network checks use one authentication preflight and two independent GETs,
    each with the existing request deadline/response limit. Cancellation escapes.
    Catalog metadata must come from :func:`catalog_summary` on the live registry.
    """
    cfg = ctx.cfg
    https = urlsplit(cfg.url).scheme == "https"
    tls = "not_used_http" if not https else (
        "disabled" if cfg.verify is False else "custom_ca" if isinstance(cfg.verify, str) else "system_ca"
    )
    unavailable: dict[str, Any] = {
        "profiles": sorted(profile.value for profile in ALL_PROFILES - cfg.profiles),
    }
    if cfg.read_only:
        unavailable["mutations"] = "read_only"
    if cfg.download_dir is None:
        unavailable["local_downloads"] = "download_directory_not_configured"
    if cfg.upload_dir is None:
        unavailable["local_uploads"] = "upload_directory_not_configured"
    if cfg.mode is MCPMode.DISPATCH:
        unavailable["raw"] = "native_mode_required"
    report: dict[str, Any] = {
        "ok": True,
        "mcp": {
            "version": __version__,
            "mode": cfg.mode.value,
            "profiles": sorted(profile.value for profile in cfg.profiles),
            "read_only": cfg.read_only,
        },
        "transport": {
            "protocol": "https" if https else "http",
            "tls_verification": tls,
            "request_timeout_seconds": cfg.request_timeout,
            "response_max_bytes": cfg.response_max_bytes,
            "download_max_bytes": cfg.download_max_bytes,
            "upload_max_bytes": cfg.upload_max_bytes,
            "event_buffer_items": cfg.event_buffer_size,
        },
        "unavailable": unavailable,
        "backend": {"status": "not_checked"},
    }
    if catalog is not None:
        report["catalog"] = catalog
    connections = getattr(ctx.c2, "_max_connections", None)
    if isinstance(connections, int) and not isinstance(connections, bool):
        report["transport"]["max_connections"] = connections
    if not check_connection:
        return report

    backend: dict[str, Any] = {"status": "degraded"}
    report["backend"] = backend
    try:
        await ensure_fresh_token(ctx)
    except Exception as exc:
        backend["session"] = _failure(exc)
        backend["server"] = {"status": "skipped", "reason": "authentication_preflight_failed"}
        backend["identity"] = {"status": "skipped", "reason": "authentication_preflight_failed"}
        report["ok"] = False
        return report

    # A cached token is not proof of current server availability or permissions.
    backend["session"] = {"status": "ready"}
    for name, path, normalize in (
        ("server", INFO_PATH, _server_info),
        ("identity", IDENTITY_PATH, _identity),
    ):
        try:
            value = await call_without_retry(ctx, ctx.c2.request_get, path)
            backend[name] = normalize(value, ctx)
        except Exception as exc:
            backend[name] = _failure(exc)
    report["ok"] = all(backend[name]["status"] == "ok" for name in ("server", "identity"))
    backend["status"] = "ok" if report["ok"] else "degraded"

    # JSON escaping can expand even bounded strings. Preserve check outcomes and
    # identity while reducing only the explicitly counted authority preview.
    identity = backend["identity"]
    while not would_fit_success(report, budget=DIAGNOSTICS_BUDGET) and identity.get("authorities"):
        identity["authorities"].pop()
        identity["authorities_omitted"] += 1
    return report
