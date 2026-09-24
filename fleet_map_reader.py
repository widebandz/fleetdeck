"""Read the opt-in, metadata-only fleet snapshot from an external collector.

This module never reads panes, cards, routing files, or shell command strings.
The collector has a fixed argv contract; this boundary projects only fields the
map can show and retains the last valid snapshot in memory if collection fails.
"""

import copy
import datetime as dt
import json
import os
import re
import selectors
import subprocess
import threading
import time

SCHEMA = "agent-fleet.snapshot.v1"
COLLECTOR_ENV = "FLEETDECK_FLEET_SNAPSHOT"
DEFAULT_COLLECTOR = "~/bin/tm-fleet-snapshot"
MAX_STDOUT = 2 * 1024 * 1024
MAX_STDERR = 4096
TIMEOUT = 8.0
MIN_INTERVAL = 3.0
MAX_NODES = 2000
MAX_EDGES = 4000

_PRIVATE_VALUE = re.compile(
    r"(?:\b(?:\d{1,3}\.){3}\d{1,3}\b|\+?\d{10,}\b|"
    r"\b(?:imsg:)?chat[\s:-]*\d+\b|(?:/(?:Users|home|tmp|private|var|etc)/|~/|file://)|"
    r"(?<![A-Za-z0-9])/[A-Za-z0-9._-]+(?:/|$)|[A-Za-z]:\\|"
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b|"
    r"\b(?:sk-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{16,})\b)", re.I)
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
        if node_type not in {"host", "session", "window", "pane", "agent", "channel", "chat", "skill", "tool", "workspace", "file", "endpoint"}:
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
        if "reachability" in item:
            node["reachability"] = _safe_text(item["reachability"], limit=60)
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
                             "uses", "reads"}:
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
        if edge["evidence"] not in {"declared", "observed", "computed"}:
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


def _collector_output(executable):
    """Read bounded stdout/stderr from fixed argv without a shell or temp file."""
    argv = [os.path.expanduser(executable), "--host-id", "local", "--json"]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, close_fds=True)
    except OSError as exc:
        raise SnapshotError("collector unavailable") from exc
    sel = selectors.DefaultSelector()
    chunks = {"out": bytearray(), "err": bytearray()}
    deadline = time.monotonic() + TIMEOUT
    try:
        sel.register(proc.stdout, selectors.EVENT_READ, "out")
        sel.register(proc.stderr, selectors.EVENT_READ, "err")
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SnapshotError("collector timed out")
            for key, _ in sel.select(remaining):
                stream = key.fileobj
                data = os.read(stream.fileno(), 65536)
                if not data:
                    sel.unregister(stream)
                    continue
                target = chunks[key.data]
                target.extend(data)
                if len(target) > (MAX_STDOUT if key.data == "out" else MAX_STDERR):
                    raise SnapshotError("collector output exceeded limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SnapshotError("collector timed out")
        try:
            exit_code = proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise SnapshotError("collector timed out") from exc
        if exit_code != 0:
            raise SnapshotError("collector failed")
        try:
            return json.loads(chunks["out"].decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SnapshotError("collector returned invalid JSON") from exc
    finally:
        sel.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()


class FleetMapCache:
    def __init__(self, collector=None, clock=None):
        self.collector = collector or (lambda: _collector_output(
            os.environ.get(COLLECTOR_ENV) or DEFAULT_COLLECTOR))
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
               "routing_descriptions": "routing_conf", "chat_bindings": "chatbind"}
    return (any(ref == source or ref.startswith(source + ":") or ref.startswith(source + "#")
                or ref.startswith(aliases.get(source, "\x00")) for source in unavailable)
            or (("tmux" in unavailable or "routing_descriptions" in unavailable)
                and ref.startswith("imsg-router#")))


def _retain_unavailable_sources(current, previous, unavailable):
    """Keep missing prior facts dated and visibly stale during a source outage."""
    current = copy.deepcopy(current)
    prior_nodes = {node["id"]: node for node in previous["nodes"]}
    current_nodes = {node["id"]: node for node in current["nodes"]}
    for node_id, old in prior_nodes.items():
        affected = any(_from_unavailable(ref, unavailable) for ref in old["source_refs"])
        if not affected:
            continue
        if node_id not in current_nodes:
            kept = copy.deepcopy(old)
            kept["stale"] = True
            if "sessions_conf" in unavailable:
                kept["last_known_declared"] = kept["declared"]
                kept["declared"] = None
            if "tmux" in unavailable:
                kept["last_known_observed"] = kept["observed"]
                kept["observed"] = None
            elif kept["type"] in ("session", "window", "pane") and kept["observed"] is not None:
                # Current tmux inventory is authoritative for live presence even
                # when another source (such as identity cards) failed.
                kept["last_known_observed"] = kept["observed"]
                kept["observed"] = False
            current["nodes"].append(kept)
            current_nodes[node_id] = kept
        elif any(_from_unavailable(ref, unavailable) for ref in old["source_refs"]):
            now = current_nodes[node_id]
            if ({"identity", "identity_cards"} & unavailable) and "identity" in old:
                now["last_known_identity"] = copy.deepcopy(old["identity"])
                now.setdefault("stale_fields", []).append("identity")
            if ({"state", "state_cards"} & unavailable) and "state" in old:
                now["last_known_state"] = copy.deepcopy(old["state"])
                now.setdefault("stale_fields", []).append("state")
            if "sessions_conf" in unavailable and now["declared"] is None and old["declared"] is not None:
                now["last_known_declared"] = old["declared"]
            if "tmux" in unavailable and old["observed"] is not None:
                now["last_known_observed"] = old["observed"]
                now["observed"] = None
    current_edge_ids = {edge["id"] for edge in current["edges"]}
    for old in previous["edges"]:
        if old["id"] in current_edge_ids or not _from_unavailable(old["source"], unavailable):
            continue
        if old["from"] not in current_nodes or old["to"] not in current_nodes:
            continue
        kept = copy.deepcopy(old)
        kept["stale"] = True
        kept["freshness"]["status"] = "source_unavailable"
        current["edges"].append(kept)
    return current
