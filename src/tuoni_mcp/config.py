from __future__ import annotations

import os
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


class Profile(str, Enum):
    AGENT_OPS = "agent-ops"
    ACCESS = "access"
    ARTIFACTS = "artifacts"
    DISCOVERY = "discovery"
    ADMINISTRATION = "administration"
    AUTHORING = "authoring"
    EVENTS = "events"
    RAW = "raw"


class MCPMode(str, Enum):
    NATIVE = "native"
    DISPATCH = "dispatch"


DEFAULT_PROFILES = frozenset({Profile.AGENT_OPS})
ALL_PROFILES = frozenset(Profile)
ALL_EXPANSION = ALL_PROFILES - {Profile.RAW}
_PROFILE_TOKENS = {profile.value: profile for profile in Profile}
_PROFILE_TOKENS["all"] = None


def validate_base_url(url: str) -> str:
    """Validate without echoing a potentially credential-bearing URL."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
        valid = (
            parsed.scheme in {"http", "https"} and parsed.hostname
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
            and (port is None or 1 <= port <= 65535)
            and not any(character.isspace() or ord(character) < 32 for character in url)
            and "\\" not in url
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("TUONI_URL must be an HTTP(S) base URL without credentials, query, fragment, or invalid port")
    return url.rstrip("/")


def _env_bool(name: str, default: bool = False, *, environ: Mapping[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    raw = source.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false, got: {raw!r}")


def _parse_verify(raw: str | None) -> str | bool:
    if raw is None or raw.strip() == "":
        return True
    low = raw.strip().lower()
    if low in {"1", "true", "yes", "on"}:
        return True
    if low in {"0", "false", "no", "off"}:
        return False
    if not Path(raw).is_file():
        raise ValueError(f"TUONI_VERIFY_TLS points to a non-existent file: {raw}")
    return raw


def _parse_timeout(raw: str | None) -> float:
    if raw is None or raw.strip() == "":
        return 30.0
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"TUONI_REQUEST_TIMEOUT must be a number of seconds, got: {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("TUONI_REQUEST_TIMEOUT must be finite and greater than zero")
    return value


def _parse_download_dir(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("TUONI_MCP_DOWNLOAD_DIR must be an absolute path")
    resolved = path.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise ValueError("TUONI_MCP_DOWNLOAD_DIR must not be a filesystem root")
    return str(resolved)


def _parse_upload_dir(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("TUONI_MCP_UPLOAD_DIR must be an absolute path")
    try:
        if path.is_symlink():
            raise ValueError("TUONI_MCP_UPLOAD_DIR must not be a symlink")
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("TUONI_MCP_UPLOAD_DIR must be an existing directory") from exc
    if not resolved.is_dir():
        raise ValueError("TUONI_MCP_UPLOAD_DIR must be an existing directory")
    if resolved == Path(resolved.anchor):
        raise ValueError("TUONI_MCP_UPLOAD_DIR must not be a filesystem root")
    return str(resolved)


def _positive_int(name: str, raw: str, *, minimum: int = 1) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got: {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got: {value}")
    return value


def parse_profiles(raw: str | None, *, present: bool | None = None) -> frozenset[Profile]:
    """Parse and expand startup-static profiles.

    `present` distinguishes an omitted variable from a present-but-empty one. It is
    optional so callers outside Settings.from_env can parse a value directly.
    """
    if present is None:
        present = raw is not None
    if not present:
        return DEFAULT_PROFILES
    if raw is None or not raw.strip():
        raise ValueError(
            "TUONI_MCP_PROFILES must not be empty; omit it to use the agent-ops default"
        )

    tokens = [part.strip().lower() for part in raw.split(",")]
    if any(not token for token in tokens):
        raise ValueError("TUONI_MCP_PROFILES contains an empty profile name")
    unknown = sorted(set(tokens) - _PROFILE_TOKENS.keys())
    if unknown:
        valid = ", ".join(sorted(_PROFILE_TOKENS))
        raise ValueError(
            f"TUONI_MCP_PROFILES contains unknown profile(s): {', '.join(unknown)}. "
            f"Valid profiles: {valid}"
        )

    expanded: set[Profile] = set()
    for token in tokens:
        if token == "all":
            expanded.update(ALL_EXPANSION)
        else:
            profile = _PROFILE_TOKENS[token]
            assert profile is not None
            expanded.add(profile)
    if not expanded:
        raise ValueError("TUONI_MCP_PROFILES must select at least one profile")
    return frozenset(expanded)


def parse_tool_prefix(raw: str | None) -> str:
    """Parse the prefix the connected client adds to every exposed tool name.

    OpenCode exposes `tuoni_agents_list` as `tuoni_tuoni_agents_list`, so a
    continuation naming the server-side tool is not callable there. Declaring
    the prefix makes returned continuations match what the client exposes.
    """
    if raw is None or not raw.strip():
        return ""
    prefix = raw.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", prefix):
        raise ValueError(
            "TUONI_MCP_TOOL_PREFIX may contain only letters, digits, underscore, dot, or hyphen"
        )
    if len(prefix) > 64:
        raise ValueError("TUONI_MCP_TOOL_PREFIX must be at most 64 characters")
    return prefix


def parse_mode(raw: str | None, *, present: bool | None = None) -> MCPMode:
    if present is None:
        present = raw is not None
    if not present:
        return MCPMode.NATIVE
    if raw is None or not raw.strip():
        raise ValueError("TUONI_MCP_MODE must not be empty; omit it to use native mode")
    try:
        return MCPMode(raw.strip().lower())
    except ValueError as exc:
        raise ValueError(
            f"TUONI_MCP_MODE must be native or dispatch, got: {raw!r}"
        ) from exc


@dataclass(frozen=True)
class Settings:
    url: str
    username: str
    password: str = field(repr=False)
    verify: str | bool
    read_only: bool = False
    events_enabled: bool = field(init=False)
    event_buffer_size: int = 200
    request_timeout: float = 30.0
    response_max_bytes: int = 16 * 1024 * 1024
    profiles: frozenset[Profile] = field(default_factory=lambda: DEFAULT_PROFILES)
    mode: MCPMode = MCPMode.NATIVE
    download_dir: str | None = None
    download_max_bytes: int = 128 * 1024 * 1024
    upload_dir: str | None = None
    upload_max_bytes: int = 16 * 1024 * 1024
    tool_prefix: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", validate_base_url(self.url))
        try:
            normalized_profiles = frozenset(
                profile if isinstance(profile, Profile) else Profile(profile)
                for profile in self.profiles
            )
        except ValueError as exc:
            raise ValueError("Settings.profiles contains an unknown profile") from exc
        if not normalized_profiles:
            raise ValueError("Settings.profiles must not be empty")
        normalized_mode = self.mode if isinstance(self.mode, MCPMode) else MCPMode(self.mode)
        if Profile.RAW in normalized_profiles and normalized_mode is MCPMode.DISPATCH:
            raise ValueError(
                "The raw profile is native-mode-only; remove raw or set TUONI_MCP_MODE=native"
            )
        if self.event_buffer_size < 1:
            raise ValueError("TUONI_MCP_EVENT_BUFFER must be at least 1")
        if self.download_max_bytes < 1:
            raise ValueError("Settings.download_max_bytes must be at least 1")
        if self.response_max_bytes < 1:
            raise ValueError("Settings.response_max_bytes must be at least 1")
        if self.upload_max_bytes < 1:
            raise ValueError("Settings.upload_max_bytes must be at least 1")
        if not math.isfinite(self.request_timeout) or self.request_timeout <= 0:
            raise ValueError("TUONI_REQUEST_TIMEOUT must be finite and greater than zero")
        normalized_download_dir = _parse_download_dir(self.download_dir)
        normalized_upload_dir = _parse_upload_dir(self.upload_dir)
        object.__setattr__(self, "tool_prefix", parse_tool_prefix(self.tool_prefix))
        object.__setattr__(self, "profiles", normalized_profiles)
        object.__setattr__(self, "mode", normalized_mode)
        object.__setattr__(self, "events_enabled", Profile.EVENTS in normalized_profiles)
        object.__setattr__(self, "download_dir", normalized_download_dir)
        object.__setattr__(self, "upload_dir", normalized_upload_dir)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if environ is None else environ
        missing = [
            key
            for key in ("TUONI_URL", "TUONI_USERNAME", "TUONI_PASSWORD")
            if not source.get(key)
        ]
        if missing:
            raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
        if "TUONI_MCP_TOOLGROUPS" in source:
            raise ValueError(
                "TUONI_MCP_TOOLGROUPS is unsupported; use additive TUONI_MCP_PROFILES "
                "(default: agent-ops)"
            )
        if "TUONI_MCP_EVENTS" in source:
            raise ValueError(
                "TUONI_MCP_EVENTS is unsupported; add or remove the events entry in "
                "TUONI_MCP_PROFILES"
            )
        if "TUONI_MCP_MAX_INLINE_BYTES" in source:
            raise ValueError(
                "TUONI_MCP_MAX_INLINE_BYTES is unsupported; download tools return authenticated "
                "references and never inline binary bodies"
            )

        profiles = parse_profiles(
            source.get("TUONI_MCP_PROFILES"),
            present="TUONI_MCP_PROFILES" in source,
        )
        mode = parse_mode(
            source.get("TUONI_MCP_MODE"),
            present="TUONI_MCP_MODE" in source,
        )
        return cls(
            url=source["TUONI_URL"].rstrip("/"),
            username=source["TUONI_USERNAME"],
            password=source["TUONI_PASSWORD"],
            verify=_parse_verify(source.get("TUONI_VERIFY_TLS")),
            read_only=_env_bool("TUONI_MCP_READ_ONLY", default=False, environ=source),
            event_buffer_size=_positive_int(
                "TUONI_MCP_EVENT_BUFFER", source.get("TUONI_MCP_EVENT_BUFFER", "200")
            ),
            request_timeout=_parse_timeout(source.get("TUONI_REQUEST_TIMEOUT")),
            response_max_bytes=_positive_int(
                "TUONI_MCP_RESPONSE_MAX_BYTES",
                source.get("TUONI_MCP_RESPONSE_MAX_BYTES", str(16 * 1024 * 1024)),
            ),
            profiles=profiles,
            mode=mode,
            download_dir=_parse_download_dir(source.get("TUONI_MCP_DOWNLOAD_DIR")),
            download_max_bytes=_positive_int(
                "TUONI_MCP_DOWNLOAD_MAX_BYTES",
                source.get("TUONI_MCP_DOWNLOAD_MAX_BYTES", str(128 * 1024 * 1024)),
            ),
            upload_dir=source.get("TUONI_MCP_UPLOAD_DIR"),
            upload_max_bytes=_positive_int(
                "TUONI_MCP_UPLOAD_MAX_BYTES",
                source.get("TUONI_MCP_UPLOAD_MAX_BYTES", str(16 * 1024 * 1024)),
            ),
            tool_prefix=parse_tool_prefix(source.get("TUONI_MCP_TOOL_PREFIX")),
        )
