"""Offline checks for the map guide's evidence and read-only HTTP boundary."""

import io
import json
import unittest
from email.message import Message
from unittest import mock

import fleet_explainer as guide
import portal_server as portal
from test_fleet_map import snapshot


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.graph = snapshot()
        self.graph["status"] = "fresh"

    def test_fallback_uses_current_card_and_source_status(self):
        result = guide.explain(self.graph, "What does assigned mean?", "session:sample",
                               model_answer=lambda *_: None)
        self.assertEqual(result["mode"], "source-backed-fallback")
        self.assertEqual(result["navigate"], {"kind": "node", "id": "session:sample", "layer": "identity"})
        self.assertIn("declares role assigned", result["answer"])
        self.assertIn("does not verify the current agent occupant", result["answer"])
        self.assertTrue(any(ref["sources"] == ["tmux", "identity:sample", "sessions_conf"]
                            for ref in result["references"] if ref["kind"] == "node"))

    def test_edge_navigation_is_derived_from_graph_not_model(self):
        result = guide.explain(self.graph, "Trace this route", "exec:sample", "overview",
                               model_answer=lambda *_: "I am at a different invented node.")
        self.assertEqual(result["navigate"], {"kind": "edge", "id": "exec:sample", "layer": "routing"})
        self.assertEqual(result["mode"], "local-model")

    def test_unavailable_and_invalid_question_fail_closed(self):
        with self.assertRaises(ValueError):
            guide.explain({"status": "source_unavailable", "nodes": []}, "Where is Trace?")
        with self.assertRaises(ValueError):
            guide.explain(self.graph, " " * 3)

    def test_compact_model_context_excludes_unneeded_private_fields(self):
        self.graph["nodes"][1]["private_note"] = "never send this"
        captured = {}
        def model(question, graph, nodes, edges, layer):
            captured["nodes"] = nodes
            return None
        guide.explain(self.graph, "Explain this session", "session:sample", model_answer=model)
        self.assertNotIn("private_note", json.dumps(captured))


class HandlerTests(unittest.TestCase):
    def handler(self, path, body, origin="https://sample.invalid:18970", content_type="application/json"):
        handler = object.__new__(portal.Handler)
        handler.path = path
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = Message()
        handler.headers["Host"] = "sample.invalid:18970"
        handler.headers["Origin"] = origin
        handler.headers["Content-Type"] = content_type
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        handler._send = mock.Mock()
        return handler

    def test_only_explanation_query_passes_map_post_boundary(self):
        body = json.dumps({"question": "Explain routing", "focus_id": "session:sample", "layer": "routing"}).encode()
        handler = self.handler("/api/fleet-explain", body)
        with mock.patch.object(portal, "FLEET_MAP_ENABLED", True), mock.patch.object(portal, "allowed", return_value=True), \
             mock.patch.object(portal.fleet_map_cache, "get", return_value=(200, {"status": "fresh"})), \
             mock.patch.object(portal, "explain_fleet", return_value={"answer": "Source-backed"}) as explain:
            handler.do_POST()
        self.assertEqual(handler._send.call_args.args[0], 200)
        explain.assert_called_once_with({"status": "fresh"}, "Explain routing", "session:sample", "routing")
        for path in ("/api/chat", "/api/fleet-adopt", "/api/terminal/send"):
            forbidden = self.handler(path, body)
            with mock.patch.object(portal, "FLEET_MAP_ENABLED", True), mock.patch.object(portal, "allowed", return_value=True):
                forbidden.do_POST()
            self.assertEqual(forbidden._send.call_args.args[0], 405)

    def test_wrong_origin_and_non_json_are_rejected(self):
        body = b'{"question":"Explain routing"}'
        for handler, expected in ((self.handler("/api/fleet-explain", body, origin="https://elsewhere.invalid"), 403),
                                  (self.handler("/api/fleet-explain", body, content_type="text/plain"), 415)):
            with mock.patch.object(portal, "FLEET_MAP_ENABLED", True), mock.patch.object(portal, "allowed", return_value=True):
                handler.do_POST()
            self.assertEqual(handler._send.call_args.args[0], expected)


if __name__ == "__main__":
    unittest.main()
