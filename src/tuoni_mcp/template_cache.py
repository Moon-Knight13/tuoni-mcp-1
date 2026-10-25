from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from tuoni_mcp.paths import url_path_segment


FetchJson = Callable[[str], Awaitable[Any]]


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float
    size: int = 0


def _template_ids(agent: dict[str, Any]) -> set[str]:
    raw = agent.get("availableCommandTemplates") or []
    if isinstance(raw, dict):
        raw = raw.values()
    return {str(value) for value in raw if value is not None}


def template_identifiers(template: dict[str, Any]) -> set[str]:
    return {
        str(template.get(key) or "").casefold()
        for key in ("id", "name", "qualifiedName", "fullyQualifiedName")
        if template.get(key)
    }


def template_fingerprint(template: dict[str, Any]) -> str:
    """Hash only command identity and validation-relevant fields."""
    material = {
        key: template.get(key)
        for key in (
            "id",
            "name",
            "pluginId",
            "scope",
            "qualifiedName",
            "fullyQualifiedName",
            "configurationSchema",
            "updateConfigurationSchema",
            "updateSchema",
            "supportedExecUnitTypes",
        )
    }
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TemplateCache:
    """Sixty-second raw agent/template cache with coalesced concurrent misses."""

    def __init__(
        self,
        ttl_seconds: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_agents: int = 256,
        max_agent_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if max_agents < 1 or max_agent_bytes < 1:
            raise ValueError("Agent cache limits must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_agents = max_agents
        self.max_agent_bytes = max_agent_bytes
        self._agent_bytes = 0
        self._clock = clock
        self._global: _CacheEntry | None = None
        self._agents: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._global_lock = asyncio.Lock()
        # Locks exist only while a request/waiter holds a strong reference, so
        # probing many unique GUIDs cannot grow this map forever.
        self._agent_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._locks_guard = asyncio.Lock()

    def _fresh(self, entry: _CacheEntry | None) -> bool:
        return entry is not None and entry.expires_at > self._clock()

    async def _agent_lock(self, agent_guid: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._agent_locks.setdefault(agent_guid, asyncio.Lock())

    async def _global_templates(self, fetch: FetchJson, *, force: bool = False) -> list[dict[str, Any]]:
        """Private immutable catalog; select before copying large plugin schemas."""
        if not force and self._fresh(self._global):
            return self._global.value
        async with self._global_lock:
            if not force and self._fresh(self._global):
                return self._global.value
            raw = await fetch("/api/v1/command-templates")
            if raw is None:
                templates: list[dict[str, Any]] = []
            elif isinstance(raw, list) and all(isinstance(item, dict) for item in raw):
                templates = raw
            else:
                raise ValueError("Tuoni returned an invalid command-template catalog")
            self._global = _CacheEntry(copy.deepcopy(templates), self._clock() + self.ttl_seconds)
            return self._global.value

    async def global_templates(self, fetch: FetchJson, *, force: bool = False) -> list[dict[str, Any]]:
        return copy.deepcopy(await self._global_templates(fetch, force=force))

    async def agent(self, fetch: FetchJson, agent_guid: str, *, force: bool = False) -> dict[str, Any] | None:
        cached = self._agents.get(agent_guid)
        if not force and self._fresh(cached):
            self._agents.move_to_end(agent_guid)
            return copy.deepcopy(cached.value)
        lock = await self._agent_lock(agent_guid)
        async with lock:
            cached = self._agents.get(agent_guid)
            if not force and self._fresh(cached):
                self._agents.move_to_end(agent_guid)
                return copy.deepcopy(cached.value)
            raw = await fetch(f"/api/v1/agents/{url_path_segment(agent_guid)}")
            if raw is not None and not isinstance(raw, dict):
                raise ValueError("Tuoni returned an invalid agent detail")
            size = len(json.dumps(raw, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
            previous = self._agents.pop(agent_guid, None)
            if previous is not None:
                self._agent_bytes -= previous.size
            if size <= self.max_agent_bytes:
                while self._agents and (
                    len(self._agents) >= self.max_agents or self._agent_bytes + size > self.max_agent_bytes
                ):
                    _, removed = self._agents.popitem(last=False)
                    self._agent_bytes -= removed.size
                self._agents[agent_guid] = _CacheEntry(copy.deepcopy(raw), self._clock() + self.ttl_seconds, size)
                self._agent_bytes += size
            return copy.deepcopy(raw)

    def cached_agent(self, agent_guid: str) -> dict[str, Any] | None:
        """Read a fresh cached agent without making an HTTP request."""
        entry = self._agents.get(agent_guid)
        return copy.deepcopy(entry.value) if self._fresh(entry) else None

    async def compatible_templates(
        self,
        fetch: FetchJson,
        agent_guid: str,
        *,
        force: bool = False,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        # Parallel refresh avoids a sequential round trip on the first search.
        agent, templates = await asyncio.gather(
            self.agent(fetch, agent_guid, force=force),
            self._global_templates(fetch, force=force),
        )
        if agent is None:
            return None, []
        available = _template_ids(agent)
        return agent, copy.deepcopy([item for item in templates if str(item.get("id")) in available])

    async def lookup_template(
        self,
        fetch: FetchJson,
        agent_guid: str,
        requested: str,
        *,
        force: bool = False,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
        agent, templates = await asyncio.gather(
            self.agent(fetch, agent_guid, force=force),
            self._global_templates(fetch, force=force),
        )
        if agent is None:
            return None, [], []
        available = _template_ids(agent)
        compatible = [item for item in templates if str(item.get("id")) in available]
        needle = requested.casefold()
        matches = copy.deepcopy([item for item in compatible if needle in template_identifiers(item)])
        # Ambiguity and typo hints need identities, not every schema. A describe
        # or prepare now copies only the selected command's schema.
        candidates = [
            {key: item[key] for key in ("id", "name", "qualifiedName", "fullyQualifiedName") if key in item}
            for item in compatible
        ]
        return agent, matches, copy.deepcopy(candidates)

    async def exact_template(
        self,
        fetch: FetchJson,
        agent_guid: str,
        requested: str,
        *,
        force: bool = False,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        agent, matches, _ = await self.lookup_template(fetch, agent_guid, requested, force=force)
        return agent, matches

    def invalidate_agent(self, agent_guid: str) -> None:
        removed = self._agents.pop(agent_guid, None)
        if removed is not None:
            self._agent_bytes -= removed.size

    def clear(self) -> None:
        self._global = None
        self._agents.clear()
        self._agent_bytes = 0


def cache_for(server: Any) -> TemplateCache:
    """Return the cache owned by one MCP lifespan context."""
    cache = getattr(server, "template_cache", None)
    if not isinstance(cache, TemplateCache):
        cache = TemplateCache()
        setattr(server, "template_cache", cache)
    return cache
