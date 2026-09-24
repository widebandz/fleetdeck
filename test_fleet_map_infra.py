#!/usr/bin/env python3
"""Synthetic, scratch-home tests for read-only fleet infrastructure projection."""

import copy
import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fleet_map_infra as I
import fleet_map_reader as R

TIME = "2026-09-24T00:00:00Z"


def snapshot():
    def node(ident, kind, label, parent=None):
        return {"id": ident, "type": kind, "label": label, "parent_id": parent,
                "declared": True, "observed": True, "source_refs": ["tmux"],
                "observed_at": TIME}
    return {"schema_version": I.SCHEMA, "collected_at": TIME,
            "nodes": [node("host:sample", "host", "Sample server"),
                      node("session:trace", "session", "trace", "host:sample"),
                      node("chat:bound-trace", "chat", "Bound chat channel")],
            "edges": [{"id": "chat_routes_to:trace", "from": "chat:bound-trace",
                       "to": "session:trace", "type": "chat_routes_to",
                       "evidence": "declared", "source": "chatbind",
                       "display": "Configured bound chat", "freshness": {"as_of": TIME,
                                                                "status": "configured-only"}}], "summary": {},
            "sources": [{"id": "tmux", "status": "available", "as_of": TIME}],
            "unknowns": []}


class InfrastructureTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name)
        self.root = self.base / "focus"
        self.root.mkdir()
        self.outbox = self.root / "outbox"
        self.outbox.mkdir()
        (self.root / "AGENTS.md").write_text("private instructions\n")
        (self.root / "CLAUDE.md").write_text("private instructions\n")
        self.sessions = self.base / "sessions.conf"
        self.sessions.write_text(f"trace={self.root}\n")
        self.memory = self.base / "memory" / "identity"
        self.memory.mkdir(parents=True)
        (self.memory / "trace.md").write_text(
            f"---\nsession: trace\nroot: {self.root}\nrouting_out:\n  - {self.outbox}\n"
            "chat_binding: imsg:chat-1\n---\n## Responsibilities\n\n"
            "- Interpret requests and route the work.\n"
            "- Carry results back to chat 1.\n"
            "- Write a concise outbox note.\n\n## Not this session\n\n- Other work.\n")
        self.services = self.base / "services.json"
        self.services.write_text(json.dumps({"services": [
            {"id": "chat", "name": "Chat API", "port": 8783, "group": "fleet"},
            {"id": "unsafe", "name": str(self.base / "secret"), "port": 9999,
             "group": "fleet"}]}))
        self.jobs = self.base / "LaunchAgents"
        self.jobs.mkdir()
        self.sender = self.base / "trace-outbox-send"
        self.sender.write_text(f'OUT="{self.outbox}"\nCHAT_ID="1"\nfiles=("$OUT"/*.txt)\n'
                               '"$IMSG" send --chat-id "$CHAT_ID" --service imessage --text "$p" --json\n')
        self.keeper = self.base / "trace-session-keep"
        self.keeper.write_text(f'SESSION="trace"\nROOT="{self.root}"\n'
                               '"$TMUX_BIN" new-session -d -s "$SESSION" -c "$ROOT" $AGENT\n')
        self.map_script = self.base / "portal_server.py"
        self.map_script.write_text("# synthetic only\n")
        self._plist("com.wideband.trace-outbox", [str(self.sender)])
        self._plist("com.wideband.trace-session", [str(self.keeper)])
        self.chatbind_script = self.base / "imsg-chatbind"
        self.chatbind_script.write_text('CONFIG_PATH = os.path.expanduser("~/.imsg-chatbind.json")\n'
                                        'with open(CONFIG_PATH)\nCFG        = load_cfg()\n'
                                        'b = bound_for(chat_id)\n'
                                        'deliver_to_session(b.get("session", "main"), text, chat_id)\n')
        self.chatbind_config = self.base / ".imsg-chatbind.json"
        self.chatbind_config.write_text(json.dumps({"bound": [{"chat_id": 1, "session": "trace"}]}))
        self._plist("ai.wideband.imsg.chatbind", [str(self.chatbind_script)], {"HOME": str(self.base)})
        self._plist("com.wideband.fleet-map-local", ["/usr/bin/python3", str(self.map_script)],
                    {"FLEETDECK_FLEET_MAP": "1", "FLEETDECK_PORT": "18790"})
        self.env = {"TM_SESSIONS_CONF": str(self.sessions),
                    "TM_MEMORY_DIR": str(self.memory.parent),
                    "FLEETDECK_FLEET_LAUNCHAGENTS_DIR": str(self.jobs),
                    "FLEETDECK_FLEET_SERVICES_FILE": str(self.services),
                    "FLEETDECK_FLEET_LSOF_CLI": "/fake/lsof",
                    "FLEETDECK_FLEET_LAUNCHCTL_CLI": "/fake/launchctl",
                    "FLEETDECK_FLEET_TAILSCALE_CLI": "/fake/tailscale"}
        self.env["TM_CHATBIND"] = str(self.chatbind_config)

    def _plist(self, label, argv, variables=None):
        raw = {"Label": label, "ProgramArguments": argv,
               "RunAtLoad": True, "EnvironmentVariables": variables or {}}
        (self.jobs / (label + ".plist")).write_bytes(plistlib.dumps(raw))

    def runner(self, argv, **_):
        if argv[0] == "/fake/lsof":
            return ("COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
                    "python 101 test 3u IPv4 0x0 0t0 TCP 127.0.0.1:18790 (LISTEN)\n"
                    "python 102 test 3u IPv4 0x0 0t0 TCP 127.0.0.1:8783 (LISTEN)\n")
        if argv[0] == "/fake/launchctl":
            return "PID Status Label\n101 0 com.wideband.trace-outbox\n"
        if argv[0] == "/fake/tailscale":
            return json.dumps({"TCP": {"18970": {"HTTPS": True}, "9000": {"HTTPS": True}},
                "Web": {"private.invalid:18970": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:18790"}}},
                        "private.invalid:9000": {"Handlers": {"/": {"Proxy": "http://10.0.0.1:8783"}}}}})
        return None

    def test_evidenced_layers_and_privacy(self):
        clean = R.validate_snapshot(I.enrich(snapshot(), self.env, self.runner))
        nodes = {n["id"]: n for n in clean["nodes"]}
        edges = {(e["type"], e["from"], e["to"]): e for e in clean["edges"]}
        self.assertIn("data:trace:outbox", nodes)
        self.assertIn("instruction:trace:agents.md", nodes)
        self.assertIn("service:fleet-map-local", nodes)
        self.assertIn("endpoint:tailnet-map", nodes)
        self.assertNotIn("service:unsafe", nodes)
        self.assertIn(("sends_chat", "job:trace-outbox", "chat:bound-trace"), edges)
        self.assertIn(("handles_bound_chat", "job:chatbind", "chat:bound-trace"), edges)
        duties = nodes["session:trace"]["responsibilities"]
        self.assertEqual(duties["source"], "infra:trace_identity")
        self.assertEqual(len(duties["items"]), 3)
        self.assertEqual(duties["items"][1], "Carry results back to bound chat.")
        self.assertIn(("launches", "job:trace-keeper", "session:trace"), edges)
        self.assertIn(("proxy_routes_to", "endpoint:tailnet-map", "service:fleet-map-local"), edges)
        self.assertIn(("schedules_job", "host:sample", "job:trace-outbox"), edges)
        self.assertEqual(edges[("sends_chat", "job:trace-outbox", "chat:bound-trace")]["layer"], "routing")
        self.assertEqual(edges[("proxy_routes_to", "endpoint:tailnet-map", "service:fleet-map-local")]["payload"], "HTTPS requests")
        serialized = json.dumps(clean)
        self.assertNotIn(str(self.base), serialized)
        self.assertNotIn("private.invalid", serialized)
        self.assertNotIn("imsg:chat-1", serialized)
        self.assertNotIn("private instructions", serialized)
        self.assertFalse(any(n["type"] == "device" or "phone" in n["label"].lower() for n in clean["nodes"]))

    def test_config_mismatch_does_not_infer_links(self):
        self.sender.write_text(self.sender.read_text().replace('CHAT_ID="1"', 'CHAT_ID="7"'))
        self.keeper.write_text(self.keeper.read_text().replace("new-session", "new-window"))
        clean = R.validate_snapshot(I.enrich(snapshot(), self.env, self.runner))
        edge_types = {e["type"] for e in clean["edges"]}
        self.assertNotIn("sends_chat", edge_types)
        self.assertNotIn("launches", {e["type"] for e in clean["edges"] if e["to"] == "session:trace"})

    def test_chatbind_path_and_binding_must_match_collector(self):
        self.chatbind_config.write_text(json.dumps({"bound": [{"chat_id": 1, "session": "other"}]}))
        clean = R.validate_snapshot(I.enrich(snapshot(), self.env, self.runner))
        self.assertFalse(any(e["type"] == "handles_bound_chat" for e in clean["edges"]))
        self.chatbind_config.write_text(json.dumps({"bound": [{"chat_id": 1, "session": "trace"}]}))
        self.env["TM_CHATBIND"] = str(self.base / "different-config.json")
        clean = R.validate_snapshot(I.enrich(snapshot(), self.env, self.runner))
        self.assertFalse(any(e["type"] == "handles_bound_chat" for e in clean["edges"]))

    def test_outbound_target_must_match_daemon_binding(self):
        self.chatbind_config.write_text(json.dumps({"bound": [{"chat_id": 2, "session": "trace"}]}))
        clean = R.validate_snapshot(I.enrich(snapshot(), self.env, self.runner))
        self.assertTrue(any(e["type"] == "handles_bound_chat" for e in clean["edges"]))
        self.assertFalse(any(e["type"] == "sends_chat" for e in clean["edges"]))

    def test_unsafe_responsibility_section_is_omitted(self):
        card = self.memory / "trace.md"
        card.write_text(card.read_text().replace("Write a concise outbox note.",
                                                 f"Read {self.base / 'private.txt'}."))
        clean = R.validate_snapshot(I.enrich(snapshot(), self.env, self.runner))
        session = next(n for n in clean["nodes"] if n["id"] == "session:trace")
        self.assertNotIn("responsibilities", session)
        self.assertNotIn(str(self.base), json.dumps(clean))

    def test_optional_tailnet_outage_keeps_other_evidence(self):
        def outage(argv, **kwargs):
            return None if argv[0] == "/fake/tailscale" else self.runner(argv, **kwargs)
        clean = R.validate_snapshot(I.enrich(snapshot(), self.env, outage))
        self.assertFalse(any(n["type"] == "endpoint" for n in clean["nodes"]))
        self.assertTrue(any(n["id"] == "data:trace:outbox" for n in clean["nodes"]))
        self.assertEqual(next(s for s in clean["sources"] if s["id"] == "infra:tailscale_serve")["status"], "unavailable")

    def test_reader_rejects_private_payload_and_invalid_layers(self):
        clean = I.enrich(snapshot(), self.env, self.runner)
        for patch in ({"payload": str(self.base / "secret")}, {"payload": "imsg:chat-9"},
                      {"layer": "private"}, {"layer": None}, {"payload": None}):
            bad = copy.deepcopy(clean)
            bad["edges"][-1].update(patch)
            with self.subTest(patch=patch), self.assertRaises(R.SnapshotError):
                R.validate_snapshot(bad)
        for unsafe in ({"items": ["Read /private/secret"], "source": "infra:trace_identity",
                        "as_of": TIME, "status": "declared"},
                       {"items": ["API_KEY=veryprivate"], "source": "infra:trace_identity",
                        "as_of": TIME, "status": "declared"},
                       {"items": ["Safe"], "source": "unknown", "as_of": TIME,
                        "status": "declared"}):
            bad = copy.deepcopy(clean)
            next(n for n in bad["nodes"] if n["id"] == "session:trace")["responsibilities"] = unsafe
            with self.assertRaises(R.SnapshotError):
                R.validate_snapshot(bad)

    def test_optional_stage_reads_stdin_and_failure_marks_only_infra(self):
        raw = snapshot()
        enriched = I.enrich(raw, self.env, self.runner)
        with mock.patch.dict(os.environ, {R.INFRA_ENV: "/fake/fleet-map-infra",
                                              R.HOST_ID_ENV: "sample"}, clear=True):
            with mock.patch.object(R, "_collector_output", return_value=raw):
                with mock.patch.object(R, "_bounded_json_process", return_value=enriched) as process:
                    self.assertEqual(R._collect_joined_snapshot(), enriched)
                    self.assertEqual(process.call_args.args[0], ["/fake/fleet-map-infra", "--json"])
                    self.assertEqual(json.loads(process.call_args.kwargs["input_bytes"]), raw)
                with mock.patch.object(R, "_bounded_json_process", side_effect=R.SnapshotError("secret failure")):
                    failed = R._collect_joined_snapshot()
        self.assertEqual(failed["nodes"], raw["nodes"])
        self.assertEqual(failed["sources"][-1]["id"], "infra")
        self.assertEqual(failed["sources"][-1]["status"], "unavailable")
        self.assertNotIn("secret failure", json.dumps(failed))

    def test_stage_outage_retains_dated_infra_facts_as_stale(self):
        prior = I.enrich(snapshot(), self.env, self.runner)
        current = snapshot()
        current["sources"].append({"id": "infra", "status": "unavailable", "as_of": TIME})
        current["unknowns"].append({"id": "unknown:infra", "kind": "source_unavailable",
                                    "source": "infra", "as_of": TIME,
                                    "detail": "Infrastructure metadata unavailable."})
        values = iter([prior, current])
        times = iter([0, 4])
        cache = R.FleetMapCache(collector=lambda: next(values), clock=lambda: next(times))
        cache.get()
        result = cache.get()[1]
        endpoint = next(n for n in result["nodes"] if n["id"] == "endpoint:tailnet-map")
        pipe = next(e for e in result["edges"] if e["type"] == "proxy_routes_to")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(endpoint["stale"])
        self.assertTrue(pipe["stale"])
        self.assertEqual(pipe["freshness"]["status"], "source_unavailable")
        session = next(n for n in result["nodes"] if n["id"] == "session:trace")
        self.assertEqual(session["last_known_responsibilities"]["items"][1],
                         "Carry results back to bound chat.")
        self.assertIn("responsibilities", session["stale_fields"])


if __name__ == "__main__":
    unittest.main()
