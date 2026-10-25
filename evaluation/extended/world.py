"""Synthetic HTTP boundary using real SDK adapters, with no network access.

Unknown endpoints fail explicitly rather than manufacturing successful responses.
The fixture records request bodies and resulting state for independent grading.
"""
from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import NAMESPACE_DNS, uuid5

from tuoni import TuoniC2

from evaluation.portable.world import PortableC2, _schema

AGENT_GUID = "00000000-0000-4000-8000-000000000001"


def template_id(name):
    return str(uuid5(NAMESPACE_DNS, "tuoni-fixture:" + name))


class SyntheticBridge:
    def __init__(self, ctx):
        self.status = SimpleNamespace(connected=True, dropped_pending_events=0)
        self._next_sequence = 1
        self.events = []
        self.startup_task = None
        ctx.c2.bridge = self

    async def start(self):
        pass

    async def stop(self):
        pass

    def events_after(self, after):
        oldest = self.events[0]["sequence"] if self.events else None
        return [e for e in self.events if e["sequence"] > after], oldest

    def emit(self, command_id):
        self.events.append({"sequence": self._next_sequence,
                            "source_uri": "tuoni://events/commands/results",
                            "topic": "command-result", "received_at": "2026-10-25T00:00:00Z",
                            "data": {"command_id": command_id}})
        self._next_sequence += 1


class ExtendedC2(PortableC2, TuoniC2):
    observes_mutations = True

    def __init__(self, templates=None, fault=None):
        TuoniC2.__init__(self, verify=True)
        PortableC2.__init__(self)
        self.writes = []
        self.bridge = None
        self.fault = fault
        for template in self.templates:
            template["id"] = template_id(template["name"])
        for command in self.commands.values():
            command["commandTemplateId"] = template_id(command["commandTemplateName"])
        self.agents[0]["guid"] = AGENT_GUID
        self.agents[0]["availableCommandTemplates"] = [t["id"] for t in self.templates if t["name"] != "sh"]
        if templates is not None:
            self.templates = copy.deepcopy(templates)
            self.agents[0]["availableCommandTemplates"] = [t["id"] for t in self.templates]
        self.listeners = {1: {"id": 1, "name": "fixture-listener", "info": "synthetic",
                              "status": "STOPPED", "plugin": "fixture.listener",
                              "configuration": {"port": 8443}, "supportedPayloadTemplateIds": ["fixture.windows-x64"]}}
        self.payloads = {42: {"id": 42, "name": "fixture-payload", "templateId": "fixture.windows-x64",
                              "configuration": {"type": "executable"}, "configurationFiles": [],
                              "status": "ACTIVE", "encryptedCommunication": True, "listeners": [1]}}
        self.files = {"file-1": {"fileId": "file-1", "originalFileName": "seed.txt", "size": 3,
                                  "filePaths": ["/fixture/seed.txt"], "downloadHref": "/api/v1/file/file-1/download"}}
        self.users = {"operator": {"username": "operator", "enabled": True, "authorities": ["VIEW_RESOURCES"]}}
        self.jobs = {1: {"id": 1, "name": "fixture-job", "status": "RUNNING",
                               "source": "fixture", "supportedActions": ["PAUSE", "RESUME", "RESTART"],
                               "openResources": [], "messages": []}}
        self.settings = {"fixture.label": "seed", "fixture.enabled": True,
                         "fixture.large": {"entries": ["entry-" + str(i) + "x" * 80 for i in range(150)]}}
        self.discovery = {
            "hosts": {"host-1": {"id": "host-1", "address": "192.0.2.10", "name": "seed", "note": "", "status": "ACTIVE"}},
            "services": {"service-1": {"id": "service-1", "address": "192.0.2.10", "port": 443,
                                        "protocol": "tcp", "banner": "fixture", "note": "", "status": "ACTIVE"}},
            "credentials": {"credential-1": {"id": "credential-1", "username": "seed", "password": "fixture-secret",
                                              "host": "192.0.2.10", "realm": "fixture", "source": "fixture",
                                              "note": "", "status": "ACTIVE"}},
        }
        self.aliases = {"alias-1": self.alias("alias-1", "seed-listing", {"dir": "."})}
        self.listener_plugin = {"identifier": {"id": "fixture.listener"},
                                "info": {"name": "Fixture listener", "vendor": "fixture", "description": "Synthetic listener"},
                                "configurationSchema": _schema({"port": {"type": "integer", "minimum": 1}}, required=["port"]),
                                "exampleConfigurations": [{"name": "default", "configuration": {"port": 8443}}]}
        self.payload_template = {"id": "fixture.windows-x64", "name": "fixture.windows-x64", "status": "ACTIVE",
                                 "description": "Synthetic Windows payload", "payloadType": "EXECUTABLE",
                                 "pluginId": "fixture.payload", "availableListenerIds": [1],
                                 "configurationSchema": _schema({"type": {"type": "string", "enum": ["executable", "service"]}}, required=["type"]),
                                 "exampleConfigurations": [{"name": "default", "configuration": {"type": "executable"}}]}

    @staticmethod
    def alias(identity, name, configuration):
        return {"id": identity, "name": name, "qualifiedName": "fixture:global:" + name,
                "fullyQualifiedName": "fixture:global:" + name, "description": "synthetic alias",
                "fixedConfiguration": configuration, "pluginVersion": "1", "baseTemplate": "template-ls", "status": "ACTIVE"}

    def snapshot(self):
        return copy.deepcopy({"calls": self.calls, "writes": self.writes, "commands": self.commands,
                              "listeners": self.listeners, "payloads": self.payloads, "files": self.files,
                              "users": self.users, "jobs": self.jobs, "settings": self.settings,
                              "discovery": self.discovery, "aliases": self.aliases, "agents": self.agents})

    @staticmethod
    def page(items, query):
        page, size = int(query.get("page", [0])[0]), int(query.get("pageSize", [10])[0])
        selected = items[page * size:(page + 1) * size]
        return {"items": selected, "page": page, "pageSize": size, "itemCount": len(selected),
                "totalCount": len(items), "totalPages": max(1, (len(items) + size - 1) // size),
                "hasMorePages": (page + 1) * size < len(items)}

    def request_get(self, uri, result_as_json=True, result_as_bytes=False):
        if result_as_bytes:
            self._record("GET", uri)
            return b"synthetic-file-content"
        path, query = urlsplit(uri).path, parse_qs(urlsplit(uri).query)
        if path == "/api/v2/commands":
            self._record("GET", uri)
            items = [{**v, "agentGuid": AGENT_GUID} for v in reversed(list(self.commands.values()))]
            return self.page(items, query)
        if path.startswith(("/api/v2/agents", "/api/v1/agents/", "/api/v1/commands/")) or path == "/api/v1/command-templates":
            return PortableC2.request_get(self, uri, result_as_json, result_as_bytes)
        self._record("GET", uri)
        if path == "/api/v1/plugins/listeners":
            return {"fixture.listener": self.listener_plugin}
        if path == "/api/v1/plugins/payloads":
            return {"fixture.payload": {"identifier": {"id": "fixture.payload"},
                    "info": {"name": "Fixture payload", "vendor": "fixture", "description": "Synthetic payloads"},
                    "payloads": {"fixture.windows-x64": self.payload_template}}}
        if path.startswith("/api/v1/payloads/templates/"):
            return copy.deepcopy(self.payload_template)
        if path == "/api/v1/plugins/commands":
            return {"fixture.commands": {"identifier": {"id": "fixture.commands"},
                    "info": {"name": "Fixture commands", "vendor": "fixture", "description": "Synthetic commands"},
                    "commands": {t["name"]: t for t in self.templates}}}
        if path.startswith("/api/v1/command-templates/"):
            return copy.deepcopy(next((t for t in self.templates if t["id"] == path.rsplit("/", 1)[-1]), None))
        collections = {"listeners": self.listeners, "payloads": self.payloads, "files": self.files,
                       "users": self.users, "command-alias": self.aliases}
        for name, store in collections.items():
            base = "/api/v1/" + name
            if path == base:
                return copy.deepcopy(store if name == "listeners" else list(store.values()))
            if path.startswith(base + "/"):
                key = unquote(path[len(base) + 1:])
                key = int(key) if name in {"listeners", "payloads"} and key.isdigit() else key
                return copy.deepcopy(store.get(key))
        if path.startswith("/api/v1/file/by-path/"):
            hosted = "/" + unquote(path.removeprefix("/api/v1/file/by-path/")).lstrip("/")
            return copy.deepcopy(next((f for f in self.files.values() if hosted in f["filePaths"]), None))
        if path.startswith("/api/v1/file/"):
            return copy.deepcopy(self.files.get(path.rsplit("/", 1)[-1]))
        if path.startswith("/api/v1/jobs/"):
            return self.page(list(self.jobs.values()), query) if "/all" in path else list(self.jobs.values())
        if path.startswith("/api/v1/job/"):
            return copy.deepcopy(self.jobs.get(int(path.rsplit("/", 1)[-1])))
        if path == "/api/v1/settings":
            return {"server": {k: {"key": k, "value": v} for k, v in self.settings.items()}, "plugin": {}}
        if path.startswith("/api/v1/setting/"):
            return {"key": unquote(path.rsplit("/", 1)[-1]), "value": self.settings.get(unquote(path.rsplit("/", 1)[-1]))}
        if path.startswith("/api/v1/discovery/"):
            tail = path.removeprefix("/api/v1/discovery/").split("/")
            if tail[0] in self.discovery:
                return self.page(list(self.discovery[tail[0]].values()), query)
            plural = {"host": "hosts", "service": "services", "credential": "credentials"}.get(tail[0])
            if plural and len(tail) == 2:
                return copy.deepcopy(self.discovery[plural].get(tail[1]))
        raise NotImplementedError("Synthetic fixture has no GET " + path)

    def write(self, method, uri, body=None, files=None):
        self._record(method, uri)
        path = urlsplit(uri).path
        body = copy.deepcopy(body or {})
        upload = next(iter((files or {}).values()), None)
        upload_info = {"filename": upload[0], "size": len(upload[1]), "sha256": hashlib.sha256(upload[1]).hexdigest()} if upload else None
        self.writes.append({"method": method, "uri": uri, "body": body, "upload": upload_info})
        if path == f"/api/v1/agents/{AGENT_GUID}/commands":
            created = self._create_command(body)
            self.commands[created["id"]]["commandTemplateId"] = body["template"]
            created["commandTemplateId"] = body["template"]
            if self.bridge:
                self.bridge.emit(created["id"])
            if self.fault == "ambiguous-command":
                from requests import Timeout
                raise Timeout("Synthetic response lost after accepting the command")
            return created
        if path.startswith("/api/v1/commands/"):
            identity = int(path.split("/")[4])
            if path.endswith("/stop"):
                self.commands[identity]["status"] = "CANCELED"
            elif path.endswith("/update"):
                self.commands[identity].setdefault("updates", []).append(body)
            return {"ok": True}
        if path.startswith("/api/v1/agents/"):
            agent = self.agents[0]
            if path.endswith("/block"):
                agent["status"] = "BLOCKED"
            elif path.endswith("/restore"):
                agent.update(active=True, status="ACTIVE")
            elif path.endswith("/inactive"):
                agent["active"] = False
            elif path.endswith("/metadata"):
                agent["metadata"].update(body)
            return {"ok": True}
        for name, store in [("listeners", self.listeners), ("payloads", self.payloads)]:
            base = "/api/v1/" + name
            if path == base:
                identity = max(store, default=0) + 1
                record = copy.deepcopy(next(iter(store.values())))
                record.update(body, id=identity)
                if name == "payloads":
                    record["templateId"] = body["payloadTemplateId"]
                store[identity] = record
                return copy.deepcopy(record)
            if path.startswith(base + "/"):
                parts = path[len(base) + 1:].split("/")
                identity = int(parts[0])
                if method == "DELETE":
                    return store.pop(identity, None)
                record = store[identity]
                record.update(body)
                if len(parts) > 1:
                    record["status"] = "STARTED" if parts[1] == "start" else "STOPPED"
                return copy.deepcopy(record)
        if path == "/api/v1/files":
            identity = "file-" + str(len(self.files) + 1)
            hosted = next(iter(files))
            record = {"fileId": identity, "originalFileName": upload[0], "size": len(upload[1]),
                      "filePaths": [hosted], "downloadHref": f"/api/v1/file/{identity}/download"}
            self.files[identity] = record
            return record
        if path.startswith("/api/v1/files/"):
            hosted = "/" + unquote(path.removeprefix("/api/v1/files/")).lstrip("/")
            self.files = {k: v for k, v in self.files.items() if hosted not in v["filePaths"]}
            return {"ok": True}
        if path.startswith("/api/v1/file/"):
            identity = path.rsplit("/", 1)[-1]
            if method == "DELETE":
                return self.files.pop(identity, None)
            self.files[identity].update(originalFileName=upload[0], size=len(upload[1]))
            return self.files[identity]
        if path == "/api/v1/users":
            self.users[body["username"]] = {**body, "enabled": True}
            return self.users[body["username"]]
        if path.startswith("/api/v1/users/"):
            parts = path.removeprefix("/api/v1/users/").split("/")
            self.users[parts[0]].update(body)
            return self.users[parts[0]]
        if path == "/api/v1/settings":
            self.settings.update(body)
            return {"updated": True}
        if path.startswith("/api/v1/setting/"):
            self.settings[unquote(path.rsplit("/", 1)[-1])] = body["value"]
            return body
        if path.startswith("/api/v1/job/"):
            _, identity, action = path.rsplit("/", 2)
            self.jobs[int(identity)]["status"] = "PAUSED" if action == "pause" else "RUNNING"
            return {"ok": True}
        if path == "/api/v1/command-alias":
            identity = "alias-" + str(len(self.aliases) + 1)
            result = self.alias(identity, body["name"], body["fixedConfiguration"])
            result.update(body)
            self.aliases[identity] = result
            return copy.deepcopy(result)
        if path.startswith("/api/v1/command-alias/"):
            identity = path.rsplit("/", 1)[-1]
            if method == "DELETE":
                self.aliases[identity]["status"] = "ARCHIVED"
            elif "fixedConfiguration" in body:
                self.aliases[identity]["status"] = "ARCHIVED"
                new_id = "alias-" + str(len(self.aliases) + 1)
                self.aliases[new_id] = {**self.aliases[identity], **body, "id": new_id, "status": "ACTIVE"}
                return copy.deepcopy(self.aliases[new_id])
            else:
                self.aliases[identity].update(body)
            return copy.deepcopy(self.aliases[identity])
        if path.startswith("/api/v1/discovery/"):
            tail = path.removeprefix("/api/v1/discovery/").split("/")
            plural = tail[0] if tail[0] in self.discovery else {"host": "hosts", "service": "services", "credential": "credentials"}[tail[0]]
            store = self.discovery[plural]
            if tail[-1] == "bulk-archive":
                for identities in body.values():
                    for identity in identities:
                        store[identity]["status"] = "ARCHIVED"
                return {"archived": True}
            if len(tail) == 1:
                identity = {"hosts": "host", "services": "service", "credentials": "credential"}[plural] + "-" + str(len(store) + 1)
                store[identity] = {**body, "id": identity, "status": "ACTIVE"}
                return store[identity]
            record = store[tail[1]]
            if method == "DELETE":
                record["status"] = "ARCHIVED"
            else:
                record.update(body)
            return copy.deepcopy(record)
        raise NotImplementedError("Synthetic fixture has no " + method + " " + path)

    def request_post(self, uri, json_data=None, files=None):
        return self.observed_write("POST", uri, json_data, files)

    def request_put(self, uri, json_data=None):
        return self.observed_write("PUT", uri, json_data)

    def request_patch(self, uri, json_data=None, files=None):
        return self.observed_write("PATCH", uri, json_data, files)

    def request_delete(self, uri, json_data=None):
        return self.observed_write("DELETE", uri, json_data)

    def request_put_multipart(self, uri, files):
        return self.observed_write("PUT", uri, files=files)

    def observed_write(self, method, uri, body=None, files=None):
        # Mirror the production HTTP boundary, including composite SDK calls.
        # A response lost after acceptance intentionally remains unacknowledged.
        from tuoni_mcp.mutations import mutation_request
        attempt = mutation_request(method)
        try:
            value = self.write(method, uri, body, files)
        except BaseException as exc:
            if attempt is not None and type(getattr(exc, "status_code", None)) is int:
                attempt.respond(exc.status_code)
            raise
        if attempt is not None:
            attempt.respond(200)
        return value
