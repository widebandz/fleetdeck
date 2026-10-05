#!/usr/bin/env python3
"""Optional read-only infrastructure facts for the fleet map.

Every relation is derived from an explicit local declaration or a bounded
observation. This module never emits addresses, raw paths, ports, private chat
handles, instruction contents, queued messages, or process command lines. A
single bounded local DQR chat number is projected after three-source agreement.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import plistlib
import re
import selectors
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCHEMA = "agent-fleet.snapshot.v1"
MAX_INPUT = 2_000_000
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
SLUG = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
PRIVATE = re.compile(r"(?:\b(?:\d{1,3}\.){3}\d{1,3}\b|\+?\d{10,}\b|"
                     r"\b(?:imsg:)?chat[\s:-]*\d+\b|(?:/(?:Users|home|tmp|private|var|etc)/|~/|file://)|"
                     r"(?<![A-Za-z0-9])/[A-Za-z0-9._-]+(?:/|$)|"
                     r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b|"
                     r"\b(?:sk-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|vck_[A-Za-z0-9]{16,})\b|"
                     r"\b(?:api[_ -]?key|access[_ -]?token|secret|password|authorization)\s*[:=]\s*[^\s,;]+)", re.I)
SERVICE_GROUPS = {"fleet", "apps", "models", "data"}
SERVICE_KINDS = {"app", "api", "web"}
SCOPE_BRIEF_VERSION = "fleet-map.scope-brief.v1"
SCOPE_BRIEF_KINDS = {"own", "input", "output", "boundary", "dependency",
                     "approval_gate", "tool_requirement", "tool_available",
                     "handoff", "completion_check", "completion_evidence"}
SCOPE_BRIEF_EVIDENCE = {"declared", "observed", "checked"}
SCOPE_BRIEF_OUTCOMES = {"required", "pending", "passed", "failed", "blocked", "partial"}
SCOPE_APPROVAL_DECISIONS = {"pending", "approved", "rejected"}
SCOPE_BRIEF_MAX_ITEMS = 36
SCOPE_BRIEF_CHECK_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_text(value: Any, limit: int = 100) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value) or PRIVATE.search(value):
        return None
    return value


def mtime(path: Path) -> str | None:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except OSError:
        return None


def read_text(path: Path, max_bytes: int = 64_000) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def read_json(path: Path, max_bytes: int = 512_000) -> dict | None:
    raw = read_text(path, max_bytes)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _scope_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            dt.date.fromisoformat(value)
            return True
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
            dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            return True
    except ValueError:
        pass
    return False


def _scope_brief(value: Any, session: str) -> dict | None:
    """Validate a small operator brief before exposing any of its prose."""
    keys = {"version", "session", "role", "mission", "source", "as_of",
            "status", "association", "items"}
    if (not isinstance(value, dict) or set(value) != keys
            or value.get("version") != SCOPE_BRIEF_VERSION
            or not isinstance(value.get("session"), str)
            or value.get("session") != session
            or value.get("status") != "declared"
            or value.get("association") != "session_name_only"
            or safe_text(value.get("role"), 120) is None
            or safe_text(value.get("mission"), 240) is None
            or safe_text(value.get("source"), 100) is None
            or not _scope_date(value.get("as_of"))):
        return None
    raw_items = value.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= SCOPE_BRIEF_MAX_ITEMS:
        return None
    items = []
    check_keys = set()
    for item in raw_items:
        if (not isinstance(item, dict)
                or not {"kind", "text", "source", "evidence", "as_of"} <= set(item)
                or set(item) - {"kind", "text", "source", "evidence", "as_of", "outcome",
                                "direction", "counterparty", "check_key", "decision", "approver"}
                or not isinstance(item.get("kind"), str)
                or item["kind"] not in SCOPE_BRIEF_KINDS
                or safe_text(item.get("text"), 240) is None
                or safe_text(item.get("source"), 100) is None
                or not isinstance(item.get("evidence"), str)
                or item["evidence"] not in SCOPE_BRIEF_EVIDENCE
                or not _scope_date(item.get("as_of"))):
            return None
        if "outcome" in item and (item["kind"] not in {"completion_check", "completion_evidence"}
                                  or not isinstance(item["outcome"], str)
                                  or item["outcome"] not in SCOPE_BRIEF_OUTCOMES):
            return None
        if item["kind"] == "handoff":
            if (("direction" in item) != ("counterparty" in item)):
                return None
            if "direction" in item and (item["direction"] not in ("incoming", "outgoing")
                                        or safe_text(item["counterparty"], 100) is None):
                return None
        elif "direction" in item or "counterparty" in item:
            return None
        if item["kind"] == "approval_gate":
            if ("decision" in item) != ("approver" in item):
                return None
            if "decision" in item and (not isinstance(item["decision"], str)
                                       or item["decision"] not in SCOPE_APPROVAL_DECISIONS
                                       or safe_text(item["approver"], 100) is None
                                       or (item["decision"] != "pending" and item["evidence"] == "declared")):
                return None
        elif "decision" in item or "approver" in item:
            return None
        if item["kind"] in {"completion_check", "completion_evidence"}:
            if "check_key" in item and (not isinstance(item["check_key"], str)
                                        or not SCOPE_BRIEF_CHECK_KEY.fullmatch(item["check_key"])):
                return None
            if item["kind"] == "completion_check" and "check_key" in item:
                if item["check_key"] in check_keys:
                    return None
                check_keys.add(item["check_key"])
        elif "check_key" in item:
            return None
        items.append({key: item[key] for key in ("kind", "text", "source", "evidence", "as_of",
                                                 "outcome", "direction", "counterparty", "check_key",
                                                 "decision", "approver")
                      if key in item})
    if any(item["kind"] == "completion_evidence" and "check_key" in item
           and item["check_key"] not in check_keys for item in items):
        return None
    return {key: value[key] for key in ("version", "session", "role", "mission", "source",
                                        "as_of", "status", "association")} | {"items": items}


def run_text(argv: list[str], timeout: float = 4.0, max_bytes: int = 512_000) -> str | None:
    if not argv or not argv[0] or not os.path.isabs(argv[0]):
        return None
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, close_fds=True)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        chunks = bytearray()
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                for key, _ in selector.select(remaining):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        break
                    chunks.extend(data)
                    if len(chunks) > max_bytes:
                        return None
        if proc.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
            return None
        return chunks.decode("utf-8")
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


class Graph:
    def __init__(self, raw: dict):
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA or not isinstance(raw.get("nodes"), list) or not isinstance(raw.get("edges"), list):
            raise ValueError("expected agent-fleet.snapshot.v1")
        self.data = copy.deepcopy(raw)
        self.nodes = {item.get("id") for item in self.data["nodes"] if isinstance(item, dict)}
        self.edges = {item.get("id") for item in self.data["edges"] if isinstance(item, dict)}
        self.at = stamp()

    def source(self, ident: str, available: bool | None, path: Path | None = None, detail: str | None = None):
        status = "available" if available is True else "partial" if available is None else "unavailable"
        sources = self.data.setdefault("sources", [])
        if not any(isinstance(x, dict) and x.get("id") == ident for x in sources):
            sources.append({"id": ident, "status": status, "as_of": self.at,
                            "source_mtime": mtime(path) if path else None})
        if available is False:
            self.unknown("source_unavailable", ident, detail or "Infrastructure metadata could not be read.")
        elif available is None and detail:
            self.unknown("invalid_metadata", ident, detail)

    def unknown(self, kind: str, source: str, detail: str):
        unknowns = self.data.setdefault("unknowns", [])
        unknowns.append({"id": f"unknown:{source}:{len(unknowns)+1}", "kind": kind,
                         "source": source, "as_of": self.at, "detail": detail})

    def node(self, ident: str, node_type: str, label: str, ref: str, **fields):
        if ident in self.nodes or not safe_text(label):
            return False
        value = {"id": ident, "type": node_type, "label": label, "parent_id": None,
                 "declared": True, "observed": None, "source_refs": [ref],
                 "observed_at": None}
        value.update(fields)
        self.data["nodes"].append(value)
        self.nodes.add(ident)
        return True

    def edge(self, ident: str, source: str, target: str, kind: str, ref: str,
             layer: str, payload: str, display: str, evidence: str = "declared",
             status: str = "configured-only", source_path: Path | None = None):
        if ident in self.edges or source not in self.nodes or target not in self.nodes:
            return False
        if any(safe_text(x, 140) is None for x in (payload, display)):
            return False
        self.data["edges"].append({"id": ident, "from": source, "to": target,
            "type": kind, "evidence": evidence, "source": ref,
            "layer": layer, "payload": payload, "display": display,
            "freshness": {"as_of": self.at, "source_mtime": mtime(source_path) if source_path else None,
                          "status": status}})
        self.edges.add(ident)
        return True

    def finish(self) -> dict:
        self.data["nodes"].sort(key=lambda x: x.get("id", ""))
        self.data["edges"].sort(key=lambda x: x.get("id", ""))
        summary = self.data.setdefault("summary", {})
        summary["semantic_edges"] = len(self.data["edges"])
        summary["infrastructure_nodes"] = sum(n.get("type") in {"service", "job", "data", "instruction", "endpoint"}
                                              and any(str(x).startswith("infra:") for x in n.get("source_refs", []))
                                              for n in self.data["nodes"])
        if any(s.get("status") != "available" for s in self.data.get("sources", []) if str(s.get("id", "")).startswith("infra:")):
            summary["source_health"] = "degraded"
        return self.data


def _listeners(env: dict[str, str], runner) -> dict[int, str] | None:
    exe = env.get("FLEETDECK_FLEET_LSOF_CLI") or shutil.which("lsof")
    raw = runner([exe, "-nP", "-iTCP", "-sTCP:LISTEN"], timeout=4) if exe else None
    if raw is None:
        return None
    out: dict[int, str] = {}
    rank = {"host": 0, "tailnet": 1, "open": 2}
    for line in raw.splitlines()[1:1000]:
        fields = line.split()
        if len(fields) < 9 or not fields[-1].startswith("("):
            continue
        address = fields[-2]
        bind, sep, port_text = address.rpartition(":")
        if not sep or not port_text.isdigit():
            continue
        port = int(port_text)
        if not 0 < port < 65536:
            continue
        reach = "host" if bind in {"127.0.0.1", "[::1]", "::1"} else "tailnet" if bind.startswith("100.") else "open"
        if port not in out or rank[reach] > rank[out[port]]:
            out[port] = reach
    return out


def _local_host(graph: Graph) -> str | None:
    return next((n.get("id") for n in graph.data["nodes"] if isinstance(n, dict)
                 and n.get("type") == "host" and n.get("reachability") == "local"), None) or next(
                     (n.get("id") for n in graph.data["nodes"] if isinstance(n, dict)
                      and n.get("type") == "host" and n.get("observed") is True), None)


def _services(graph: Graph, env: dict[str, str], runner) -> tuple[dict[int, list[str]], dict[int, str] | None]:
    path_text = env.get("FLEETDECK_FLEET_SERVICES_FILE")
    path = Path(path_text).expanduser() if path_text and os.path.isabs(path_text) else None
    registry = read_json(path) if path else None
    graph.source("infra:services", registry is not None, path,
                 "Curated service registry unavailable; service declarations are unknown.")
    listeners = _listeners(env, runner)
    graph.source("infra:listeners", listeners is not None, None,
                 "Local TCP listener metadata unavailable; live service status is unknown.")
    by_port: dict[int, list[str]] = {}
    host = _local_host(graph)
    if registry is None or not isinstance(registry.get("services"), list):
        return by_port, listeners
    invalid = 0
    for item in registry["services"][:100]:
        if not isinstance(item, dict):
            invalid += 1; continue
        slug, label, port = item.get("id"), safe_text(item.get("name")), item.get("port")
        if not isinstance(slug, str) or not SLUG.fullmatch(slug) or label is None or type(port) is not int or not 0 < port < 65536:
            invalid += 1; continue
        group = item.get("group", "apps")
        kind = item.get("kind", "app")
        if group not in SERVICE_GROUPS or kind not in SERVICE_KINDS:
            invalid += 1; continue
        ident = f"service:{slug}"
        if ident in graph.nodes:
            invalid += 1; continue
        observed = (port in listeners) if listeners is not None else None
        reach = listeners.get(port, "down") if listeners is not None else "unknown"
        graph.node(ident, "service", label, "infra:services", declared=True,
                   observed=observed, observed_at=graph.at if observed is not None else None,
                   kind=kind, group=group, reach=reach,
                   status="listener-present" if observed else "listener-absent" if observed is False else "unknown")
        by_port.setdefault(port, []).append(ident)
        if host in graph.nodes:
            graph.edge(f"declares_service:{host}:{ident}", host, ident, "declares_service",
                       "infra:services", "services", "service registration",
                       "Registered local service; availability is checked separately",
                       source_path=path)
            if observed:
                graph.edge(f"runs_service:{host}:{ident}", host, ident, "runs_service",
                           "infra:listeners", "services", "local TCP listener",
                           "Registered port has a listener; process identity is unverified",
                           evidence="observed", status="snapshot-computed")
    if invalid:
        graph.unknown("invalid_metadata", "infra:services", f"{invalid} service entries were excluded because their metadata was incomplete or unsafe.")
    graph.data.setdefault("summary", {})["services_registered"] = sum(len(v) for v in by_port.values())
    graph.data["summary"]["services_listening"] = sum(1 for port, ids in by_port.items() for _ in ids if listeners is not None and port in listeners)
    return by_port, listeners


def _source_conf(env: dict[str, str]) -> Path:
    value = env.get("TM_SESSIONS_CONF")
    if value:
        return Path(value).expanduser()
    config = env.get("TM_CONFIG_DIR")
    if config:
        return Path(config).expanduser() / "config" / "sessions.conf"
    return Path.home() / ".config/tmux-command-center/config/sessions.conf"


def _declared_root(path: Path, session: str) -> Path | None:
    raw = read_text(path, 256_000)
    if raw is None:
        return None
    found = []
    for line in raw.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        name, root = (x.strip() for x in line.split("=", 1))
        if name == session:
            candidate = Path(os.path.expandvars(os.path.expanduser(root)))
            if candidate.is_absolute():
                found.append(candidate)
    return found[0] if len(found) == 1 else None


def _card_fields(raw: str) -> dict[str, Any]:
    lines = raw.splitlines()
    if not lines or lines[0] != "---":
        return {}
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}
    fields: dict[str, Any] = {}
    current = None
    for line in lines[1:end]:
        if line.startswith("  - ") and current:
            fields.setdefault(current, []).append(line[4:].strip())
        elif not line.startswith(" ") and ":" in line:
            key, value = line.split(":", 1)
            if re.fullmatch(r"[a-z_]+", key):
                fields[key] = value.strip() if value.strip() else []
                current = key if not value.strip() else None
    return fields


def _responsibilities(raw: str) -> list[str] | None:
    lines = raw.splitlines()
    try:
        start = lines.index("## Responsibilities") + 1
    except ValueError:
        return None
    bullets = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if not line.strip():
            continue
        if not line.startswith("- "):
            # Wrapped or prose entries need human review, so omit the section.
            return None
        bullet = re.sub(r"\bchat\s*[-:]?\s*\d+\b", "bound chat", line[2:].strip(), flags=re.I)
        if safe_text(bullet, 140) is None:
            return None
        bullets.append(bullet)
        if len(bullets) > 3:
            return None
    return bullets or None


def _focus_files(graph: Graph, env: dict[str, str]) -> tuple[str | None, Path | None, Path | None]:
    focus = env.get("FLEETDECK_FLEET_FOCUS_SESSION", "trace")
    if not NAME.fullmatch(focus):
        return None, None, None
    session_node = next((n for n in graph.data["nodes"] if isinstance(n, dict) and n.get("type") == "session"
                         and n.get("label") == focus and isinstance(n.get("parent_id"), str)
                         and n["parent_id"].startswith("host:") and n.get("observed") is True), None)
    if not session_node:
        graph.unknown("focus_unknown", "infra:trace_identity", "Focus session is not currently observed; no Trace-specific infrastructure was inferred.")
        return None, None, None
    sid = session_node["id"]
    standard_path = _source_conf(env)
    root = _declared_root(standard_path, focus)
    graph.source("infra:trace_workspace", root is not None and root.is_dir(), standard_path,
                 "Focus session root declaration unavailable; instruction files are unknown.")
    memory = Path(env.get("TM_MEMORY_DIR") or Path.home() / ".config/agent-session-memory")
    card_path = memory / "identity" / f"{focus}.md"
    card_text = read_text(card_path)
    graph.source("infra:trace_identity", card_text is not None, card_path,
                 "Focus identity card unavailable; outbox route is unknown.")
    card = _card_fields(card_text) if card_text is not None else {}
    if card_text is not None and card.get("session") == focus:
        duties = _responsibilities(card_text)
        if duties:
            session_node["responsibilities"] = {"items": duties, "source": "infra:trace_identity",
                                                "as_of": graph.at, "status": "declared"}
            if "infra:trace_identity" not in session_node["source_refs"]:
                session_node["source_refs"].append("infra:trace_identity")
        else:
            graph.unknown("unsafe_metadata", "infra:trace_identity",
                          "Focus responsibilities could not be safely projected.")
    if root is not None and root.is_dir() and card.get("session") == focus:
        card_root = card.get("root")
        if isinstance(card_root, str) and os.path.realpath(os.path.expanduser(card_root)) != os.path.realpath(root):
            graph.unknown("root_drift", "infra:trace_identity", "Focus card root differs from the session standard; no instruction relation was inferred.")
            root = None
    if root is not None and root.is_dir():
        for filename in ("AGENTS.md", "CLAUDE.md"):
            file = root / filename
            if not file.is_file():
                continue
            ident = f"instruction:{focus}:{filename.lower()}"
            graph.node(ident, "instruction", filename, "infra:trace_workspace",
                       status="present", kind="markdown")
            graph.edge(f"has_instruction_file:{sid}:{ident}", sid, ident,
                       "has_instruction_file", "infra:trace_workspace", "instructions",
                       "instruction file available", "Instruction file exists in declared session root; current agent load is unverified",
                       source_path=file)
    outbox = None
    routes = card.get("routing_out")
    if isinstance(routes, list):
        exact = [item for item in routes if isinstance(item, str) and item.startswith(("~/", "/"))
                 and " " not in item and Path(item).name == "outbox"]
        if len(exact) == 1:
            candidate = Path(exact[0]).expanduser()
            if candidate.is_dir():
                outbox = candidate.resolve()
                ident = f"data:{focus}:outbox"
                graph.node(ident, "data", f"{focus.title()} outbox", "infra:trace_identity",
                           kind="queue", status="present")
                graph.edge(f"writes_queue:{sid}:{ident}", sid, ident, "writes_queue",
                           "infra:trace_identity", "state", "queued text files",
                           "Identity card declares an outbox write target; individual writes are not observed",
                           source_path=card_path)
    if outbox is None and card_text is not None:
        graph.unknown("unresolved_reference", "infra:trace_identity", "No exact existing outbox directory was declared for the focus session.")
    return sid, root, outbox


def _assignment(script: str, key: str) -> str | None:
    # Read a literal shell assignment only; never evaluate shell expansion.
    found = re.findall(r'^' + re.escape(key) + r'="([^"\n]*)"(?:\s*(?:#.*)?)?$', script, re.M)
    return found[0] if len(found) == 1 else None


def _job_status(label: str, listing: str | None) -> str:
    if listing is None:
        return "unknown"
    for line in listing.splitlines()[1:1000]:
        columns = line.split()
        if len(columns) >= 3 and columns[-1] == label:
            return "running" if columns[0].isdigit() else "registered"
    return "not-loaded"


def _chatbind_link(graph: Graph, env: dict[str, str], config: dict,
                   script_path: Path, job_id: str, focus: str, focus_sid: str | None) -> int | None:
    if not focus_sid or not script_path.is_absolute():
        return
    script = read_text(script_path, 128_000)
    if not script or not all(fragment in script for fragment in (
            'CONFIG_PATH = os.path.expanduser("~/.imsg-chatbind.json")',
            "with open(CONFIG_PATH)", "CFG        = load_cfg()",
            "b = bound_for(chat_id)", 'deliver_to_session(b.get("session", "main"), text, chat_id')):
        return
    variables = config.get("EnvironmentVariables")
    job_home = variables.get("HOME") if isinstance(variables, dict) else None
    if not isinstance(job_home, str) or not os.path.isabs(job_home):
        job_home = str(Path.home())
    config_path = Path(job_home) / ".imsg-chatbind.json"
    collector_path = Path(env.get("TM_CHATBIND") or Path.home() / ".imsg-chatbind.json")
    if os.path.realpath(config_path) != os.path.realpath(collector_path):
        return
    bindings = read_json(config_path)
    entries = bindings.get("bound") if isinstance(bindings, dict) else None
    if not isinstance(entries, list):
        return
    matches = [x for x in entries if isinstance(x, dict) and x.get("session") == focus
               and type(x.get("chat_id")) is int and x["chat_id"] >= 0]
    target = f"chat:bound-{focus}"
    if len(matches) != 1 or target not in graph.nodes or not any(
            e.get("type") == "chat_routes_to" and e.get("from") == target and
            e.get("to") == focus_sid and e.get("source") == "chatbind"
            for e in graph.data["edges"] if isinstance(e, dict)):
        return
    if sum(isinstance(x, dict) and x.get("chat_id") == matches[0]["chat_id"] for x in entries) != 1:
        return
    graph.edge(f"handles_bound_chat:{job_id}:{target}", job_id, target,
               "handles_bound_chat", "infra:launchagents", "routing",
               "incoming bound chat events",
               "Daemon config assigns this bound chat to the session; delivery is unobserved",
               source_path=config_path)
    return matches[0]["chat_id"]


def _jobs(graph: Graph, env: dict[str, str], runner, focus_sid: str | None,
          root: Path | None, outbox: Path | None,
          by_port: dict[int, list[str]], listeners: dict[int, str] | None) -> int | None:
    focus = env.get("FLEETDECK_FLEET_FOCUS_SESSION", "trace")
    if not NAME.fullmatch(focus):
        return None
    home = Path(env.get("FLEETDECK_FLEET_LAUNCHAGENTS_DIR") or Path.home() / "Library/LaunchAgents")
    if not home.is_dir():
        graph.source("infra:launchagents", False, home,
                     "Local LaunchAgent directory unavailable; job declarations are unknown.")
        return None
    launchctl = env.get("FLEETDECK_FLEET_LAUNCHCTL_CLI") or shutil.which("launchctl")
    listing = runner([launchctl, "list"], timeout=4) if launchctl else None
    graph.source("infra:launchctl", listing is not None, None,
                 "LaunchAgent load status unavailable; job registration is shown from configuration only.")
    specs = (
        ("ai.wideband.imsg.chatbind", "imsg-chatbind", "Chat binding service", "chatbind"),
        (f"com.wideband.{focus}-outbox", f"{focus}-outbox-send", f"{focus} outbox sender", "outbox"),
        (f"com.wideband.{focus}-session", f"{focus}-session-keep", f"{focus} session keeper", "keeper"),
        ("com.wideband.fleet-map-local", "portal_server.py", "Fleet map preview job", "map"),
    )
    found = 0
    map_port = None
    bound_chat_id = None
    host = _local_host(graph)
    for label, executable, safe_label, role in specs:
        path = home / (label + ".plist")
        try:
            if not path.is_file() or path.stat().st_size > 64_000:
                continue
            config = plistlib.loads(path.read_bytes())
        except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
            continue
        if not isinstance(config, dict) or config.get("Label") != label:
            continue
        argv = config.get("ProgramArguments")
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv) or not argv:
            continue
        executable_path = Path(argv[0])
        program = (argv[1] if role == "map" and len(argv) > 1 else argv[0])
        if Path(program).name != executable or not executable_path.is_absolute():
            continue
        if role == "map" and config.get("EnvironmentVariables", {}).get("FLEETDECK_FLEET_MAP") != "1":
            continue
        found += 1
        ident = f"job:{focus}-{role}" if role in ("outbox", "keeper") else f"job:{role}"
        graph.node(ident, "job", safe_label, "infra:launchagents",
                   kind="LaunchAgent", status=_job_status(label, listing))
        if host in graph.nodes:
            graph.edge(f"schedules_job:{host}:{ident}", host, ident, "schedules_job",
                       "infra:launchagents", "deployment", "job registration",
                       "Host has this LaunchAgent declaration; load status is shown separately",
                       source_path=path)
        if role == "chatbind":
            bound_chat_id = _chatbind_link(graph, env, config, executable_path, ident, focus, focus_sid)
            continue
        script_path = Path(program)
        script = read_text(script_path, 64_000) if script_path.is_absolute() else None
        if role == "keeper" and focus_sid and root and script:
            script_session = _assignment(script, "SESSION")
            script_root = _assignment(script, "ROOT")
            invokes = bool(re.search(r'\bnew-session\s+-d\s+-s\s+"\$SESSION"\s+-c\s+"\$ROOT"', script))
            if (script_session == focus and script_root and
                    os.path.realpath(script_root) == os.path.realpath(root) and invokes):
                graph.edge(f"launches:{ident}:{focus_sid}", ident, focus_sid, "launches",
                           "infra:launchagents", "deployment", "session start or repair",
                           "Keeper script is configured to create or repair this tmux session",
                           source_path=script_path)
        elif role == "outbox" and outbox and script:
            script_outbox = _assignment(script, "OUT")
            consumes = 'files=("$OUT"/*.txt)' in script
            sends = bool(re.search(r'\bsend\s+--chat-id\s+"\$CHAT_ID"\s+--service\s+imessage\s+--text\s+"\$p"', script))
            if script_outbox and os.path.realpath(script_outbox) == os.path.realpath(outbox) and consumes:
                data_id = f"data:{focus}:outbox"
                graph.edge(f"consumes_queue:{ident}:{data_id}", ident, data_id, "consumes_queue",
                           "infra:launchagents", "state", "queued text files",
                           "Sender script scans queued text files; a send is not implied",
                           source_path=script_path)
                card_path = Path(env.get("TM_MEMORY_DIR") or Path.home() / ".config/agent-session-memory") / "identity" / f"{focus}.md"
                card_raw = read_text(card_path)
                card = _card_fields(card_raw) if card_raw else {}
                binding = card.get("chat_binding")
                target = f"chat:bound-{focus}"
                chat_id = _assignment(script, "CHAT_ID")
                if (target in graph.nodes and sends and isinstance(binding, str)
                        and re.fullmatch(r"imsg:chat-([0-9]+)", binding)
                        and chat_id == binding.rsplit("-", 1)[1]
                        and chat_id == str(bound_chat_id)):
                    graph.edge(f"sends_chat:{ident}:{target}", ident, target, "sends_chat",
                               "infra:launchagents", "routing", "queued text messages",
                               "Sender and identity card target the bound channel; delivery is unobserved",
                               source_path=script_path)
        elif role == "map":
            vars = config.get("EnvironmentVariables")
            port_text = vars.get("FLEETDECK_PORT") if isinstance(vars, dict) else None
            if not isinstance(port_text, str) or not port_text.isdigit() or not 0 < int(port_text) < 65536:
                continue
            map_port = int(port_text)
            service_id = "service:fleet-map-local"
            observed = (map_port in listeners) if listeners is not None else None
            graph.node(service_id, "service", "Fleet map preview", "infra:launchagents",
                       kind="web", group="fleet", reach=(listeners.get(map_port, "down") if listeners is not None else "unknown"),
                       status="listener-present" if observed else "listener-absent" if observed is False else "unknown",
                       observed=observed, observed_at=graph.at if observed is not None else None)
            by_port.setdefault(map_port, []).append(service_id)
            graph.edge(f"launches:{ident}:{service_id}", ident, service_id, "launches",
                       "infra:launchagents", "deployment", "local map process",
                       "LaunchAgent args start the Fleetdeck map server in preview mode",
                       source_path=path)
            if host in graph.nodes and observed:
                graph.edge(f"runs_service:{host}:{service_id}", host, service_id, "runs_service",
                           "infra:listeners", "services", "local TCP listener",
                           "Map server port has a listener; process identity is unverified",
                           evidence="observed", status="snapshot-computed")
    graph.source("infra:launchagents", True, home)
    graph.data.setdefault("summary", {})["launchagents_declared"] = found
    return map_port


def _tailscale(graph: Graph, env: dict[str, str], runner,
               by_port: dict[int, list[str]], map_port: int | None):
    cli = env.get("FLEETDECK_FLEET_TAILSCALE_CLI") or shutil.which("tailscale")
    if not cli:
        candidate = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
        cli = str(candidate) if candidate.is_file() else None
    raw = runner([cli, "serve", "status", "--json"], timeout=4) if cli else None
    try:
        data = json.loads(raw) if raw is not None and len(raw) < 512_000 else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("Web"), dict) or not isinstance(data.get("TCP"), dict):
        graph.source("infra:tailscale_serve", False, None,
                     "Tailnet Serve mapping unavailable; endpoint routes are unknown.")
        return
    routes = []
    for authority, site in list(data["Web"].items())[:100]:
        if not isinstance(authority, str) or not isinstance(site, dict):
            continue
        outer_port = authority.rpartition(":")[2]
        tcp = data["TCP"].get(outer_port)
        if not outer_port.isdigit() or not isinstance(tcp, dict) or tcp.get("HTTPS") is not True:
            continue
        handlers = site.get("Handlers")
        if not isinstance(handlers, dict):
            continue
        for route, handler in list(handlers.items())[:10]:
            if route != "/" or not isinstance(handler, dict):
                continue
            proxy = handler.get("Proxy")
            parsed = urlparse(proxy) if isinstance(proxy, str) else None
            if not parsed or parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
                continue
            try:
                port = parsed.port
            except ValueError:
                continue
            if port in by_port and not parsed.path.strip("/") and not parsed.query and not parsed.fragment:
                routes.append((authority, port))
    for ordinal, (_, port) in enumerate(sorted(set(routes), key=lambda x: (x[1], x[0])), 1):
        targets = by_port[port]
        is_map = map_port == port and "service:fleet-map-local" in targets
        ident = "endpoint:tailnet-map" if is_map else f"endpoint:tailnet-link-{ordinal}"
        label = "Tailnet HTTPS map link" if is_map else f"Tailnet HTTPS link {ordinal}"
        graph.node(ident, "endpoint", label, "infra:tailscale_serve",
                   kind="reverse proxy", transport="HTTPS", reach="tailnet", status="configured")
        for target in targets:
            graph.edge(f"proxy_routes_to:{ident}:{target}", ident, target, "proxy_routes_to",
                       "infra:tailscale_serve", "network", "HTTPS requests",
                       "Tailnet Serve config proxies HTTPS requests to this local service; traffic is unobserved")
    graph.source("infra:tailscale_serve", True)
    graph.data.setdefault("summary", {})["tailnet_routes_declared"] = len(routes)


def _dqr_path(env: dict[str, str], key: str, default: Path) -> Path | None:
    value = env.get(key)
    path = Path(value).expanduser() if value else default
    return path if path.is_absolute() else None


def _scope_briefs(graph: Graph, env: dict[str, str]) -> None:
    """Attach local role briefs to observed sessions by exact tmux name only.

    A brief describes a declared responsibility model. It never verifies an
    agent occupant or creates tool, dependency, or handoff graph edges.
    """
    configured = env.get("FLEETDECK_FLEET_SCOPE_BRIEF_DIR")
    memory = Path(env.get("TM_MEMORY_DIR") or Path.home() / ".config/agent-session-memory")
    directory = Path(configured).expanduser() if configured else memory / "scope-briefs"
    if not directory.is_absolute():
        graph.unknown("invalid_metadata", "infra:scope_briefs",
                      "Scope brief directory is not absolute; role briefs were omitted.")
        return
    for session_node in graph.data["nodes"]:
        if (not isinstance(session_node, dict) or session_node.get("type") != "session"
                or session_node.get("observed") is not True):
            continue
        session = session_node.get("label")
        if (not isinstance(session, str) or not NAME.fullmatch(session)
                or session_node.get("id") != f"session:{session}"):
            continue
        path = directory / f"{session}.json"
        if not path.exists() and not path.is_symlink():
            continue
        source_id = f"infra:scope_brief:{session}"
        if path.is_symlink():
            graph.source(source_id, None, path,
                         "Scope brief is a link; responsibility model was omitted.")
            continue
        raw_text = read_text(path, 16_000)
        if raw_text is None:
            graph.source(source_id, False, path,
                         "Scope brief could not be read; responsibility model is unavailable.")
            continue
        try:
            raw_brief = json.loads(raw_text)
        except ValueError:
            raw_brief = None
        brief = _scope_brief(raw_brief, session)
        if brief is None:
            graph.source(source_id, None, path,
                         "Scope brief invalid or unreadable; responsibility model was omitted.")
            continue
        refs = session_node.get("source_refs")
        if not isinstance(refs, list) or (source_id not in refs and len(refs) >= 20):
            graph.source(source_id, None, path,
                         "Session has no room for scope provenance; responsibility model was omitted.")
            continue
        graph.source(source_id, True, path)
        session_node["responsibility_model"] = brief
        if source_id not in refs:
            refs.append(source_id)


def _dqr_scope(graph: Graph, env: dict[str, str], runner) -> None:
    """Project DQR's change-request scope without private chat or auth data.

    The card's separate image work is disclosed as out-of-scope drift, not
    connected to this focused change-request graph. No message, credential,
    participant handle, or raw filesystem path is copied into the snapshot.
    """
    session = next((n for n in graph.data["nodes"] if isinstance(n, dict)
                    and n.get("id") == "session:DQR" and n.get("type") == "session"), None)
    if session is None:
        return

    facts: list[dict[str, str]] = []

    def fact(key: str, value: str, evidence: str, source: str) -> None:
        if safe_text(value, 140) and safe_text(source, 100):
            facts.append({"key": key, "value": value, "evidence": evidence,
                          "source": source, "as_of": graph.at})

    home = Path(env.get("HOME") or Path.home())
    card_path = _dqr_path(env, "FLEETDECK_FLEET_DQR_CARD",
                          Path(env.get("TM_MEMORY_DIR") or home / ".config/agent-session-memory") / "identity/DQR.md")
    bind_path = _dqr_path(env, "FLEETDECK_FLEET_DQR_CHATBIND",
                          Path(env.get("TM_CHATBIND") or home / ".imsg-chatbind.json"))
    bind_script_path = _dqr_path(env, "FLEETDECK_FLEET_DQR_CHATBIND_SCRIPT", home / "bin/imsg-chatbind")
    push_path = _dqr_path(env, "FLEETDECK_FLEET_DQR_PUSH_SCRIPT", home / "bin/dqr-push")
    repo = _dqr_path(env, "FLEETDECK_FLEET_DQR_REPO", home / "dailyquranreading")

    card_text = read_text(card_path) if card_path else None
    card = _card_fields(card_text) if card_text else {}
    card_ok = card.get("session") == "DQR"
    graph.source("infra:dqr_identity", card_ok, card_path,
                 "DQR identity card unavailable or mismatched; client label and repo root are unknown.")
    # The collector faithfully lists every card tool. This focused map is the
    # DQR change-request path; retain a dated drift note instead of implying
    # its separate image script or draft machinery is part of that path.
    session.pop("responsibilities", None)
    session.pop("last_known_responsibilities", None)
    # Remove only the DQR edges created by the older scope projection. Other
    # sessions may use these relation types, so filtering by type would erase
    # unrelated facts from the fleet graph.
    omitted_edge_ids = {"holds_draft:dqr", "approves_draft:dqr", "releases_reply:dqr",
                        "stages_media:dqr", "writes_asset:dqr", "invokes_tool:dqr"}
    card_tools = card.get("tools") if card_ok else None
    if isinstance(card_tools, str):
        card_tool_names = {part.strip() for part in card_tools.split(",")}
    elif isinstance(card_tools, list):
        card_tool_names = set(card_tools)
    else:
        card_tool_names = set()
    media_card_declared = "dqr-media" in card_tool_names
    graph.data["edges"] = [e for e in graph.data["edges"] if isinstance(e, dict)
                           and e.get("id") not in omitted_edge_ids
                           and not (e.get("type") == "uses_tool" and e.get("from") == "session:DQR"
                                    and e.get("to") == "tool:dqr-media")]
    graph.edges = {e["id"] for e in graph.data["edges"]}
    omitted_nodes = {"chat:dqr-approval", "data:dqr-held-drafts"}
    omitted_nodes.add("tool:dqr-media")
    omitted_nodes = {node_id for node_id in omitted_nodes
                     if not any(node_id in (e.get("from"), e.get("to")) for e in graph.data["edges"])}
    graph.data["nodes"] = [n for n in graph.data["nodes"] if isinstance(n, dict)
                           and n.get("id") not in omitted_nodes]
    graph.nodes = {n["id"] for n in graph.data["nodes"]}

    binding = read_json(bind_path) if bind_path else None
    entries = binding.get("bound") if isinstance(binding, dict) else None
    matches = [b for b in entries if isinstance(b, dict) and b.get("session") == "DQR"
               and type(b.get("chat_id")) is int and b["chat_id"] >= 0] if isinstance(entries, list) else []
    bound = matches[0] if len(matches) == 1 else None
    card_binding = card.get("chat_binding") if card_ok else None
    chat_matches_card = bool(bound and isinstance(card_binding, str)
                             and re.fullmatch(r"imsg:chat-[0-9]+", card_binding)
                             and card_binding == f"imsg:chat-{bound['chat_id']}")
    bound_node = "chat:bound-DQR"
    bound_edge = any(isinstance(e, dict) and e.get("type") == "chat_routes_to"
                     and e.get("from") == bound_node and e.get("to") == "session:DQR"
                     and e.get("source") == "chatbind" for e in graph.data["edges"])
    unique_chat_id = bool(bound and isinstance(entries, list) and
                          sum(isinstance(b, dict) and b.get("chat_id") == bound["chat_id"]
                              for b in entries) == 1)
    bind_ok = bool(chat_matches_card and bound_edge and bound_node in graph.nodes and unique_chat_id
                   and 0 < bound["chat_id"] <= 1_000_000)
    graph.source("infra:dqr_chatbind", bind_ok, bind_path,
                 "DQR bound chat, card, and collector route do not agree; chat scope is unknown.")
    bind_script = read_text(bind_script_path, 128_000) if bind_script_path else None
    script_ok = bool(bind_script and all(marker in bind_script for marker in
                     ("def handle_record", "if is_operator(sender):",
                      'deliver_to_session(b.get("session", "main"), text, chat_id, sender=sender)',
                      "hold_draft(b, sender, text, draft)")))
    graph.source("infra:dqr_chatbind_script", script_ok, bind_script_path,
                 "DQR sender-specific chat handling is unavailable; pane routing is unknown.")
    if bind_ok:
        fact("chat_binding", "DQR bound chat configured; local ID shown on channel", "checked",
             "chatbind+DQR-card")
        chat_node = next(n for n in graph.data["nodes"] if n.get("id") == bound_node)
        chat_node["local_chat_id"] = bound["chat_id"]
        if "infra:dqr_chatbind" not in chat_node["source_refs"]:
            chat_node["source_refs"].append("infra:dqr_chatbind")
        label = bound.get("label")
        operator = binding.get("operator")
        if (isinstance(operator, str) and operator and isinstance(label, str)
                and re.match(r"^DQR\s*[—-]\s*Zayed\s*\+\s*Imam El\b", label)):
            fact("operator", "Zayed (configured operator)", "declared", "chatbind#operator")
        owner = card.get("owner") if card_ok else None
        if (isinstance(label, str) and re.match(r"^DQR\s*[—-]\s*Zayed\s*\+\s*Imam El\b", label)
                and isinstance(owner, str) and re.fullmatch(
                    r"dailyquranreading\.com\s+[—-]\s+Imam El engagement", owner)):
            fact("requester", "Imam El named as client contact; handle-to-person mapping unverified",
                 "declared", "identity:DQR#owner+chatbind#label")
        if script_ok:
            route = next(e for e in graph.data["edges"] if e.get("type") == "chat_routes_to"
                         and e.get("from") == bound_node and e.get("to") == "session:DQR")
            route["layer"] = "routing"
            route["payload"] = "operator instructions; external text held outside DQR"
            route["display"] = "Operator text targets DQR; external text is drafted and held by chatbind"

    media_bind_declared = bool(bound and bound.get("media") is True)
    if media_card_declared or media_bind_declared:
        details = []
        if media_card_declared:
            details.append("DQR card lists an image tool.")
        if media_bind_declared:
            details.append("DQR chat configuration enables media handling.")
        graph.unknown("out_of_scope_coupling", "infra:dqr_scope",
                      " ".join(details) + " Image handling is omitted from this focused change-request graph; execution is unobserved.")

    push_script = read_text(push_path, 128_000) if push_path else None

    repo_ok = bool(repo and repo.is_dir() and (repo / ".git").exists())
    git_cli = env.get("FLEETDECK_FLEET_DQR_GIT_CLI") or shutil.which("git")
    git = (lambda *args: runner([git_cli, "-C", str(repo), *args], timeout=4, max_bytes=2048)) if repo_ok and git_cli and os.path.isabs(git_cli) else None
    git_name = git("config", "user.name") if git else None
    git_branch = git("branch", "--show-current") if git else None
    git_remote = git("remote", "get-url", "origin") if git else None
    push_repo = _assignment(push_script, "REPO") if push_script else None
    push_name = _assignment(push_script, "WANT_NAME") if push_script else None
    push_branch = _assignment(push_script, "BRANCH") if push_script else None
    push_remote = _assignment(push_script, "REMOTE") if push_script else None
    # The helper credential itself is never queried by this frequent collector.
    push_guards_ok = bool(push_script and all(marker in push_script for marker in
                          ('[[ "$name" == "$WANT_NAME"', '[[ "$mail" == "$WANT_EMAIL"',
                           '[[ "${url%.git}" == "$REMOTE"', '[[ "$cred" == "$WANT_NAME"',
                           '[[ "$branch" == "$BRANCH"')))
    push_ok = bool(repo_ok and push_script and push_guards_ok and push_repo and push_name and push_branch
                   and push_remote and os.path.isabs(push_repo)
                   and os.path.realpath(push_repo) == os.path.realpath(repo)
                   and git_name and git_name.strip() == push_name
                   and git_branch and git_branch.strip() == push_branch
                   and git_remote and git_remote.strip().removesuffix(".git") == push_remote.removesuffix(".git")
                   and re.search(r'\bpush\s+origin\s+"\$BRANCH"', push_script))
    card_root = card.get("root") if card_ok else None
    expanded_card_root = os.path.expanduser(card_root) if isinstance(card_root, str) else None
    card_repo_ok = bool(repo_ok and expanded_card_root and os.path.isabs(expanded_card_root)
                        and os.path.realpath(expanded_card_root) == os.path.realpath(repo))
    graph.source("infra:dqr_repo", bool(push_ok and card_repo_ok), repo,
                 "DQR card root, repo, Git configuration, and push wrapper do not agree.")
    if push_ok and card_repo_ok:
        fact("git_identity", f"{push_name} (repository Git user; credential not rechecked)",
             "checked", "git-config+dqr-push")
        if (push_name == "haqzy" and push_branch == "main"
                and push_remote.removesuffix(".git") == "https://github.com/haqzy/dailyquranreading"):
            fact("repository", "GitHub haqzy/dailyquranreading · main", "checked", "git-origin+dqr-push")
            graph.node("workspace:dqr-repo", "workspace", "DQR local repository", "infra:dqr_repo",
                       kind="Git repository", status="present")
            graph.node("endpoint:dqr-github-main", "endpoint", "DQR GitHub main", "infra:dqr_repo",
                       kind="GitHub branch", status="configured")
            graph.edge("uses_workspace:dqr", "session:DQR", "workspace:dqr-repo", "uses_workspace",
                       "infra:dqr_repo", "state", "declared repository root",
                       "Card and push wrapper point to this repo; current pane cwd checked separately",
                       source_path=card_path)
            if "tool:dqr-push" in graph.nodes:
                graph.edge("pushes_to:dqr", "tool:dqr-push", "endpoint:dqr-github-main", "pushes_to",
                           "infra:dqr_repo", "deployment", "commit and Git push to main",
                           "Wrapper validates local identity and remote before pushing; push unobserved",
                           source_path=push_path)

    package = read_json(repo / "package.json", 128_000) if repo else None
    dependencies = {}
    if isinstance(package, dict):
        for bucket in ("dependencies", "devDependencies"):
            entries = package.get(bucket)
            if isinstance(entries, dict):
                dependencies.update(entries)
    major_versions = {"next": "16", "react": "19", "typescript": "5",
                      "tailwindcss": "4", "@supabase/supabase-js": "2"}
    stack_ok = bool(repo_ok and all(isinstance(dependencies.get(name), str)
                                    and re.fullmatch(r"[~^]?" + major + r"(?:\.[0-9]+){0,2}",
                                                     dependencies[name])
                                    for name, major in major_versions.items()))
    graph.source("infra:dqr_stack", stack_ok, repo / "package.json" if repo else None,
                 "DQR package dependencies unavailable or different; framework stack is unknown.")
    if stack_ok and card_repo_ok:
        fact("stack", "Next.js 16 · React 19 · TypeScript 5 · Tailwind 4 · Supabase SDK 2 (declared)",
             "declared", "package.json#dependencies")

    std = session.get("standard") if isinstance(session.get("standard"), dict) else {}
    roots = session.get("root_evidence") if isinstance(session.get("root_evidence"), dict) else {}
    if std.get("present") is False and roots.get("observed_cwd_within_card") is False:
        fact("runtime_scope", "Observed pane outside card root; DQR absent from session standard",
             "checked", "tmux+identity_cards+sessions_conf")
    elif std.get("present") is False:
        fact("runtime_scope", "DQR absent from session standard; pane placement unknown or separate",
             "checked", "sessions_conf+tmux")

    vercel = read_json(repo / ".vercel/project.json", 16_000) if repo else None
    vercel_ok = bool(vercel and vercel.get("projectName") == "dailyquranreading"
                     and isinstance(vercel.get("orgId"), str) and vercel["orgId"]
                     and isinstance(vercel.get("projectId"), str) and vercel["projectId"]
                     and push_script and "Vercel GitHub" in push_script
                     and "dailyquranreading.com" in push_script)
    graph.source("infra:dqr_deploy", vercel_ok, repo / ".vercel/project.json" if repo else None,
                 "DQR Vercel project link or declared GitHub deployment route unavailable.")
    if vercel_ok and "endpoint:dqr-github-main" in graph.nodes:
        graph.node("endpoint:dqr-production-site", "endpoint", "dailyquranreading.com production",
                   "infra:dqr_deploy", kind="Vercel site", status="configured")
        graph.edge("triggers_deploy:dqr", "endpoint:dqr-github-main", "endpoint:dqr-production-site",
                   "triggers_deploy", "infra:dqr_deploy", "deployment", "GitHub main commit to Vercel build",
                   "Wrapper declares the GitHub integration; a release is not observed",
                   source_path=repo / ".vercel/project.json")
        fact("deployment", "Vercel project linked locally; production release unobserved",
             "declared", "vercel-project+dqr-push")

    if facts:
        session["scope"] = {"facts": facts}


def enrich(raw: dict, env: dict[str, str] | None = None, runner=run_text) -> dict:
    graph = Graph(raw)
    env = os.environ if env is None else env
    by_port, listeners = _services(graph, env, runner)
    focus_sid, root, outbox = _focus_files(graph, env)
    map_port = _jobs(graph, env, runner, focus_sid, root, outbox, by_port, listeners)
    _tailscale(graph, env, runner, by_port, map_port)
    _dqr_scope(graph, env, runner)
    _scope_briefs(graph, env)
    return graph.finish()


def main() -> int:
    if sys.argv[1:] != ["--json"]:
        print("usage: fleet_map_infra.py --json", file=sys.stderr)
        return 2
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        print("snapshot too large", file=sys.stderr)
        return 2
    try:
        result = enrich(json.loads(raw))
    except (ValueError, TypeError, KeyError) as exc:
        print("invalid snapshot", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(result, separators=(",", ":")))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
