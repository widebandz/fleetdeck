#!/usr/bin/env python3
"""Optional read-only infrastructure facts for the fleet map.

Every relation is derived from an explicit local declaration or a bounded
observation. This module never emits addresses, raw paths, ports, chat IDs,
instruction contents, queued messages, or process command lines.
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
                     r"\b(?:sk-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{16,})\b|"
                     r"\b(?:api[_ -]?key|access[_ -]?token|secret|password|authorization)\s*[:=]\s*[^\s,;]+)", re.I)
SERVICE_GROUPS = {"fleet", "apps", "models", "data"}
SERVICE_KINDS = {"app", "api", "web"}


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


def enrich(raw: dict, env: dict[str, str] | None = None, runner=run_text) -> dict:
    graph = Graph(raw)
    env = os.environ if env is None else env
    by_port, listeners = _services(graph, env, runner)
    focus_sid, root, outbox = _focus_files(graph, env)
    map_port = _jobs(graph, env, runner, focus_sid, root, outbox, by_port, listeners)
    _tailscale(graph, env, runner, by_port, map_port)
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
