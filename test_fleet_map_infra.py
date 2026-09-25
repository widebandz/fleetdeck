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

    def _install_brief(self):
        def item(kind, text, evidence="declared", outcome=None, **detail):
            value = {"kind": kind, "text": text, "source": "Zayed scope directive",
                     "evidence": evidence, "as_of": TIME}
            if outcome is not None:
                value["outcome"] = outcome
            value.update(detail)
            return value
        return {"version": I.SCOPE_BRIEF_VERSION, "session": "install",
                "role": "Installation and runtime-integration specialist",
                "mission": "Build and verify the adopted product on the host.",
                "source": "Zayed scope directive", "as_of": TIME,
                "status": "declared", "association": "session_name_only",
                "items": [item("own", "Integrate the backend and frontend."),
                          item("handoff", "Report evidence to Trace.",
                               direction="outgoing", counterparty="Trace"),
                          item("completion_check", "Verify a browser path.",
                               outcome="required", check_key="browser_path"),
                          item("approval_gate", "Escalate model selection before download.",
                               decision="pending", approver="Zayed")]}

    def test_session_scope_brief_is_declared_and_does_not_create_edges(self):
        raw = snapshot()
        raw["nodes"].append({"id": "session:install", "type": "session", "label": "install",
                             "parent_id": "host:sample", "declared": False, "observed": True,
                             "source_refs": ["tmux"], "observed_at": TIME})
        brief_dir = self.base / "briefs"
        brief_dir.mkdir()
        (brief_dir / "install.json").write_text(json.dumps(self._install_brief()))
        env = dict(self.env, FLEETDECK_FLEET_SCOPE_BRIEF_DIR=str(brief_dir))
        clean = R.validate_snapshot(I.enrich(raw, env, self.runner))
        install = next(n for n in clean["nodes"] if n["id"] == "session:install")
        model = install["responsibility_model"]
        self.assertEqual(model["association"], "session_name_only")
        self.assertEqual(model["role"], "Installation and runtime-integration specialist")
        self.assertEqual(model["items"][2]["outcome"], "required")
        self.assertEqual(model["items"][1]["direction"], "outgoing")
        self.assertEqual(model["items"][2]["check_key"], "browser_path")
        self.assertEqual(model["items"][3]["decision"], "pending")
        self.assertEqual(model["items"][3]["approver"], "Zayed")
        self.assertFalse(any(i["kind"] == "completion_evidence" for i in model["items"]))
        self.assertNotIn("registry", install)
        self.assertFalse(any(e["from"] == "session:install" or e["to"] == "session:install"
                             for e in clean["edges"]))
        self.assertEqual(next(s for s in clean["sources"] if s["id"] == "infra:scope_brief:install")["status"], "available")
        self.assertNotIn(str(brief_dir), json.dumps(clean))

    def test_invalid_scope_brief_is_omitted_without_leaking_content(self):
        raw = snapshot()
        raw["nodes"].append({"id": "session:install", "type": "session", "label": "install",
                             "parent_id": "host:sample", "declared": False, "observed": True,
                             "source_refs": ["tmux"], "observed_at": TIME})
        brief_dir = self.base / "briefs"
        brief_dir.mkdir()
        path = brief_dir / "install.json"
        env = dict(self.env, FLEETDECK_FLEET_SCOPE_BRIEF_DIR=str(brief_dir))
        variants = []
        wrong = self._install_brief()
        wrong["session"] = "other"
        variants.append(wrong)
        private = self._install_brief()
        private["items"][0]["text"] = f"Read {self.base / 'private.txt'}"
        variants.append(private)
        unknown = self._install_brief()
        unknown["items"][0]["kind"] = "deploys"
        variants.append(unknown)
        outcome = self._install_brief()
        outcome["items"][0]["outcome"] = "passed"
        variants.append(outcome)
        incomplete_handoff = self._install_brief()
        incomplete_handoff["items"][1].pop("counterparty")
        variants.append(incomplete_handoff)
        private_counterparty = self._install_brief()
        private_counterparty["items"][1]["counterparty"] = str(self.base / "private")
        variants.append(private_counterparty)
        unmatched_evidence = self._install_brief()
        unmatched_evidence["items"].append({"kind": "completion_evidence", "text": "Build ran.",
                                             "source": "local check", "evidence": "checked",
                                             "as_of": TIME, "outcome": "partial",
                                             "check_key": "unknown_check"})
        variants.append(unmatched_evidence)
        duplicate_check = self._install_brief()
        duplicate_check["items"].append(dict(duplicate_check["items"][2]))
        variants.append(duplicate_check)
        wrong_decision = self._install_brief()
        wrong_decision["items"][3]["decision"] = "unverified"
        variants.append(wrong_decision)
        incomplete_approval = self._install_brief()
        incomplete_approval["items"][3].pop("approver")
        variants.append(incomplete_approval)
        private_approver = self._install_brief()
        private_approver["items"][3]["approver"] = str(self.base / "private")
        variants.append(private_approver)
        inferred_approval = self._install_brief()
        inferred_approval["items"][3]["decision"] = "approved"
        variants.append(inferred_approval)
        misplaced_decision = self._install_brief()
        misplaced_decision["items"][0]["decision"] = "pending"
        misplaced_decision["items"][0]["approver"] = "Zayed"
        variants.append(misplaced_decision)
        extra = self._install_brief()
        extra["credential"] = "hidden"
        variants.append(extra)
        for value in variants:
            with self.subTest(value=value.get("session"), kind=value["items"][0]["kind"]):
                path.write_text(json.dumps(value))
                clean = R.validate_snapshot(I.enrich(raw, env, self.runner))
                install = next(n for n in clean["nodes"] if n["id"] == "session:install")
                self.assertNotIn("responsibility_model", install)
                self.assertTrue(any(u["kind"] == "invalid_metadata" and
                                    u["source"] == "infra:scope_brief:install" for u in clean["unknowns"]))
                self.assertNotIn(str(self.base), json.dumps(clean))
                self.assertNotIn("hidden", json.dumps(clean))
        path.unlink()
        clean = R.validate_snapshot(I.enrich(raw, env, self.runner))
        install = next(n for n in clean["nodes"] if n["id"] == "session:install")
        self.assertNotIn("responsibility_model", install)
        self.assertFalse(any(s["id"] == "infra:scope_brief:install" for s in clean["sources"]))

    def test_unreadable_scope_brief_marks_source_unavailable(self):
        raw = snapshot()
        raw["nodes"].append({"id": "session:install", "type": "session", "label": "install",
                             "parent_id": "host:sample", "declared": False, "observed": True,
                             "source_refs": ["tmux"], "observed_at": TIME})
        brief_dir = self.base / "briefs"
        brief_dir.mkdir()
        path = brief_dir / "install.json"
        path.write_text(json.dumps(self._install_brief()))
        original = I.read_text

        def unreadable(candidate, max_bytes=64_000):
            return None if candidate == path else original(candidate, max_bytes)

        env = dict(self.env, FLEETDECK_FLEET_SCOPE_BRIEF_DIR=str(brief_dir))
        with mock.patch.object(I, "read_text", side_effect=unreadable):
            clean = R.validate_snapshot(I.enrich(raw, env, self.runner))
        install = next(n for n in clean["nodes"] if n["id"] == "session:install")
        self.assertNotIn("responsibility_model", install)
        self.assertTrue(any(u["kind"] == "source_unavailable" and
                            u["source"] == "infra:scope_brief:install" for u in clean["unknowns"]))
        self.assertNotIn(str(path), json.dumps(clean))

    def test_malformed_scope_brief_keeps_only_dated_last_known_model(self):
        raw = snapshot()
        raw["nodes"].append({"id": "session:install", "type": "session", "label": "install",
                             "parent_id": "host:sample", "declared": False, "observed": True,
                             "source_refs": ["tmux"], "observed_at": TIME})
        brief_dir = self.base / "briefs"
        brief_dir.mkdir()
        path = brief_dir / "install.json"
        path.write_text(json.dumps(self._install_brief()))
        env = dict(self.env, FLEETDECK_FLEET_SCOPE_BRIEF_DIR=str(brief_dir))
        prior = I.enrich(raw, env, self.runner)
        unsafe = self._install_brief()
        unsafe["items"][0]["text"] = f"Read {self.base / 'secret.txt'}"
        path.write_text(json.dumps(unsafe))
        current = I.enrich(raw, env, self.runner)
        values = iter([prior, current, current])
        times = iter([0, 4, 8])
        cache = R.FleetMapCache(collector=lambda: next(values), clock=lambda: next(times))
        self.assertEqual(cache.get()[1]["status"], "fresh")
        result = cache.get()[1]
        install = next(n for n in result["nodes"] if n["id"] == "session:install")
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("responsibility_model", install)
        self.assertEqual(install["last_known_responsibility_model"]["mission"],
                         "Build and verify the adopted product on the host.")
        self.assertIn("responsibility_model", install["stale_fields"])
        self.assertNotIn(str(self.base), json.dumps(result))
        repeated = cache.get()[1]
        install_again = next(n for n in repeated["nodes"] if n["id"] == "session:install")
        self.assertEqual(install_again["last_known_responsibility_model"]["mission"],
                         "Build and verify the adopted product on the host.")

    def test_many_scope_briefs_keep_snapshot_bounded_and_readable(self):
        raw = snapshot()
        brief_dir = self.base / "briefs"
        brief_dir.mkdir()
        for index in range(45):
            name = f"scope{index:02d}"
            raw["nodes"].append({"id": f"session:{name}", "type": "session", "label": name,
                                 "parent_id": "host:sample", "declared": False, "observed": True,
                                 "source_refs": ["tmux"], "observed_at": TIME})
            brief = self._install_brief()
            brief["session"] = name
            (brief_dir / f"{name}.json").write_text(json.dumps(brief))
        env = dict(self.env, FLEETDECK_FLEET_SCOPE_BRIEF_DIR=str(brief_dir))
        clean = R.validate_snapshot(I.enrich(raw, env, self.runner))
        self.assertEqual(sum("responsibility_model" in n for n in clean["nodes"]), 45)
        self.assertLessEqual(len(clean["sources"]), R.MAX_SOURCES)

    def _dqr_fixture(self):
        raw = snapshot()
        raw["nodes"].extend([
            {"id": "session:DQR", "type": "session", "label": "DQR", "parent_id": "host:sample",
             "declared": False, "observed": True, "source_refs": ["tmux", "identity_cards"],
             "observed_at": TIME, "standard": {"present": False},
             "root_evidence": {"observed_cwd_within_card": False, "source_refs": ["tmux", "identity_cards"],
                               "as_of": TIME}},
            {"id": "chat:bound-DQR", "type": "chat", "label": "Bound chat → DQR", "parent_id": None,
             "declared": True, "observed": None, "source_refs": ["chatbind"], "observed_at": None},
        ])
        for tool in ("dqr-media", "dqr-push"):
            raw["nodes"].append({"id": f"tool:{tool}", "type": "tool", "label": tool,
                                 "parent_id": None, "declared": True, "observed": None,
                                 "source_refs": ["identity:DQR#tools"], "observed_at": None})
        raw["edges"].append({"id": "chat_routes_to:dqr", "from": "chat:bound-DQR", "to": "session:DQR",
                             "type": "chat_routes_to", "evidence": "declared", "source": "chatbind",
                             "display": "Bound chat ownership", "freshness": {"as_of": TIME,
                                                                          "status": "configured-only"}})
        raw["edges"].append({"id": "uses_tool:dqr-media", "from": "session:DQR", "to": "tool:dqr-media",
                             "type": "uses_tool", "evidence": "declared", "source": "identity:DQR#tools",
                             "display": "Listed tool", "freshness": {"as_of": TIME,
                                                                 "status": "configured-only"}})
        repo = self.base / "dailyquranreading"
        (repo / ".git").mkdir(parents=True, exist_ok=True)
        (repo / ".vercel").mkdir(exist_ok=True)
        (repo / ".vercel/project.json").write_text(json.dumps({"projectName": "dailyquranreading",
                                                                "orgId": "test-org", "projectId": "test-project"}))
        (repo / "package.json").write_text(json.dumps({"dependencies": {
            "next": "16.1.6", "react": "19.2.3", "@supabase/supabase-js": "^2.95.3"},
            "devDependencies": {"typescript": "^5", "tailwindcss": "^4"}}))
        card = self.memory / "DQR.md"
        card.write_text(f"---\nsession: DQR\nrole: assigned\nroot: {repo}\n"
                        "owner: dailyquranreading.com — Imam El engagement\n"
                        "chat_binding: imsg:chat-19\n"
                        "tools: dqr-push, dqr-media, dqr-bind, dqr-keeper\n"
                        "approval:\n  - publishing to the live site\n---\n"
                        "## Responsibilities\n\n- Hold replies to chat 19 for approval.\n"
                        "- Sanitize site images.\n- Keep the inbox healthy.\n")
        bind = self.base / "dqr-chatbind.json"
        bind.write_text(json.dumps({"operator": "+15555550101",
                                    "operator_aliases": ["+15555550102"], "operator_chat_id": 1,
                                    "bound": [{"session": "DQR", "chat_id": 19, "guid": "fake-guid-private",
                                               "label": "DQR — Zayed + Imam El (dailyquranreading.com)",
                                               "participants": ["+15555550101", "+15555550102", "+15555550103"],
                                               "approval_chat_id": 1,
                                               "external_mode": "private-draft-and-hold", "media": True},
                                              {"session": "operator", "chat_id": 1,
                                               "participants": ["+15555550101"]}]}))
        media = self.base / "dqr-media"
        push = self.base / "dqr-push"
        chat_script = self.base / "imsg-chatbind-dqr"
        chat_script.write_text(f'DQR_MEDIA  = "{media}"\n'
                               'def handle_record(): pass\nif is_operator(sender):\n'
                               'deliver_to_session(b.get("session", "main"), text, chat_id, sender=sender)\n'
                               'hold_draft(b, sender, text, draft)\n'
                               'vck_0123456789abcdefghijklmnop\n')
        media.write_text(f'REPO="{repo}"\nPUSH={push}\nDEST_REL="public/images"\n'
                         'push=1\nif [[ "$push" == "1" ]]; then\n  "$PUSH"\nfi\n'
                         '"$EXIFTOOL" -all= "$dest"\n"$EXIFTOOL" -gps:all "$dest"\n')
        push.write_text(f'REPO="{repo}"\nWANT_NAME="haqzy"\nBRANCH="main"\n'
                        'REMOTE="https://github.com/haqzy/dailyquranreading"\n'
                        '[[ "$name" == "$WANT_NAME" ]]\n'
                        '[[ "$mail" == "$WANT_EMAIL" ]]\n'
                        '[[ "${url%.git}" == "$REMOTE" ]]\n'
                        '[[ "$cred" == "$WANT_NAME" ]]\n'
                        '[[ "$branch" == "$BRANCH" ]]\n'
                        'git push origin "$BRANCH"\n# Vercel GitHub deploys dailyquranreading.com\n')
        env = dict(self.env, FLEETDECK_FLEET_DQR_CARD=str(card),
                   FLEETDECK_FLEET_DQR_CHATBIND=str(bind),
                   FLEETDECK_FLEET_DQR_CHATBIND_SCRIPT=str(chat_script),
                   FLEETDECK_FLEET_DQR_MEDIA_SCRIPT=str(media),
                   FLEETDECK_FLEET_DQR_PUSH_SCRIPT=str(push),
                   FLEETDECK_FLEET_DQR_REPO=str(repo),
                   FLEETDECK_FLEET_DQR_GIT_CLI="/fake/git")
        def runner(argv, **kwargs):
            if argv[0] == "/fake/git":
                if argv[-2:] == ["config", "user.name"]: return "haqzy\n"
                if argv[-2:] == ["branch", "--show-current"]: return "main\n"
                if argv[-3:] == ["remote", "get-url", "origin"]:
                    return "https://github.com/haqzy/dailyquranreading\n"
            return self.runner(argv, **kwargs)
        return raw, env, runner, card, bind, media, push

    def test_dqr_focus_projects_requester_chat_id_and_own_stack_without_media(self):
        raw, env, runner, *_ = self._dqr_fixture()
        clean = R.validate_snapshot(I.enrich(raw, env, runner))
        dqr = next(n for n in clean["nodes"] if n["id"] == "session:DQR")
        facts = {item["key"]: item for item in dqr["scope"]["facts"]}
        self.assertEqual(set(facts), {"requester", "operator", "chat_binding", "git_identity",
                                      "repository", "stack", "runtime_scope", "deployment"})
        self.assertIn("handle-to-person mapping unverified", facts["requester"]["value"])
        self.assertIn("Supabase SDK 2 (declared)", facts["stack"]["value"])
        self.assertNotIn("responsibilities", dqr)
        chat = next(n for n in clean["nodes"] if n["id"] == "chat:bound-DQR")
        self.assertEqual(chat["local_chat_id"], 19)
        edges = {e["type"]: e for e in clean["edges"]}
        self.assertTrue({"chat_routes_to", "uses_workspace", "pushes_to", "triggers_deploy"} <= set(edges))
        self.assertFalse({"holds_draft", "approves_draft", "releases_reply", "stages_media",
                          "writes_asset", "invokes_tool"} & set(edges))
        self.assertFalse(any(e["from"] == "session:DQR" and e["to"] == "tool:dqr-media"
                             for e in clean["edges"]))
        self.assertFalse(any(n["id"] in {"tool:dqr-media", "chat:dqr-approval", "data:dqr-held-drafts"}
                             for n in clean["nodes"]))
        dqr_route = next(e for e in clean["edges"] if e["id"] == "chat_routes_to:dqr")
        self.assertIn("operator instructions", dqr_route["payload"])
        self.assertIn("external text held", dqr_route["payload"])
        self.assertEqual(edges["triggers_deploy"]["evidence"], "declared")
        self.assertIn("not observed", edges["triggers_deploy"]["display"])
        self.assertTrue(any(x["kind"] == "out_of_scope_coupling" for x in clean["unknowns"]))
        serialized = json.dumps(clean)
        for private in ("chat-19", "fake-guid-private", "+15555550101", "+15555550102",
                        "+15555550103", "vck_0123456789abcdefghijklmnop", str(self.base)):
            self.assertNotIn(private, serialized)

    def test_dqr_mismatches_omit_chat_id_and_requester(self):
        raw, env, runner, card, bind, media, push = self._dqr_fixture()
        duplicate = json.loads(bind.read_text())
        duplicate["bound"].append({"session": "other", "chat_id": 19})
        bind.write_text(json.dumps(duplicate))
        clean = R.validate_snapshot(I.enrich(raw, env, runner))
        self.assertNotIn("local_chat_id", next(n for n in clean["nodes"] if n["id"] == "chat:bound-DQR"))
        for invalid in (-7, 0, 1_000_001):
            changed = json.loads(bind.read_text())
            changed["bound"][0]["chat_id"] = invalid
            bind.write_text(json.dumps(changed))
            card.write_text(card.read_text().replace("imsg:chat-19", f"imsg:chat-{invalid}"))
            clean = R.validate_snapshot(I.enrich(raw, env, runner))
            self.assertNotIn("local_chat_id", next(n for n in clean["nodes"] if n["id"] == "chat:bound-DQR"))
            card.write_text(card.read_text().replace(f"imsg:chat-{invalid}", "imsg:chat-19"))
            bind.write_text(json.dumps(duplicate))
        duplicate["bound"].pop()
        bind.write_text(json.dumps(duplicate))
        card.write_text(card.read_text().replace("imsg:chat-19", "imsg:chat-23"))
        clean = R.validate_snapshot(I.enrich(raw, env, runner))
        self.assertNotIn("local_chat_id", next(n for n in clean["nodes"] if n["id"] == "chat:bound-DQR"))
        self.assertNotIn("chat_binding", {f["key"] for f in next(n for n in clean["nodes"]
                                                   if n["id"] == "session:DQR")["scope"]["facts"]})
        card.write_text(card.read_text().replace("imsg:chat-23", "imsg:chat-19"))
        binding = json.loads(bind.read_text())
        binding["bound"][0]["label"] = "DQR private chat"
        bind.write_text(json.dumps(binding))
        clean = R.validate_snapshot(I.enrich(raw, env, runner))
        self.assertNotIn("requester", {f["key"] for f in next(n for n in clean["nodes"]
                                               if n["id"] == "session:DQR")["scope"]["facts"]})
        push.write_text(push.read_text().replace('WANT_NAME="haqzy"', 'WANT_NAME="other"'))
        clean = R.validate_snapshot(I.enrich(raw, env, runner))
        self.assertFalse({"pushes_to", "triggers_deploy"} & {e["type"] for e in clean["edges"]})

    def test_dqr_missing_vercel_metadata_does_not_assert_production_route(self):
        raw, env, runner, *_ = self._dqr_fixture()
        (Path(env["FLEETDECK_FLEET_DQR_REPO"]) / ".vercel/project.json").unlink()
        clean = R.validate_snapshot(I.enrich(raw, env, runner))
        types = {e["type"] for e in clean["edges"]}
        self.assertIn("pushes_to", types)
        self.assertNotIn("triggers_deploy", types)
        self.assertNotIn("deployment", {f["key"] for f in next(n for n in clean["nodes"]
                                                  if n["id"] == "session:DQR")["scope"]["facts"]})

    def test_dqr_missing_sources_omit_unverified_facts(self):
        raw, env, runner, card, bind, media, push = self._dqr_fixture()
        card.unlink()
        clean = R.validate_snapshot(I.enrich(raw, env, runner))
        dqr = next(n for n in clean["nodes"] if n["id"] == "session:DQR")
        keys = {f["key"] for f in dqr.get("scope", {}).get("facts", [])}
        self.assertFalse({"requester", "chat_binding", "git_identity", "repository",
                          "stack", "deployment"} & keys)
        self.assertNotIn("local_chat_id", next(n for n in clean["nodes"] if n["id"] == "chat:bound-DQR"))
        coupling = next(u for u in clean["unknowns"] if u["kind"] == "out_of_scope_coupling")
        self.assertNotIn("card lists", coupling["detail"])

        raw, env, runner, card, bind, media, push = self._dqr_fixture()
        raw["edges"] = [e for e in raw["edges"] if e["type"] != "chat_routes_to" or e["to"] != "session:DQR"]
        clean = R.validate_snapshot(I.enrich(raw, env, runner))
        self.assertNotIn("local_chat_id", next(n for n in clean["nodes"] if n["id"] == "chat:bound-DQR"))

        raw, env, runner, card, bind, media, push = self._dqr_fixture()
        (Path(env["FLEETDECK_FLEET_DQR_REPO"]) / "package.json").unlink()
        clean = R.validate_snapshot(I.enrich(raw, env, runner))
        keys = {f["key"] for f in next(n for n in clean["nodes"]
                                      if n["id"] == "session:DQR")["scope"]["facts"]}
        self.assertNotIn("stack", keys)

    def test_dqr_filter_preserves_other_sessions_media_edges(self):
        raw, env, runner, *_ = self._dqr_fixture()
        raw["nodes"].extend([
            {"id": "session:other", "type": "session", "label": "Other", "parent_id": "host:sample",
             "declared": True, "observed": True, "source_refs": ["tmux"], "observed_at": TIME},
            {"id": "data:other-draft", "type": "data", "label": "Other draft", "parent_id": None,
             "declared": True, "observed": None, "source_refs": ["other-source"], "observed_at": None},
            {"id": "chat:dqr-approval", "type": "chat", "label": "Old approval chat", "parent_id": None,
             "declared": True, "observed": None, "source_refs": ["infra:dqr_chatbind"], "observed_at": None},
            {"id": "data:dqr-held-drafts", "type": "data", "label": "Old held drafts", "parent_id": None,
             "declared": True, "observed": None, "source_refs": ["infra:dqr_chatbind_script"], "observed_at": None},
        ])
        raw["edges"].extend([
            {"id": "holds_draft:dqr", "from": "chat:bound-DQR", "to": "data:dqr-held-drafts",
             "type": "holds_draft", "evidence": "declared", "source": "infra:dqr_chatbind_script",
             "layer": "routing", "payload": "draft", "display": "Old DQR draft",
             "freshness": {"as_of": TIME, "status": "configured-only"}},
            {"id": "stages_media:dqr", "from": "chat:bound-DQR", "to": "tool:dqr-media",
             "type": "stages_media", "evidence": "declared", "source": "infra:dqr_media",
             "layer": "routing", "payload": "image", "display": "Old DQR image",
             "freshness": {"as_of": TIME, "status": "configured-only"}},
        ])
        raw["edges"].append({"id": "holds_draft:other", "from": "session:other", "to": "data:other-draft",
                             "type": "holds_draft", "evidence": "declared", "source": "other-source",
                             "layer": "routing", "payload": "other session text", "display": "Other session draft",
                             "freshness": {"as_of": TIME, "status": "configured-only"}})
        clean = R.validate_snapshot(I.enrich(raw, env, runner))
        self.assertIn("holds_draft:other", {e["id"] for e in clean["edges"]})
        self.assertFalse({"holds_draft:dqr", "stages_media:dqr"} & {e["id"] for e in clean["edges"]})
        node_ids = {n["id"] for n in clean["nodes"]}
        self.assertFalse({"chat:dqr-approval", "data:dqr-held-drafts", "tool:dqr-media"} & node_ids)
        self.assertTrue(all(e["from"] in node_ids and e["to"] in node_ids for e in clean["edges"]))

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
                      {"payload": "vck_0123456789abcdefghijklmnop"},
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
