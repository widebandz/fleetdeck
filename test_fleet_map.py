#!/usr/bin/env python3
"""Offline contract tests for the read-only fleet map bridge and routes."""

import copy
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

os.environ.setdefault("FLEETDECK_HOST", "sample.invalid")

import fleet_map_reader as R
import portal_server as P

TIME = "2026-09-24T00:00:00Z"


def node(id, type, label, parent_id=None, refs=None, **extra):
    return {"id": id, "type": type, "label": label, "parent_id": parent_id,
            "declared": True if type in ("host", "session") else False,
            "observed": type in ("host", "session", "window", "pane"),
            "source_refs": refs or ["tmux"], "observed_at": TIME, **extra}


def edge(id, source, target, type="hands_off_to", evidence="declared", ref="state:sample"):
    return {"id": id, "from": source, "to": target, "type": type,
            "evidence": evidence, "source": ref,
            "freshness": {"as_of": TIME, "status": "available"}, "display": "Sample link"}


def snapshot():
    return {"schema_version": R.SCHEMA, "collected_at": TIME,
            "nodes": [node("host:sample", "host", "Sample device", refs=["tmux"]),
                      node("session:sample", "session", "Sample session", "host:sample",
                           refs=["tmux", "identity:sample", "sessions_conf"],
                           identity={"role": "assigned", "card_present": True},
                           root_evidence={"card_vs_standard_match": None,
                                          "observed_cwd_within_card": True,
                                          "observed_cwd_within_standard": None,
                                          "source_refs": ["identity_cards", "tmux"],
                                          "as_of": TIME},
                           standard={"present": True, "source_mtime": TIME}),
                      node("pane:sample:%1", "pane", "Pane 1", "session:sample",
                           process={"classification": "agent_like_process", "display": "Generic process", "basis": "foreground command"}),
                      node("file:sample", "file", "Script file", refs=["identity:sample"], observed=None)],
            "edges": [edge("exec:sample", "session:sample", "file:sample", "executes_file", ref="identity:sample")],
            "summary": {"live_sessions": 1, "source_health": "complete"},
            "sources": [{"id": "tmux", "status": "available", "as_of": TIME, "source_mtime": None}],
            "unknowns": []}


class ValidationTests(unittest.TestCase):
    def test_dqr_scope_is_bounded_and_rejects_private_facts(self):
        value = snapshot()
        dqr = node("session:DQR", "session", "DQR", "host:sample", refs=["tmux", "infra:dqr_identity"],
                   scope={"facts": [{"key": "chat_binding",
                                      "value": "DQR bound chat configured; local ID shown on channel",
                                      "evidence": "checked", "source": "chatbind+DQR-card", "as_of": TIME,
                                      "private_note": "+15555550103"},
                                     {"key": "requester",
                                      "value": "Imam El named as client contact; handle-to-person mapping unverified",
                                      "evidence": "declared", "source": "identity:DQR#owner+chatbind#label",
                                      "as_of": TIME}]})
        value["nodes"].append(dqr)
        value["nodes"].append(node("chat:bound-DQR", "chat", "Bound chat → DQR",
                                   refs=["chatbind", "infra:dqr_chatbind"], observed=None,
                                   local_chat_id=19))
        value["edges"].append({"id": "chat_routes_to:dqr", "from": "chat:bound-DQR", "to": "session:DQR",
                               "type": "chat_routes_to", "evidence": "declared", "source": "chatbind",
                               "layer": "routing", "payload": "operator instructions; external text held outside DQR",
                               "display": "Operator text targets DQR; external text is held",
                               "freshness": {"as_of": TIME, "status": "configured-only"}})
        clean = R.validate_snapshot(value)
        projected = next(n for n in clean["nodes"] if n["id"] == "session:DQR")
        self.assertEqual(projected["scope"]["facts"][0]["key"], "chat_binding")
        self.assertEqual(clean["nodes"][-1]["local_chat_id"], 19)
        self.assertNotIn("private_note", json.dumps(clean))
        self.assertEqual(clean["edges"][-1]["type"], "chat_routes_to")
        for private in ("chat 19", "imsg:chat-19", "+15555550103", "+1 (555) 555-0103",
                        "zayed@example.invalid", "vck_0123456789abcdefghijklmnop", "/tmp/private"):
            bad = copy.deepcopy(value)
            bad["nodes"][-2]["scope"]["facts"][0]["value"] = private
            with self.subTest(private=private), self.assertRaises(R.SnapshotError):
                R.validate_snapshot(bad)
        for patch in ({"key": "unknown"}, {"evidence": "observed"}, {"source": ""}, {"as_of": ""}):
            bad = copy.deepcopy(value)
            bad["nodes"][-2]["scope"]["facts"][0].update(patch)
            with self.subTest(patch=patch), self.assertRaises(R.SnapshotError):
                R.validate_snapshot(bad)
        bad = copy.deepcopy(value)
        bad["nodes"][-2]["scope"]["facts"].append(copy.deepcopy(bad["nodes"][-2]["scope"]["facts"][0]))
        with self.assertRaises(R.SnapshotError):
            R.validate_snapshot(bad)
        for invalid in (-1, 0, True, "19", 1_000_001):
            bad = copy.deepcopy(value)
            bad["nodes"][-1]["local_chat_id"] = invalid
            with self.subTest(local_chat_id=invalid), self.assertRaises(R.SnapshotError):
                R.validate_snapshot(bad)
        bad = copy.deepcopy(value)
        bad["nodes"][-1]["source_refs"] = ["chatbind"]
        with self.assertRaises(R.SnapshotError):
            R.validate_snapshot(bad)
        bad = copy.deepcopy(value)
        bad["nodes"][-1]["id"] = "chat:other"
        bad["edges"][-1]["from"] = "chat:other"
        with self.assertRaises(R.SnapshotError):
            R.validate_snapshot(bad)

    def test_registry_binding_is_projected_without_private_extra_fields(self):
        value = snapshot()
        value["nodes"][1]["registry"] = {"agent_id": "agent-moss", "state": "planned",
                                           "host_id": "sample", "session_name": "Sample session",
                                           "private_note": "do not expose"}
        value["nodes"].append(node("agent:agent-moss", "agent", "agent-moss", refs=["registry"],
                                   observed=None, registry={"agent_id": "agent-moss",
                                                             "state": "planned", "host_id": "sample",
                                                             "session_name": "Sample session"}))
        value["edges"].append({**edge("binding:moss", "agent:agent-moss", "session:sample",
                                      "planned_binding", "approved_registry_binding", "registry"),
                               "private_note": "do not expose"})
        value["summary"]["registry_revision"] = 3
        value["sources"].append({"id": "registry", "status": "available", "as_of": TIME})
        clean = R.validate_snapshot(value)
        self.assertEqual(clean["summary"]["registry_revision"], 3)
        self.assertEqual(clean["nodes"][1]["registry"]["agent_id"], "agent-moss")
        self.assertNotIn("private_note", json.dumps(clean))
        self.assertEqual(clean["edges"][-1]["evidence"], "approved_registry_binding")

    def test_remote_stale_markers_and_runtime_receipt_are_projected(self):
        value = snapshot()
        value["nodes"][1]["stale"] = True
        value["nodes"][1]["last_known_observed"] = True
        value["nodes"][1]["observed"] = None
        value["nodes"].append(node("agent:sample", "agent", "Sample agent", refs=["registry"],
                                   observed=None, registry={"agent_id": "sample", "state": "verified",
                                                             "host_id": "sample", "session_name": "Sample session"}))
        value["edges"].append({**edge("occupies:sample", "agent:sample", "pane:sample:%1",
                                      "occupies", "launcher_attested", "occupant_receipts"),
                               "stale": False})
        clean = R.validate_snapshot(value)
        self.assertTrue(clean["nodes"][1]["stale"])
        self.assertIsNone(clean["nodes"][1]["observed"])
        self.assertTrue(clean["nodes"][1]["last_known_observed"])
        self.assertEqual(clean["nodes"][1]["observed_at"], TIME)
        self.assertFalse(clean["edges"][-1]["stale"])
        self.assertEqual(clean["edges"][-1]["type"], "occupies")
        bad = copy.deepcopy(value)
        bad["nodes"][1]["last_known_observed"] = "yes"
        with self.assertRaises(R.SnapshotError):
            R.validate_snapshot(bad)

    def test_path_claim_projects_access_without_canonical_path(self):
        value = snapshot()
        claim = node("workspace:sample:sample:1", "workspace", "Approved workspace 1",
                     refs=["registry"], observed=None,
                     registry={"host_id": "sample", "owner_session": "Sample session",
                               "access": "exclusive", "ordinal": 1,
                               "canonical_path": "/tmp/private"})
        value["nodes"].append(claim)
        value["edges"].append({**edge("path_claim:sample", "session:sample", claim["id"],
                                      "path_claim", "approved_registry_claim", "registry"),
                               "access": "exclusive", "canonical_path": "/tmp/private"})
        clean = R.validate_snapshot(value)
        self.assertEqual(clean["nodes"][-1]["registry"]["access"], "exclusive")
        self.assertEqual(clean["edges"][-1]["access"], "exclusive")
        self.assertNotIn("canonical_path", json.dumps(clean))
        bad = copy.deepcopy(value)
        bad["edges"][-1]["access"] = "owner"
        with self.assertRaises(R.SnapshotError):
            R.validate_snapshot(bad)

    def test_collector_schema_including_file_and_process_projection(self):
        value = R.validate_snapshot(snapshot())
        self.assertEqual(value["nodes"][2]["process"]["classification"], "agent_like_process")
        self.assertIsNone(value["nodes"][1]["root_evidence"]["card_vs_standard_match"])
        self.assertEqual(value["nodes"][1]["standard"]["present"], True)
        self.assertEqual(value["edges"][0]["type"], "executes_file")
        self.assertEqual(value["sources"][0]["status"], "available")

    def test_computed_route_is_distinct_and_requires_source_and_freshness(self):
        value = snapshot()
        value["edges"] = [edge("route:sample", "session:sample", "pane:sample:%1",
                               "router_agent_pane_ready", "computed", "imsg-router#pane_has_agent")]
        self.assertEqual(R.validate_snapshot(value)["edges"][0]["evidence"], "computed")
        for change in ({"source": ""}, {"freshness": {}}, {"freshness": {"status": "available"}}):
            bad = copy.deepcopy(value)
            bad["edges"][0].update(change)
            with self.assertRaises(R.SnapshotError):
                R.validate_snapshot(bad)

    def test_rejects_private_labels_and_bad_schema(self):
        for label in ("chat 19", "2125550123", "/tmp/private-note", "/private/secret",
                      "/opt/custom/file", "/Volumes/work", "name@example.test",
                      "vck_0123456789abcdefghijklmnop"):
            bad = snapshot()
            bad["nodes"][1]["label"] = label
            with self.subTest(label=label), self.assertRaises(R.SnapshotError):
                R.validate_snapshot(bad)
        bad = snapshot()
        bad["schema_version"] = "other"
        with self.assertRaises(R.SnapshotError):
            R.validate_snapshot(bad)

    def test_rejects_unknown_source_status(self):
        bad = snapshot()
        bad["sources"][0]["status"] = "everything fine"
        with self.assertRaises(R.SnapshotError):
            R.validate_snapshot(bad)


class CacheTests(unittest.TestCase):
    def cache_for(self, *values):
        queue = iter(values)
        ticks = iter([0, 4, 8, 12])

        def collect():
            value = next(queue)
            if isinstance(value, Exception):
                raise value
            return value

        return R.FleetMapCache(collector=collect, clock=lambda: next(ticks))

    def test_failure_returns_dated_last_good(self):
        cache = self.cache_for(snapshot(), R.SnapshotError("collector failed"))
        self.assertEqual(cache.get()[1]["status"], "fresh")
        code, value = cache.get()
        self.assertEqual(code, 200)
        self.assertEqual(value["status"], "stale")
        self.assertEqual(value["collected_at"], TIME)
        self.assertEqual(value["edges"][0]["freshness"]["as_of"], TIME)
        self.assertNotIn("collector failed", json.dumps(value))

    def test_partial_identity_keeps_last_known_separate_from_unknown(self):
        current = snapshot()
        current["nodes"][1]["identity"] = {"role": None, "card_present": None}
        current["unknowns"] = [{"id": "identity-unavailable", "kind": "source_unavailable",
                                "source": "identity_cards", "as_of": TIME, "detail": "Cards unavailable"}]
        current["sources"].append({"id": "identity_cards", "status": "unavailable", "as_of": TIME})
        current["summary"]["source_health"] = "degraded"
        cache = self.cache_for(snapshot(), current)
        cache.get()
        code, value = cache.get()
        session = next(n for n in value["nodes"] if n["id"] == "session:sample")
        self.assertEqual(code, 200)
        self.assertEqual(value["status"], "partial")
        self.assertIsNone(session["identity"]["role"])
        self.assertEqual(session["last_known_identity"]["role"], "assigned")
        self.assertIn("identity", session["stale_fields"])

    def test_missing_live_session_is_not_still_observed(self):
        current = snapshot()
        current["nodes"] = [current["nodes"][0]]
        current["edges"] = []
        current["unknowns"] = [{"kind": "source_unavailable", "source": "identity_cards", "as_of": TIME}]
        cache = self.cache_for(snapshot(), current)
        cache.get()
        value = cache.get()[1]
        session = next(n for n in value["nodes"] if n["id"] == "session:sample")
        self.assertTrue(session["stale"])
        self.assertFalse(session["observed"])
        self.assertTrue(session["last_known_observed"])

    def test_routing_outage_retains_computed_pipe_as_stale(self):
        prior = snapshot()
        prior["nodes"].append(node("channel:router", "channel", "Router channel", refs=["routing_conf"], observed=None))
        prior["edges"].append(edge("route:sample", "channel:router", "session:sample",
                                   "router_addressable", "computed", "imsg-router#live_sessions"))
        current = snapshot()
        current["unknowns"] = [{"kind": "source_unavailable", "source": "routing_descriptions", "as_of": TIME}]
        cache = self.cache_for(prior, current)
        cache.get()
        value = cache.get()[1]
        routed = next(e for e in value["edges"] if e["id"] == "route:sample")
        self.assertEqual(value["status"], "partial")
        self.assertTrue(routed["stale"])
        self.assertEqual(routed["freshness"]["status"], "source_unavailable")

    def test_registry_outage_keeps_binding_as_last_known(self):
        prior = snapshot()
        prior["nodes"][1]["registry"] = {"agent_id": "agent-moss", "state": "planned",
                                          "host_id": "sample", "session_name": "Sample session"}
        prior["nodes"].append(node("agent:agent-moss", "agent", "agent-moss", refs=["registry"],
                                   observed=None, registry=prior["nodes"][1]["registry"]))
        prior["edges"].append(edge("binding:moss", "agent:agent-moss", "session:sample",
                                    "planned_binding", "approved_registry_binding", "registry"))
        prior["sources"].append({"id": "registry", "status": "available", "as_of": TIME})
        current = snapshot()
        current["sources"].append({"id": "registry", "status": "unavailable", "as_of": TIME})
        cache = self.cache_for(prior, current)
        cache.get()
        value = cache.get()[1]
        self.assertEqual(value["status"], "partial")
        session = next(n for n in value["nodes"] if n["id"] == "session:sample")
        self.assertNotIn("registry", session)
        self.assertEqual(session["last_known_registry"]["agent_id"], "agent-moss")
        self.assertIn("registry", session["stale_fields"])
        self.assertTrue(next(n for n in value["nodes"] if n["id"] == "agent:agent-moss")["stale"])
        self.assertTrue(next(e for e in value["edges"] if e["id"] == "binding:moss")["stale"])

    def test_runtime_outage_never_retains_occupancy_or_lease(self):
        prior = snapshot()
        prior["nodes"].append(node("agent:sample", "agent", "Sample agent", refs=["registry"], observed=None,
                                   registry={"agent_id": "sample", "state": "verified",
                                             "host_id": "sample", "session_name": "Sample session"}))
        prior["nodes"].append(node("workspace:lease", "workspace", "Managed lease", refs=["work_leases"], observed=None))
        prior["edges"].append(edge("occupies:sample", "agent:sample", "pane:sample:%1",
                                    "occupies", "launcher_attested", "occupant_receipts"))
        prior["edges"].append(edge("lease:sample", "agent:sample", "workspace:lease",
                                    "work_lease", "observed", "work_leases"))
        current = snapshot()
        current["nodes"].append(prior["nodes"][-2])
        current["sources"].extend([{"id": "occupant_receipts", "status": "unavailable", "as_of": TIME},
                                   {"id": "work_leases", "status": "unavailable", "as_of": TIME}])
        cache = self.cache_for(prior, current)
        cache.get()
        value = cache.get()[1]
        self.assertNotIn("occupies", [e["type"] for e in value["edges"]])
        self.assertNotIn("work_lease", [e["type"] for e in value["edges"]])
        self.assertNotIn("workspace:lease", [n["id"] for n in value["nodes"]])

    def test_whole_remote_stage_failure_retains_remote_branch_as_unknown(self):
        prior = snapshot()
        prior["nodes"].append(node("host:side", "host", "Side device", refs=["remote:side:tmux"],
                                   observed=True, reachability="reachable"))
        prior["nodes"].append(node("session:side:work", "session", "work", "host:side",
                                   refs=["remote:side:tmux"], observed=True))
        prior["edges"].append(edge("remote:handoff", "session:sample", "session:side:work",
                                    "hands_off_to", "declared", "remote:side:state:work"))
        current = snapshot()
        current["nodes"].append(node("host:side", "host", "Side device", refs=["devices_conf"],
                                     observed=None, reachability="unknown_not_checked"))
        current["sources"].append({"id": "remote_hosts", "status": "unavailable", "as_of": TIME})
        cache = self.cache_for(prior, current)
        cache.get()
        value = cache.get()[1]
        host = next(n for n in value["nodes"] if n["id"] == "host:side")
        session = next(n for n in value["nodes"] if n["id"] == "session:side:work")
        self.assertIsNone(host["observed"])
        self.assertEqual(host["last_known_reachability"], "reachable")
        self.assertTrue(session["stale"])
        self.assertIsNone(session["observed"])
        self.assertTrue(session["last_known_observed"])
        self.assertTrue(next(e for e in value["edges"] if e["id"] == "remote:handoff")["stale"])

    def test_remote_transport_outage_matches_prefixed_host_sources(self):
        prior = snapshot()
        prior["nodes"].append(node("host:side", "host", "Side device", refs=["remote:side:tmux"],
                                   observed=True, reachability="reachable"))
        prior["nodes"].append(node("session:side:work", "session", "work", "host:side",
                                   refs=["remote:side:tmux"], observed=True))
        current = snapshot()
        current["nodes"].append(node("host:side", "host", "Side device", refs=["remote_transport"],
                                     observed=None, reachability="unknown_not_checked"))
        current["sources"].append({"id": "remote:side:transport", "status": "unavailable", "as_of": TIME})
        cache = self.cache_for(prior, current)
        cache.get()
        value = cache.get()[1]
        session = next(n for n in value["nodes"] if n["id"] == "session:side:work")
        self.assertTrue(session["stale"])
        self.assertIsNone(session["observed"])


class RegistryBridgeTests(unittest.TestCase):
    def test_optional_read_pipeline_orders_remote_registry_runtime(self):
        raw = snapshot()
        remote = copy.deepcopy(raw); remote["summary"]["remote_hosts_fresh"] = 1
        joined = copy.deepcopy(remote); joined["summary"]["registry_revision"] = 2
        runtime = copy.deepcopy(joined); runtime["summary"]["attested_occupants"] = 1
        with mock.patch.dict(os.environ, {R.COLLECTOR_ENV: "/tmp/test-collector",
                                              R.HOST_ID_ENV: "sample",
                                              R.REMOTE_ENV: "/tmp/test-remote",
                                              R.REMOTE_HOST_MAP_ENV: "/tmp/test-host-map",
                                              R.REMOTE_CACHE_DIR_ENV: "/tmp/test-cache",
                                              R.REGISTRY_ENV: "/tmp/test-registry",
                                              R.RUNTIME_ENV: "/tmp/test-runtime"}):
            with mock.patch.object(R, "_collector_output", return_value=raw):
                with mock.patch.object(R, "_bounded_json_process", side_effect=[remote, joined, runtime]) as runner:
                    value = R._collect_joined_snapshot()
        self.assertEqual(value["summary"]["attested_occupants"], 1)
        calls = runner.call_args_list
        self.assertEqual([call.args[0][0] for call in calls],
                         ["/tmp/test-remote", "/tmp/test-registry", "/tmp/test-runtime"])
        self.assertEqual(json.loads(calls[0].kwargs["input_bytes"]), raw)
        self.assertEqual(json.loads(calls[1].kwargs["input_bytes"]), remote)
        self.assertEqual(json.loads(calls[2].kwargs["input_bytes"]), joined)

    def test_bounded_join_pipe_reads_stdin_without_temp_file(self):
        script = "import json,sys; a=json.load(sys.stdin); json.dump({'seen':a['token']},sys.stdout)"
        result = R._bounded_json_process([sys.executable, "-c", script],
                                         input_bytes=b'{"token":"sample"}', source="registry")
        self.assertEqual(result, {"seen": "sample"})

    def test_registry_join_failure_marks_only_registry_unavailable(self):
        raw = snapshot()
        with mock.patch.dict(os.environ, {R.REGISTRY_ENV: "/tmp/test-registry"}):
            with mock.patch.object(R, "_collector_output", return_value=raw):
                with mock.patch.object(R, "_bounded_json_process", side_effect=R.SnapshotError("registry failed")):
                    value = R._collect_joined_snapshot()
        self.assertEqual(value["nodes"], raw["nodes"])
        self.assertEqual(value["summary"]["source_health"], "degraded")
        self.assertEqual(value["sources"][-1]["status"], "unavailable")
        self.assertEqual(value["unknowns"][-1]["source"], "registry")

    def test_join_receives_collector_json_via_stdin(self):
        raw = snapshot()
        joined = copy.deepcopy(raw)
        joined["summary"]["registry_revision"] = 2
        with mock.patch.dict(os.environ, {R.COLLECTOR_ENV: "/tmp/test-collector",
                                              R.REGISTRY_ENV: "/tmp/test-registry",
                                              R.HOST_ID_ENV: "test-host"}):
            with mock.patch.object(R, "_collector_output", return_value=raw) as collector:
                with mock.patch.object(R, "_bounded_json_process", return_value=joined) as runner:
                    self.assertEqual(R._collect_joined_snapshot()["summary"]["registry_revision"], 2)
        collector.assert_called_once_with("/tmp/test-collector")
        args, kwargs = runner.call_args
        self.assertEqual(args[0], ["/tmp/test-registry", "join", "--snapshot", "-", "--json"])
        self.assertEqual(json.loads(kwargs["input_bytes"]), raw)

    def test_invalid_host_id_stops_before_exec(self):
        for invalid in ("bad;host", "7host", "bad.host"):
            with self.subTest(host_id=invalid), mock.patch.dict(os.environ, {R.HOST_ID_ENV: invalid}):
                with self.assertRaises(R.SnapshotError):
                    R._collector_output("/tmp/test-collector")


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%s" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_flag_off_hides_both_routes(self):
        old = P.FLEET_MAP_ENABLED
        P.FLEET_MAP_ENABLED = False
        try:
            for path in ("/fleet-map", "/api/fleet-map"):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(self.base + path, timeout=2)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
        finally:
            P.FLEET_MAP_ENABLED = old

    def test_enabled_preview_is_read_only_and_no_store(self):
        old_flag, old_cache = P.FLEET_MAP_ENABLED, P.fleet_map_cache
        P.FLEET_MAP_ENABLED = True
        P.fleet_map_cache = type("FakeCache", (), {"get": lambda self: (200, R.validate_snapshot(snapshot()))})()
        try:
            with urllib.request.urlopen(self.base + "/api/fleet-map", timeout=2) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(json.load(response)["schema_version"], R.SCHEMA)
            with urllib.request.urlopen(self.base + "/fleet-map", timeout=2) as response:
                self.assertIn(b"Live terminal network", response.read())
            # These inherited portal routes can read pane/chat content and must
            # never be reachable on the metadata-only preview listener.
            for path in ("/api/fleet", "/api/tunnel?name=sample", "/api/brief", "/board"):
                with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(self.base + path, timeout=2)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
            request = urllib.request.Request(self.base + "/api/fleet-map", data=b"", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(error.exception.code, 405)
            error.exception.close()
        finally:
            P.FLEET_MAP_ENABLED, P.fleet_map_cache = old_flag, old_cache


if __name__ == "__main__":
    unittest.main()
