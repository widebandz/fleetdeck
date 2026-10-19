#!/usr/bin/env python3
"""Offline Work portal contracts. No live ledger, tmux or messaging is used."""

import base64
import copy
import json
import os
import subprocess
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

os.environ.setdefault("FLEETDECK_HOST", "sample.invalid")
import portal_server as portal


def sample_ticket():
    return {
        "id": "REQ-0042", "goal": "Prepare the next client contract",
        "done_when": "The scope, price, and delivery dates are ready for review",
        "owner": "contracts", "state": "waiting", "priority": 2,
        "created": 1000, "updated": 1120, "last_report": 1120,
        "work_started": True, "auto_paused": False, "pause_reason": None,
        "idle_timeout_seconds": 300,
        "pending_question": "Approve the delivery date?", "wait_reason": "operator",
        "timing": {"work_seconds": 90, "wait_seconds": 30,
                   "operator_wait_seconds": 30, "elapsed_seconds": 120},
        "history": [{"kind": "wait", "at": 1120, "source": "private-source",
                     "data": {"reason": "operator", "question": "Approve the delivery date?",
                              "credentials": "never-exposed"}}],
        "body": "private-original-receipt", "media": ["private-file"],
        "attachments": [{"id": "ATT-123", "original_name": "IMG_9770.png",
                         "mime_type": "image/png", "size": 2000, "state": "available",
                         "path": "/private/attachment-path", "source": "private-source",
                         "identity": "private-identity", "sha256": "private-hash",
                         "error": "private-error"}],
        "dispatches": [{"text": "private-dispatch"}], "record": {"sender": "private-handle"},
    }


class WorkAdapterTests(unittest.TestCase):
    def test_fixed_command_and_private_fields(self):
        result = subprocess.CompletedProcess([], 0, json.dumps([sample_ticket()]), "")
        with mock.patch.object(portal.subprocess, "run", return_value=result) as run:
            matrix = portal.work_matrix()
        self.assertEqual(run.call_args.args[0], [portal.WORK_COMMAND, "list", "--all", "--json"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["timeout"], 8)
        self.assertNotIn("shell", run.call_args.kwargs)
        text = json.dumps(matrix)
        for value in ("private-original-receipt", "private-file", "private-dispatch", "private-handle"):
            self.assertNotIn(value, text)
        detail = portal.work_ticket(sample_ticket(), detailed=True)
        self.assertEqual(detail["history"][0]["at"], 1120)
        self.assertNotIn("never-exposed", json.dumps(detail))
        self.assertNotIn("private-source", json.dumps(detail))
        self.assertEqual(detail["attachments"], [{"id": "ATT-123", "original_name": "IMG_9770.png",
                         "mime_type": "image/png", "size": 2000, "state": "available"}])
        for value in ("attachment-path", "private-identity", "private-hash", "private-error"):
            self.assertNotIn(value, json.dumps(detail))
        self.assertNotIn("attachments", matrix["tickets"][0])

    def test_command_failure_never_returns_stderr(self):
        failure = subprocess.CalledProcessError(1, ["wb-work"], stderr="private-token")
        with mock.patch.object(portal.subprocess, "run", side_effect=failure):
            with self.assertRaises(portal.WorkUnavailable) as raised:
                portal.work_command("list")
        self.assertNotIn("private-token", str(raised.exception))
        with self.assertRaises(ValueError):
            portal.work_command("dispatch", "REQ-0042")


class WorkHTTPTests(unittest.TestCase):
    def setUp(self):
        self.ticket = sample_ticket()
        self.days = []
        self.commands = []

        def ledger(*args):
            self.commands.append(args)
            if args == ("list", "--all"):
                return [copy.deepcopy(self.ticket)]
            if args == ("show", "REQ-0042"):
                return copy.deepcopy(self.ticket)
            if args == ("days",):
                return copy.deepcopy(self.days)
            if args == ("close-day",):
                if self.days:
                    return dict(self.days[0], duplicate=True)
                snapshot = {"day": "2026-10-05", "timezone": "America/New_York",
                            "closed_at": "2026-10-05T20:00:00Z", "duplicate": False,
                            "carried": [self.ticket["id"]], "resolved": [], "work_seconds": 90,
                            "tickets": [copy.deepcopy(self.ticket)]}
                self.days.append(snapshot)
                return snapshot
            raise AssertionError("unexpected ledger command: %r" % (args,))

        self.patches = [mock.patch.object(portal, "work_command", side_effect=ledger),
                        mock.patch.object(portal, "onboarding_config", return_value=None),
                        mock.patch.object(portal, "notes_summary", return_value="0 notes"),
                        mock.patch.object(portal, "FLEET_MAP_ENABLED", False),
                        mock.patch.object(portal, "allowed", return_value=True)]
        for patch in self.patches:
            patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), portal.Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%s" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        for patch in reversed(self.patches):
            patch.stop()

    def request(self, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read().decode(), response.headers

    def test_page_and_authoritative_ticket_detail(self):
        code, page, headers = self.request("/work")
        self.assertEqual(code, 200)
        self.assertIn("Close today", page)
        self.assertIn("/api/work/ticket?id=", page)
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("__MACHINE__", page)
        self.assertNotIn("__VT__", page)
        code, body, _ = self.request("/api/work")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["tickets"][0]["wait_reason"], "operator")
        code, body, _ = self.request("/api/work/ticket?id=REQ-0042")
        self.assertEqual(code, 200)
        detail = json.loads(body)
        self.assertEqual(detail["goal"], self.ticket["goal"])
        self.assertEqual(detail["done_when"], self.ticket["done_when"])
        self.assertEqual(detail["timing"]["work_seconds"], 90)
        self.assertEqual(detail["history"][0]["data"]["question"], "Approve the delivery date?")
        self.assertNotIn("private", body)

    def test_closeout_carries_identity_and_time_without_starting_workers(self):
        code, body, _ = self.request("/api/work/close-day", {}, {"Origin": self.base})
        self.assertEqual(code, 200)
        snapshot = json.loads(body)
        self.assertEqual(snapshot["carried"], ["REQ-0042"])
        self.assertEqual(snapshot["closed_at"], "2026-10-05T20:00:00Z")
        self.assertNotIn("tickets", snapshot)
        code, body, _ = self.request("/api/work/days")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["days"][0]["day"], "2026-10-05")
        _, body, _ = self.request("/api/work/close-day", {})
        self.assertTrue(json.loads(body)["duplicate"])
        self.assertEqual(self.ticket["state"], "waiting")
        self.assertEqual(self.ticket["timing"]["work_seconds"], 90)
        self.assertFalse(any(args[0] in {"start", "resume", "dispatch"} for args in self.commands))

    def test_unstarted_and_automatically_paused_time_contract(self):
        self.ticket.update(owner=None, state="queued", work_started=False,
                           pending_question="", wait_reason=None)
        self.ticket["timing"].update(work_seconds=0, wait_seconds=0,
                                     operator_wait_seconds=0, elapsed_seconds=600)
        _, body, _ = self.request("/api/work")
        queued = json.loads(body)["tickets"][0]
        self.assertFalse(queued["work_started"])
        self.assertEqual(queued["timing"]["work_seconds"], 0)
        self.ticket["timing"]["elapsed_seconds"] = 660
        _, body, _ = self.request("/api/work/ticket?id=REQ-0042")
        queued = json.loads(body)
        self.assertEqual(queued["timing"]["work_seconds"], 0)
        self.assertEqual(queued["timing"]["elapsed_seconds"], 660)

        self.ticket.update(owner="contracts", state="paused", work_started=True,
                           auto_paused=True, pause_reason="auto_idle")
        self.ticket["timing"].update(work_seconds=90, paused_seconds=570)
        _, body, _ = self.request("/api/work/ticket?id=REQ-0042")
        paused = json.loads(body)
        self.assertEqual(paused["state"], "paused")
        self.assertTrue(paused["work_started"])
        self.assertTrue(paused["auto_paused"])
        self.assertEqual(paused["pause_reason"], "auto_idle")
        self.assertEqual(paused["idle_timeout_seconds"], 300)
        self.assertEqual(paused["timing"]["work_seconds"], 90)
        self.assertFalse(any(args[0] in {"start", "resume", "dispatch"} for args in self.commands))

    def test_refuses_cross_origin_and_arbitrary_actions(self):
        self.assertEqual(self.request("/api/work/close-day", {}, {"Origin": "https://other.invalid"})[0], 403)
        self.assertEqual(self.request("/api/work/close-day", {}, {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("/api/work/close-day", {"command": "dispatch"})[0], 400)
        self.assertEqual(self.request("/api/work/close-day", {}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/work/dispatch", {})[0], 405)
        self.assertEqual(self.request("/api/work/ticket?id=../../private")[0], 400)
        self.assertEqual(self.commands, [])

    def test_uses_tailnet_and_existing_customer_operator_gate(self):
        with mock.patch.object(portal, "allowed", return_value=False):
            self.assertEqual(self.request("/api/work")[0], 403)
            self.assertEqual(self.request("/api/work/close-day", {})[0], 403)
        with mock.patch.object(portal, "onboarding_config", return_value={"os_name": "Customer"}), \
                mock.patch.object(portal, "CONTROL_TOKEN", "long-private-control-token"):
            self.assertEqual(self.request("/work")[0], 401)
            self.assertEqual(self.request("/api/work")[0], 401)
            self.assertEqual(self.request("/api/work/close-day", {})[0], 401)
            auth = "Basic " + base64.b64encode(b"operator:long-private-control-token").decode()
            self.assertEqual(self.request("/api/work", headers={"Authorization": auth})[0], 200)
            self.assertEqual(self.request("/api/work/close-day", {}, {"Authorization": auth})[0], 200)

    def test_phone_and_board_replace_pm_entry(self):
        work_service = {"id": "work", "name": "Work", "icon": "clock", "port": 8790,
                        "url": "https://sample.invalid:8790/work", "up": True, "linkable": True}
        data = {"services": [work_service], "agents": [], "extra": [], "groups": []}
        with mock.patch.object(portal, "cached_scan", return_value=data):
            code, phone, _ = self.request("/phone")
            self.assertEqual(code, 200)
            self.assertIn('href="/work"', phone)
            self.assertIn("WORK", phone)
            self.assertNotIn("pm not registered", phone)
            code, board, _ = self.request("/board")
            self.assertEqual(code, 200)
            self.assertIn('id="work" href="/work"', board)


if __name__ == "__main__":
    unittest.main()
