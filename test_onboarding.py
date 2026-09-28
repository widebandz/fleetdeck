#!/usr/bin/env python3
"""Local customer phone/Notes checks. Never opens the live Notes store."""

import base64
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import portal_server as portal


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.old = (portal.CONF, portal.NOTES_PATH, portal.CONTROL_TOKEN,
                    portal.cached_scan, portal.head_session_snapshot,
                    portal.SETUP_STATE_PATH, portal.FIRST_GOAL_STATUS_PATH)
        self.temp = tempfile.TemporaryDirectory()
        portal.NOTES_PATH = os.path.join(self.temp.name, "notes.json")
        portal.SETUP_STATE_PATH = os.path.join(self.temp.name, "state.json")
        portal.FIRST_GOAL_STATUS_PATH = os.path.join(self.temp.name, "goal.json")
        portal.CONF = dict(portal.CONF, onboarding={
            "os_name": "My OS", "agent_name": "Ada",
            "first_goal": "website", "first_project_url": "https://example.test/start",
            "head_session": "wb-head"})
        portal.CONTROL_TOKEN = ""
        portal.cached_scan = lambda: {"services": [], "agents": []}
        portal.head_session_snapshot = lambda session: "Ada is working\n"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), portal.Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        (portal.CONF, portal.NOTES_PATH, portal.CONTROL_TOKEN,
         portal.cached_scan, portal.head_session_snapshot,
         portal.SETUP_STATE_PATH, portal.FIRST_GOAL_STATUS_PATH) = self.old
        self.temp.cleanup()

    def request(self, path, body=None, auth=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read().decode()

    def test_phone_graph_watch_and_control_gate(self):
        code, page = self.request("/phone")
        self.assertEqual(code, 200)
        for text in ("My OS", "Ada", "Build a website", "Knowledge graph",
                     "Live terminal", "Notes", "BETA", "https://example.test/start"):
            self.assertIn(text, page)
        self.assertNotIn("/app/terminal", page)
        self.assertNotIn("/app/chat", page)
        self.assertEqual(self.request("/board")[0], 404)
        self.assertEqual(self.request("/app/terminal")[0], 404)
        self.assertEqual(self.request("/api/dispatch", {"target": "Ada"})[0], 404)

        code, graph = self.request("/graph")
        self.assertEqual(code, 200)
        self.assertIn("Your starter map", graph)
        self.assertIn("Messages listener", graph)
        code, watch = self.request("/watch")
        self.assertEqual(code, 200)
        self.assertIn("read only", watch)
        code, body = self.request("/api/watch")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {
            "running": True, "text": "Ada is working\n"})

        portal.CONTROL_TOKEN = "long-private-control-token"
        self.assertEqual(self.request("/board")[0], 401)
        auth = "Basic " + base64.b64encode(
            b"operator:long-private-control-token").decode()
        self.assertEqual(self.request("/board", auth=auth)[0], 200)

    def test_config_url_is_safe_and_escaped(self):
        portal.CONF["onboarding"]["os_name"] = "<script>alert(1)</script>"
        portal.CONF["onboarding"]["first_project_url"] = "javascript:alert(1)"
        code, page = self.request("/phone")
        self.assertEqual(code, 200)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn("javascript:alert(1)", page)
        self.assertIn("project link will appear", page)

    def test_private_setup_state_and_live_project_resolution(self):
        portal.CONF.pop("onboarding")
        with open(portal.SETUP_STATE_PATH, "w") as fh:
            json.dump({"metadata": {"os_name": "From setup",
                                    "agent_name": "Iris",
                                    "first_goal": "website",
                                    "agent_apple_account": "private@example.test"}}, fh)
        os.chmod(portal.SETUP_STATE_PATH, 0o600)
        code, page = self.request("/phone")
        self.assertEqual(code, 200)
        self.assertIn("From setup", page)
        self.assertIn("Iris", page)
        self.assertNotIn("private@example.test", page)

        with open(portal.FIRST_GOAL_STATUS_PATH, "w") as fh:
            json.dump({"goal": "website", "status": "ready",
                       "os_name": "From setup", "agent_name": "Iris",
                       "local_url": "http://127.0.0.1:4173",
                       "phone_url": "https://verified.example.test"}, fh)
        os.chmod(portal.FIRST_GOAL_STATUS_PATH, 0o600)
        self.assertIn("https://verified.example.test", self.request("/phone")[1])
        portal.cached_scan = lambda: {"services": [{
            "id": "first-project", "linkable": True,
            "url": "https://live.example.test"}], "agents": []}
        page = self.request("/phone")[1]
        self.assertIn("https://live.example.test", page)
        self.assertNotIn("https://verified.example.test", page)
        portal.cached_scan = lambda: {"services": [{
            "id": "first-project", "up": True, "linkable": False,
            "url": "http://127.0.0.1:4173"}], "agents": []}
        page = self.request("/phone")[1]
        self.assertIn("https://verified.example.test", page)
        self.assertNotIn("http://127.0.0.1:4173", page)
        portal.cached_scan = lambda: {"services": [{
            "id": "first-project", "up": False, "linkable": False,
            "url": "http://127.0.0.1:4173"}], "agents": []}
        page = self.request("/phone")[1]
        self.assertNotIn("https://verified.example.test", page)
        self.assertIn("phone link pending", page)
        with open(portal.FIRST_GOAL_STATUS_PATH, "w") as fh:
            json.dump({"goal": "website", "status": "ready",
                       "os_name": "From setup", "agent_name": "Iris",
                       "local_url": "http://127.0.0.1:4173"}, fh)
        portal.cached_scan = lambda: {"services": [{
            "id": "first-project", "up": True, "linkable": True,
            "url": "http://127.0.0.1:4173"}], "agents": []}
        page = self.request("/phone")[1]
        self.assertNotIn('href="http://127.0.0.1:4173"', page)
        self.assertIn("phone link pending", page)

        # Changing either onboarding name while keeping the website goal must
        # not reuse the old service URL or the old status phone URL.
        with open(portal.FIRST_GOAL_STATUS_PATH, "w") as fh:
            json.dump({"goal": "website", "status": "ready",
                       "os_name": "From setup", "agent_name": "Iris",
                       "phone_url": "https://old-project.example.test"}, fh)
        portal.cached_scan = lambda: {"services": [{
            "id": "first-project", "up": True, "linkable": True,
            "url": "https://old-live.example.test"}], "agents": []}
        with open(portal.SETUP_STATE_PATH, "w") as fh:
            json.dump({"metadata": {"os_name": "Renamed OS",
                                    "agent_name": "Nova",
                                    "first_goal": "website"}}, fh)
        os.chmod(portal.SETUP_STATE_PATH, 0o600)
        page = self.request("/phone")[1]
        self.assertIn("Renamed OS", page)
        self.assertNotIn("https://old-project.example.test", page)
        self.assertNotIn("https://old-live.example.test", page)
        self.assertIn("project link will appear", page)

        # A copied or world-readable setup file cannot select customer mode.
        os.chmod(portal.SETUP_STATE_PATH, 0o644)
        self.assertIsNone(portal.onboarding_config())

    def test_notes_beta_crud_and_concurrent_capture(self):
        self.assertEqual(json.loads(self.request("/api/notes")[1])["notes"], [])
        self.assertTrue(os.path.isfile(portal.NOTES_PATH))
        code, body = self.request("/api/notes", {"text": "title: First idea\nnext: Try it"})
        self.assertEqual(code, 200)
        note = json.loads(body)["note"]
        self.assertEqual(note["title"], "First idea")
        self.assertEqual(self.request("/api/notes/edit", {
            "id": note["id"], "concept": "A useful concept"})[0], 200)
        self.assertEqual(self.request("/api/notes/status", {
            "id": note["id"], "status": "ready"})[0], 200)

        def add(i):
            return self.request("/api/notes", {"title": "idea %d" % i})[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(add, range(24))), [200] * 24)
        listed = json.loads(self.request("/api/notes")[1])["notes"]
        self.assertEqual(len(listed), 25)
        self.assertEqual(len({n["id"] for n in listed}), 25)
        self.assertEqual(self.request("/api/notes/delete", {
            "id": note["id"]})[0], 200)
        with open(portal.NOTES_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)["notes"]
        self.assertEqual(len(saved), 24)
        self.assertNotIn(note["id"], {n["id"] for n in saved})

    def test_notes_lock_across_processes(self):
        code = """import portal_server as p
p.CONF = dict(p.CONF, onboarding={'os_name': 'My OS', 'agent_name': 'Ada'})
for i in range(12):
    p.notes_add({'title': 'capture %d' % i})
"""
        env = dict(os.environ, FLEETDECK_NOTES_PATH=portal.NOTES_PATH)
        processes = [subprocess.Popen(
            [sys.executable, "-c", code], cwd=os.path.dirname(__file__),
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(2)]
        for process in processes:
            _, err = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, err.decode())
        with open(portal.NOTES_PATH, encoding="utf-8") as fh:
            notes = json.load(fh)["notes"]
        self.assertEqual(len(notes), 24)
        self.assertEqual(len({n["id"] for n in notes}), 24)

    def test_writable_chat_fails_closed_in_customer_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            shutil.copy2(os.path.join(os.path.dirname(__file__), "chat_server.py"),
                         os.path.join(directory, "chat_server.py"))
            with open(os.path.join(directory, "config.json"), "w") as fh:
                json.dump({"onboarding": {
                    "os_name": "My OS", "agent_name": "Ada"}}, fh)
            done = subprocess.run(
                [sys.executable, os.path.join(directory, "chat_server.py")],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(done.returncode, 78)
            self.assertIn("refusing writable chat", done.stdout)


if __name__ == "__main__":
    unittest.main()
