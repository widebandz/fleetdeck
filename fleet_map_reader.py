"""Read the opt-in, metadata-only fleet snapshot from an external collector.

This module never reads panes, cards, routing files, or shell command strings.
The collector has a fixed argv contract; this boundary projects only fields the
map can show and retains the last valid snapshot in memory if collection fails.
"""

import copy
import datetime as dt
import ipaddress
import json
import os
import re
import selectors
import subprocess
import threading
import time

SCHEMA = "agent-fleet.snapshot.v1"
COLLECTOR_ENV = "FLEETDECK_FLEET_SNAPSHOT"
REGISTRY_ENV = "FLEETDECK_FLEET_REGISTRY"
HOST_ID_ENV = "FLEETDECK_FLEET_HOST_ID"
REMOTE_ENV = "FLEETDECK_FLEET_REMOTE"
REMOTE_HOST_MAP_ENV = "FLEETDECK_FLEET_REMOTE_HOST_MAP"
REMOTE_CACHE_DIR_ENV = "FLEETDECK_FLEET_REMOTE_CACHE_DIR"
RUNTIME_ENV = "FLEETDECK_FLEET_RUNTIME"
INFRA_ENV = "FLEETDECK_FLEET_INFRA"
DEFAULT_COLLECTOR = "~/bin/tm-fleet-snapshot"
MAX_STDOUT = 2 * 1024 * 1024
MAX_STDERR = 4096
TIMEOUT = 8.0
REMOTE_TIMEOUT = 25.0
MIN_INTERVAL = 3.0
MAX_NODES = 2000
MAX_EDGES = 4000
DQR_SCOPE_KEYS = {"engagement_owner", "operator", "git_identity", "repository",
                  "chat_binding", "participants", "reply_approval", "media_publication",
                  "publish_approval", "runtime_scope", "deployment"}
DQR_SCOPE_VALUES = {
    "engagement_owner": re.compile(r"dailyquranreading\.com\s+[—-]\s+[A-Za-z][A-Za-z .'-]{1,60} engagement"),
    "operator": re.compile(r"Zayed \(configured operator\)"),
    "git_identity": re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39} \(repository Git user; credential not rechecked\)"),
    "repository": re.compile(r"GitHub haqzy/dailyquranreading · main"),
    "chat_binding": re.compile(r"DQR group chat configured; exact ID hidden"),
    "participants": re.compile(r"(?:[1-9]|1[0-2]) configured handles: (?:[0-9]|1[0-2]) operator aliases, (?:[0-9]|1[0-2]) other; handle-to-person mapping unverified"),
    "reply_approval": re.compile(r"External replies held for an exact operator command in a separate private chat"),
    "media_publication": re.compile(r"Incoming images are sanitized then auto-pushed by default"),
    "publish_approval": re.compile(r"Mismatch: card asks for live-site approval; media script auto-pushes"),
    "runtime_scope": re.compile(r"(?:Observed pane outside card root; DQR absent from session standard|DQR absent from session standard; pane placement unknown or separate)"),
    "deployment": re.compile(r"Vercel project linked locally; production release unobserved"),
}
DQR_SCOPE_EVIDENCE = {"engagement_owner": "declared", "operator": "declared",
                      "git_identity": "checked", "repository": "checked",
                      "chat_binding": "declared", "participants": "checked",
                      "reply_approval": "declared", "media_publication": "declared",
                      "publish_approval": "mismatch", "runtime_scope": "checked",
                      "deployment": "declared"}

_PRIVATE_VALUE = re.compile(
    r"(?:\b(?:\d{1,3}\.){3}\d{1,3}\b|\+?\d{10,}\b|"
    r"\b(?:imsg:)?chat[\s:-]*\d+\b|(?:/(?:Users|home|tmp|private|var|etc)/|~/|file://)|"
    r"(?<![A-Za-z0-9])/[A-Za-z0-9._-]+(?:/|$)|[A-Za-z]:\\|"
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b|"
    r"\b(?:sk-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|vck_[A-Za-z0-9]{16,})\b|"
    r"\b(?:api[_ -]?key|access[_ -]?token|secret|password|authorization)\s*[:=]\s*[^\s,;]+)", re.I)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._%#-]{0,159}$")


class SnapshotError(Exception):
    """A collector failed or returned data unsafe for the browser."""


def _utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_text(value, *, limit=240):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit or any(c in value for c in "\r\n\x00"):
        raise SnapshotError("invalid text")
    if _PRIVATE_VALUE.search(value):
        raise SnapshotError("private value")
    return value


def _safe_id(value):
    value = _safe_text(value, limit=160)
    if not value or not _ID.fullmatch(value):
        raise SnapshotError("invalid id")
    return value


def _optional_bool(value):
    if value is None or isinstance(value, bool):
        return value
    raise SnapshotError("invalid observation flag")


def _small_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1000000 else None


def _project_map(value, text_keys=(), bool_keys=(), int_keys=()):
    if not isinstance(value, dict):
        return {}
    out = {}
    for key in text_keys:
        if key in value:
            out[key] = _safe_text(value[key])
    for key in bool_keys:
        if key in value:
            out[key] = _optional_bool(value[key])
    for key in int_keys:
        if key in value:
            n = _small_int(value[key])
            if n is not None:
                out[key] = n
    return out


def validate_snapshot(raw):
    """Allowlist the browser payload; reject malformed or private values."""
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA:
        raise SnapshotError("unsupported schema")
    if not isinstance(raw.get("nodes"), list) or not isinstance(raw.get("edges"), list):
        raise SnapshotError("missing graph")
    if len(raw["nodes"]) > MAX_NODES or len(raw["edges"]) > MAX_EDGES:
        raise SnapshotError("graph too large")
    collected_at = _safe_text(raw.get("collected_at"), limit=40)
    if not collected_at:
        raise SnapshotError("missing collection time")

    nodes = []
    ids = set()
    for item in raw["nodes"]:
        if not isinstance(item, dict):
            raise SnapshotError("invalid node")
        node_id = _safe_id(item.get("id"))
        if node_id in ids:
            raise SnapshotError("duplicate node")
        ids.add(node_id)
        node_type = _safe_text(item.get("type"), limit=40)
        if node_type not in {"host", "session", "window", "pane", "agent", "channel", "chat", "skill", "tool", "workspace", "file", "endpoint", "service", "job", "data", "instruction"}:
            raise SnapshotError("unknown node type")
        refs = item.get("source_refs") or []
        if not isinstance(refs, list) or len(refs) > 20:
            raise SnapshotError("invalid sources")
        node = {
            "id": node_id,
            "type": node_type,
            "label": _safe_text(item.get("label"), limit=100),
            "parent_id": _safe_id(item["parent_id"]) if item.get("parent_id") is not None else None,
            "declared": _optional_bool(item.get("declared")),
            "observed": _optional_bool(item.get("observed")),
            "source_refs": [_safe_text(ref, limit=100) for ref in refs],
            "observed_at": _safe_text(item.get("observed_at"), limit=40),
        }
        if "stale" in item:
            node["stale"] = _optional_bool(item["stale"])
        if "last_known_observed" in item:
            node["last_known_observed"] = _optional_bool(item["last_known_observed"])
        if "reachability" in item:
            node["reachability"] = _safe_text(item["reachability"], limit=60)
        for field in ("kind", "group", "reach", "transport", "status"):
            if field in item:
                node[field] = _safe_text(item[field], limit=60)
        if not node["label"]:
            raise SnapshotError("missing label")
        identity = _project_map(item.get("identity"),
                                text_keys=("role", "card_updated_date", "stable_session_id", "verified_agent_id"),
                                bool_keys=("card_present",))
        if identity:
            if identity.get("role") not in (None, "assigned", "unassigned", "unknown"):
                raise SnapshotError("invalid role")
            node["identity"] = identity
        runtime = _project_map(item.get("runtime"),
                               text_keys=("created_at", "last_activity_at"),
                               int_keys=("window_count", "pane_count", "attached_clients"))
        if runtime:
            node["runtime"] = runtime
        process = _project_map(item.get("process"),
                               text_keys=("classification", "engine", "display", "basis"))
        if process:
            if process.get("classification") not in (None, "recognized_engine", "agent_like_process", "agent", "shell", "unknown"):
                raise SnapshotError("invalid process class")
            node["process"] = process
        state = _project_map(item.get("state"), text_keys=("status", "updated_date"))
        if state:
            if state.get("status") not in (None, "idle", "working", "blocked", "awaiting-approval", "handoff", "retired", "unknown"):
                raise SnapshotError("invalid state")
            node["state"] = state
        standard = _project_map(item.get("standard"), text_keys=("source_mtime",),
                                bool_keys=("present",))
        if standard:
            node["standard"] = standard
        root_evidence = _project_map(item.get("root_evidence"),
                                     text_keys=("as_of",),
                                     bool_keys=("identity_matches_standard",
                                                "card_vs_standard_match",
                                                "card_vs_standard_agree",
                                                "observed_cwd_within_card",
                                                "observed_cwd_within_standard"))
        root_refs = (item.get("root_evidence") or {}).get("source_refs") if isinstance(item.get("root_evidence"), dict) else None
        if root_refs is not None:
            if not isinstance(root_refs, list) or len(root_refs) > 10:
                raise SnapshotError("invalid root sources")
            root_evidence["source_refs"] = [_safe_text(ref, limit=100) for ref in root_refs]
        if root_evidence:
            node["root_evidence"] = root_evidence
        if "responsibilities" in item:
            if node_type != "session" or not isinstance(item["responsibilities"], dict):
                raise SnapshotError("invalid responsibilities")
            duties = item["responsibilities"]
            entries = duties.get("items")
            if not isinstance(entries, list) or not 1 <= len(entries) <= 3:
                raise SnapshotError("invalid responsibilities")
            entries = [_safe_text(entry, limit=140) for entry in entries]
            if any(not entry for entry in entries):
                raise SnapshotError("invalid responsibilities")
            duty_source = duties.get("source")
            if duty_source not in ({"infra:trace_identity", "infra:dqr_identity"}
                                    if node_id == "session:DQR" else {"infra:trace_identity"}) or duties.get("status") != "declared":
                raise SnapshotError("invalid responsibilities source")
            as_of = _safe_text(duties.get("as_of"), limit=40)
            if not as_of:
                raise SnapshotError("invalid responsibilities time")
            node["responsibilities"] = {"items": entries, "source": duty_source,
                                        "as_of": as_of, "status": "declared"}
        if "scope" in item:
            if node_id != "session:DQR" or node_type != "session" or not isinstance(item["scope"], dict):
                raise SnapshotError("invalid DQR scope")
            raw_facts = item["scope"].get("facts")
            if not isinstance(raw_facts, list) or not 1 <= len(raw_facts) <= len(DQR_SCOPE_KEYS):
                raise SnapshotError("invalid DQR scope facts")
            facts, keys = [], set()
            for raw_fact in raw_facts:
                if not isinstance(raw_fact, dict):
                    raise SnapshotError("invalid DQR scope fact")
                key = raw_fact.get("key")
                if key not in DQR_SCOPE_KEYS or key in keys:
                    raise SnapshotError("invalid DQR scope key")
                keys.add(key)
                value = _safe_text(raw_fact.get("value"), limit=140)
                evidence = raw_fact.get("evidence")
                source = _safe_text(raw_fact.get("source"), limit=100)
                as_of = _safe_text(raw_fact.get("as_of"), limit=40)
                if (not value or not DQR_SCOPE_VALUES[key].fullmatch(value)
                        or evidence != DQR_SCOPE_EVIDENCE[key] or not source or not as_of):
                    raise SnapshotError("invalid DQR scope provenance")
                facts.append({"key": key, "value": value, "evidence": evidence,
                              "source": source, "as_of": as_of})
            node["scope"] = {"facts": facts}
        if node_type in ("session", "agent"):
            registry = _project_map(item.get("registry"),
                                    text_keys=("agent_id", "host_id", "session_name", "state"))
            if registry:
                if registry.get("state") not in ("planned", "verified", "retired"):
                    raise SnapshotError("invalid registry binding state")
                node["registry"] = registry
        elif node_type == "workspace" and "registry" in item:
            registry = _project_map(item.get("registry"),
                                    text_keys=("host_id", "owner_session", "access"),
                                    int_keys=("ordinal",))
            if registry.get("access") not in ("exclusive", "shared"):
                raise SnapshotError("invalid path claim access")
            node["registry"] = registry
        nodes.append(node)
    for node in nodes:
        if node["parent_id"] is not None and node["parent_id"] not in ids:
            raise SnapshotError("orphan node")

    edges = []
    edge_ids = set()
    for item in raw["edges"]:
        if not isinstance(item, dict):
            raise SnapshotError("invalid edge")
        edge_id = _safe_id(item.get("id"))
        if edge_id in edge_ids:
            raise SnapshotError("duplicate edge")
        edge_ids.add(edge_id)
        source, target = _safe_id(item.get("from")), _safe_id(item.get("to"))
        if source not in ids or target not in ids:
            raise SnapshotError("unknown edge endpoint")
        edge_type = _safe_text(item.get("type"), limit=60)
        if edge_type not in {"handoff", "hands_off_to", "uses_tool", "uses_skill",
                             "uses_workspace", "reads_file", "executes_file",
                             "chat_routes_to", "describes_session", "bound_chat",
                             "router_addressable", "router_agent_pane_ready",
                             "planned_binding", "verified_binding", "path_claim", "occupies",
                             "work_lease", "uses", "reads", "declares_service", "runs_service",
                             "schedules_job", "launches", "has_instruction_file", "writes_queue",
                             "consumes_queue", "sends_chat", "handles_bound_chat", "proxy_routes_to",
                             "holds_draft", "approves_draft", "releases_reply", "stages_media",
                             "writes_asset", "invokes_tool", "pushes_to", "triggers_deploy"}:
            raise SnapshotError("unknown edge type")
        freshness = _project_map(item.get("freshness"),
                                 text_keys=("as_of", "source_mtime", "status"))
        if not freshness.get("status") or not (freshness.get("as_of") or freshness.get("source_mtime")):
            raise SnapshotError("missing edge freshness")
        edge_source = _safe_text(item.get("source"), limit=100)
        if not edge_source:
            raise SnapshotError("missing edge source")
        edge = {
            "id": edge_id, "from": source, "to": target,
            "type": edge_type,
            "evidence": _safe_text(item.get("evidence"), limit=30),
            "source": edge_source,
            "display": _safe_text(item.get("display"), limit=140),
            "freshness": freshness,
        }
        infra_types = {"declares_service", "runs_service", "schedules_job", "launches",
                       "has_instruction_file", "writes_queue", "consumes_queue",
                       "sends_chat", "handles_bound_chat", "proxy_routes_to",
                       "holds_draft", "approves_draft", "releases_reply", "stages_media",
                       "writes_asset", "invokes_tool", "pushes_to", "triggers_deploy"}
        if "layer" in item or edge_type in infra_types:
            layer = _safe_text(item.get("layer"), limit=30)
            if layer not in {"identity", "routing", "runtime", "capabilities", "instructions",
                             "services", "network", "state", "deployment"}:
                raise SnapshotError("invalid edge layer")
            edge["layer"] = layer
        if "payload" in item or edge_type in infra_types:
            payload = _safe_text(item.get("payload"), limit=100)
            if edge_type in infra_types and not payload:
                raise SnapshotError("missing edge payload")
            edge["payload"] = payload
        if edge_type == "path_claim":
            if item.get("access") not in ("exclusive", "shared"):
                raise SnapshotError("invalid path claim access")
            edge["access"] = item["access"]
        if "stale" in item:
            edge["stale"] = _optional_bool(item["stale"])
        if edge["evidence"] not in {"declared", "observed", "computed",
                                    "approved_registry_binding", "approved_registry_claim",
                                    "launcher_attested"}:
            raise SnapshotError("invalid evidence")
        edges.append(edge)

    summary = {}
    if isinstance(raw.get("summary"), dict):
        for key, value in raw["summary"].items():
            if isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key):
                n = _small_int(value)
                if n is not None:
                    summary[key] = n
        health = raw["summary"].get("source_health")
        if health in ("complete", "degraded"):
            summary["source_health"] = health

    sources = []
    for item in raw.get("sources") or []:
        if not isinstance(item, dict) or len(sources) >= 40:
            raise SnapshotError("invalid source list")
        source = _project_map(item, text_keys=("id", "status", "as_of", "source_mtime"))
        if source.get("status") not in ("available", "unavailable", "partial") or not source.get("id"):
            raise SnapshotError("invalid source status")
        sources.append(source)

    unknowns = []
    for item in raw.get("unknowns") or []:
        if not isinstance(item, dict) or len(unknowns) >= 100:
            raise SnapshotError("invalid unknowns")
        unknowns.append(_project_map(item,
                                     text_keys=("id", "kind", "source", "as_of", "detail")))
    return {"schema_version": SCHEMA, "collected_at": collected_at,
            "nodes": nodes, "edges": edges, "summary": summary,
            "sources": sources, "unknowns": unknowns}


def _bounded_json_process(argv, *, input_bytes=None, source="source", timeout=TIMEOUT):
    """Run a fixed argv with bounded pipes, no shell, and no private temp file."""
    if input_bytes is not None and len(input_bytes) > MAX_STDOUT:
        raise SnapshotError("source input exceeded limit")
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                close_fds=True)
    except OSError as exc:
        raise SnapshotError(source + " unavailable") from exc
    sel = selectors.DefaultSelector()
    chunks = {"out": bytearray(), "err": bytearray()}
    sent = 0
    deadline = time.monotonic() + timeout
    try:
        sel.register(proc.stdout, selectors.EVENT_READ, "out")
        sel.register(proc.stderr, selectors.EVENT_READ, "err")
        if input_bytes is not None:
            os.set_blocking(proc.stdin.fileno(), False)
            sel.register(proc.stdin, selectors.EVENT_WRITE, "in")
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SnapshotError(source + " timed out")
            for key, _ in sel.select(remaining):
                stream = key.fileobj
                if key.data == "in":
                    try:
                        sent += os.write(stream.fileno(), input_bytes[sent:sent + 65536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        sent = len(input_bytes)
                    if sent >= len(input_bytes):
                        sel.unregister(stream)
                        stream.close()
                    continue
                data = os.read(stream.fileno(), 65536)
                if not data:
                    sel.unregister(stream)
                    continue
                target = chunks[key.data]
                target.extend(data)
                if len(target) > (MAX_STDOUT if key.data == "out" else MAX_STDERR):
                    raise SnapshotError(source + " output exceeded limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SnapshotError(source + " timed out")
        try:
            exit_code = proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise SnapshotError(source + " timed out") from exc
        if exit_code != 0:
            raise SnapshotError(source + " failed")
        try:
            return json.loads(chunks["out"].decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SnapshotError(source + " returned invalid JSON") from exc
    finally:
        sel.close()
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def _fleet_host_id():
    host_id = os.environ.get(HOST_ID_ENV) or "local"
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", host_id):
        raise SnapshotError("invalid fleet host ID")
    try:
        ipaddress.ip_address(host_id)
    except ValueError:
        pass
    else:
        raise SnapshotError("fleet host ID cannot be an address")
    return host_id


def _collector_output(executable):
    """Read metadata-only collector output using its fixed command contract."""
    host_id = _fleet_host_id()
    return _bounded_json_process([os.path.expanduser(executable), "--host-id", host_id, "--json"],
                                 source="collector")


def _source_unavailable(raw, source_id, detail):
    raw = copy.deepcopy(raw)
    stamp = _utc_now()
    raw.setdefault("sources", []).append({"id": source_id, "status": "unavailable",
                                           "as_of": stamp})
    raw.setdefault("unknowns", []).append({"id": "unknown:" + source_id,
                                            "kind": "source_unavailable", "source": source_id,
                                            "as_of": stamp, "detail": detail})
    raw.setdefault("summary", {})["source_health"] = "degraded"
    return raw


def _pipe_json(raw):
    return json.dumps(raw, separators=(",", ":")).encode("utf-8")


def _collect_joined_snapshot():
    """Build the opt-in read pipeline without a private snapshot temp file."""
    raw = _collector_output(os.environ.get(COLLECTOR_ENV) or DEFAULT_COLLECTOR)
    remote_executable = os.environ.get(REMOTE_ENV)
    if remote_executable:
        host_map = os.environ.get(REMOTE_HOST_MAP_ENV)
        cache_dir = os.environ.get(REMOTE_CACHE_DIR_ENV)
        if not all(path and os.path.isabs(path) for path in (remote_executable, host_map, cache_dir)):
            raw = _source_unavailable(raw, "remote_hosts", "Remote host map is unavailable.")
        else:
            try:
                raw = _bounded_json_process([remote_executable, "--host-map", host_map,
                                             "--cache-dir", cache_dir, "--all",
                                             "--local-snapshot", "-", "--local-host-id",
                                             _fleet_host_id(), "--json"],
                                            input_bytes=_pipe_json(raw), source="remote",
                                            timeout=REMOTE_TIMEOUT)
            except (SnapshotError, OSError, ValueError, TypeError):
                raw = _source_unavailable(raw, "remote_hosts", "Remote metadata unavailable.")
    registry_executable = os.environ.get(REGISTRY_ENV)
    if registry_executable:
        if not os.path.isabs(registry_executable):
            raw = _source_unavailable(raw, "registry", "Registry join unavailable.")
        else:
            try:
                raw = _bounded_json_process([registry_executable, "join", "--snapshot", "-", "--json"],
                                            input_bytes=_pipe_json(raw), source="registry")
            except (SnapshotError, OSError, ValueError, TypeError):
                raw = _source_unavailable(raw, "registry", "Registry join unavailable.")
    runtime_executable = os.environ.get(RUNTIME_ENV)
    if runtime_executable:
        if not os.path.isabs(runtime_executable):
            raw = _source_unavailable(raw, "occupant_receipts", "Runtime receipt check unavailable.")
            raw = _source_unavailable(raw, "work_leases", "Work lease check unavailable.")
        else:
            try:
                raw = _bounded_json_process([runtime_executable, "project", "--snapshot", "-",
                                             "--host-id", _fleet_host_id(), "--json"],
                                            input_bytes=_pipe_json(raw), source="runtime")
            except (SnapshotError, OSError, ValueError, TypeError):
                raw = _source_unavailable(raw, "occupant_receipts", "Runtime receipt check unavailable.")
                raw = _source_unavailable(raw, "work_leases", "Work lease check unavailable.")
    infra_executable = os.environ.get(INFRA_ENV)
    if infra_executable:
        if not os.path.isabs(infra_executable):
            raw = _source_unavailable(raw, "infra", "Infrastructure metadata unavailable.")
        else:
            try:
                raw = _bounded_json_process([infra_executable, "--json"],
                                            input_bytes=_pipe_json(raw), source="infrastructure")
            except (SnapshotError, OSError, ValueError, TypeError):
                raw = _source_unavailable(raw, "infra", "Infrastructure metadata unavailable.")
    return raw


class FleetMapCache:
    def __init__(self, collector=None, clock=None):
        self.collector = collector or _collect_joined_snapshot
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self.last_good = None
        self.last_response = None
        self.last_attempt = None

    def get(self):
        with self.lock:
            now = self.clock()
            if self.last_response is not None and self.last_attempt is not None and now - self.last_attempt < MIN_INTERVAL:
                return copy.deepcopy(self.last_response)
            self.last_attempt = now
            try:
                snapshot = validate_snapshot(self.collector())
            except (SnapshotError, OSError, ValueError, TypeError):
                if self.last_good is None:
                    result = (503, {"status": "source_unavailable", "schema_version": SCHEMA,
                                    "collected_at": None, "nodes": [], "edges": [],
                                    "summary": {}, "unknowns": [{"kind": "source_unavailable",
                                                                    "source": "collector",
                                                                    "detail": "The fleet snapshot could not be collected."}]})
                else:
                    stale = copy.deepcopy(self.last_good)
                    stale["status"] = "stale"
                    stale["unknowns"].append({"kind": "source_unavailable", "source": "collector",
                                              "detail": "The latest refresh failed; showing the last collected snapshot."})
                    result = (200, stale)
            else:
                unavailable = {item.get("source") for item in snapshot["unknowns"]
                               if item.get("kind") == "source_unavailable" and item.get("source")}
                unavailable.update(source["id"] for source in snapshot["sources"]
                                   if source["status"] in ("unavailable", "partial"))
                if unavailable and self.last_good is not None:
                    snapshot = _retain_unavailable_sources(snapshot, self.last_good, unavailable)
                snapshot["status"] = ("partial" if unavailable or
                                      snapshot["summary"].get("source_health") == "degraded"
                                      else "fresh")
                self.last_good = copy.deepcopy(snapshot)
                result = (200, snapshot)
            self.last_response = copy.deepcopy(result)
            return result


fleet_map_cache = FleetMapCache()


def _from_unavailable(ref, unavailable):
    aliases = {"identity_cards": "identity:", "state_cards": "state:",
               "routing_descriptions": "routing_conf", "chat_bindings": "chatbind",
               "remote_hosts": "remote:"}
    return (any(ref == source or ref.startswith(source + ":") or ref.startswith(source + "#")
                or ref.startswith(aliases.get(source, "\x00"))
                or (source.startswith("remote:") and source.endswith(":transport")
                    and ref.startswith(source.rsplit(":", 1)[0] + ":"))
                for source in unavailable)
            or (("tmux" in unavailable or "routing_descriptions" in unavailable)
                and ref.startswith("imsg-router#")))


def _retain_unavailable_sources(current, previous, unavailable):
    """Keep missing prior facts dated and visibly stale during a source outage."""
    current = copy.deepcopy(current)
    prior_nodes = {node["id"]: node for node in previous["nodes"]}
    current_nodes = {node["id"]: node for node in current["nodes"]}
    for node_id, old in prior_nodes.items():
        affected = (any(_from_unavailable(ref, unavailable) for ref in old["source_refs"])
                    or ("registry" in unavailable and "registry" in old))
        if not affected:
            continue
        if old["type"] == "workspace" and "work_leases" in old["source_refs"]:
            continue
        remote_lost = any(ref.startswith("remote:") and
                          ("remote_hosts" in unavailable or any(
                              source.startswith("remote:") and source.endswith(":transport")
                              and ref.startswith(source.rsplit(":", 1)[0] + ":")
                              for source in unavailable))
                          for ref in old["source_refs"])
        if node_id not in current_nodes:
            kept = copy.deepcopy(old)
            kept["stale"] = True
            if "sessions_conf" in unavailable:
                kept["last_known_declared"] = kept["declared"]
                kept["declared"] = None
            if "tmux" in unavailable or remote_lost:
                kept["last_known_observed"] = kept["observed"]
                kept["observed"] = None
            elif kept["type"] in ("session", "window", "pane") and kept["observed"] is not None:
                # Current tmux inventory is authoritative for live presence even
                # when another source (such as identity cards) failed.
                kept["last_known_observed"] = kept["observed"]
                kept["observed"] = False
            current["nodes"].append(kept)
            current_nodes[node_id] = kept
        elif affected:
            now = current_nodes[node_id]
            if ({"identity", "identity_cards"} & unavailable) and "identity" in old:
                now["last_known_identity"] = copy.deepcopy(old["identity"])
                now.setdefault("stale_fields", []).append("identity")
            if ({"state", "state_cards"} & unavailable) and "state" in old:
                now["last_known_state"] = copy.deepcopy(old["state"])
                now.setdefault("stale_fields", []).append("state")
            if "registry" in unavailable and "registry" in old:
                now["last_known_registry"] = copy.deepcopy(old["registry"])
                now.setdefault("stale_fields", []).append("registry")
            if ({"infra", "infra:trace_identity", "infra:dqr_identity"} & unavailable) and "responsibilities" in old:
                now["last_known_responsibilities"] = copy.deepcopy(old["responsibilities"])
                now.setdefault("stale_fields", []).append("responsibilities")
            if "sessions_conf" in unavailable and now["declared"] is None and old["declared"] is not None:
                now["last_known_declared"] = old["declared"]
            if "tmux" in unavailable and old["observed"] is not None:
                now["last_known_observed"] = old["observed"]
                now["observed"] = None
            if remote_lost and old["observed"] is not None:
                now["last_known_observed"] = old["observed"]
                now["observed"] = None
                if old.get("reachability"):
                    now["last_known_reachability"] = old["reachability"]
                    now.setdefault("stale_fields", []).append("reachability")
    current_edge_ids = {edge["id"] for edge in current["edges"]}
    for old in previous["edges"]:
        if old["type"] in ("occupies", "work_lease"):
            # A dated receipt or lease is never a current occupant/lock fact.
            continue
        if old["id"] in current_edge_ids or not _from_unavailable(old["source"], unavailable):
            continue
        if old["from"] not in current_nodes or old["to"] not in current_nodes:
            continue
        kept = copy.deepcopy(old)
        kept["stale"] = True
        kept["freshness"]["status"] = "source_unavailable"
        current["edges"].append(kept)
    return current
