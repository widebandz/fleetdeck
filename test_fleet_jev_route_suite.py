"""Offline contract and safety checks for the frozen Jev routing trial."""

import contextlib
import io
import json
import os
import subprocess
import unittest
import urllib.error
from unittest import mock

import fleet_jev_route_suite as suite


FAKE_KEY = "fixture-key-never-used-live"


def response(choice="UI", *, probabilities=None):
    if probabilities is None:
        probabilities = {name: (0.91 if name == choice else 0.09 / (len(suite.CANDIDATES) - 1))
                         for name in suite.CANDIDATES}
    return {
        "model": suite.gateway.MODEL,
        "answers": {suite.gateway.QUESTION: {
            "type": "choice", "choice": choice,
            "probabilities": probabilities,
        }},
        "providerMetadata": {"gateway": {"routing": {
            "finalProvider": suite.gateway.PROVIDER,
        }}},
    }


class RouteSuiteTests(unittest.TestCase):
    def test_frozen_cases_have_declared_role_labels_and_review_examples(self):
        self.assertEqual(len(suite.CASES), 13)
        self.assertEqual(len(suite.CASE_BY_ID), 13)
        self.assertEqual({case.id: case.expected for case in suite.CASES}, {
            "trace_route_explanation": "trace",
            "ui_mobile_layout": "UI",
            "media_article_images": "media",
            "video_remotion_render": "video",
            "tunnel_tailscale_proxy": "tunnel",
            "prod_headphone_eq": "prod",
            "ghl_webhook_replay": "GHL",
            "pillars_arabic_rag": "pillars",
            "pool_quote_handoff": "pool",
            "sop_publication": "sop",
            "review_phone_link": "needs_review",
            "review_crm_video": "needs_review",
            "review_no_fit": "needs_review",
        })
        self.assertEqual({case.expected for case in suite.CASES}, set(suite.CANDIDATES))
        self.assertEqual(sum(case.expected == "needs_review" for case in suite.CASES), 3)
        self.assertTrue(all(case.id and case.text and len(case.text) < 250
                            for case in suite.CASES))
        self.assertIn("trace", suite.CANDIDATES)

    def test_request_contract_is_pinned_and_does_not_send_gold_label(self):
        case = suite.CASES[0]
        calls = []

        def post(request, *, timeout):
            calls.append((request, timeout))
            return json.dumps(response(case.expected)).encode()

        ticks = iter((1.0, 1.125))
        result = suite.evaluate_case(case.id, FAKE_KEY, post=post,
                                     clock=lambda: next(ticks))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["choice"], case.expected)
        self.assertEqual(result["confidence"], 0.91)
        self.assertEqual(result["latency_ms"], 125)
        self.assertEqual(len(calls), 1)
        request, timeout = calls[0]
        self.assertEqual(request.full_url, suite.gateway.ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, suite.gateway.TIMEOUT_SECONDS)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_KEY)
        body = json.loads(request.data)
        self.assertEqual(body, suite.payload(case.id))
        self.assertEqual(body["model"], "typesafe-ai/jev")
        self.assertEqual(body["questions"]["route"]["type"], "choice")
        self.assertEqual(set(body["questions"]["route"]["criteria"]),
                         set(suite.CANDIDATES))
        self.assertEqual(body["providerOptions"]["gateway"],
                         {"zeroDataRetention": True, "only": ["typesafe-ai"]})
        self.assertNotIn("expected", json.dumps(body))
        self.assertNotIn(FAKE_KEY, json.dumps(result))

    def test_only_allowlisted_fixture_ids_can_reach_transport(self):
        with self.assertRaises(suite.RouteFailure) as failure:
            suite.evaluate_case("an operator's arbitrary text", FAKE_KEY,
                                post=lambda *_a, **_kw: self.fail("network"))
        self.assertEqual(failure.exception.status, "invalid_fixture")

    def test_full_trial_records_correct_metrics_without_router_side_effects(self):
        sent = []

        def post(request, *, timeout):
            index = len(sent)
            sent.append(request)
            choice = suite.CASES[index].expected
            return json.dumps(response(choice)).encode()

        with mock.patch.object(subprocess, "run", side_effect=AssertionError("dispatch")), \
             mock.patch.object(os, "system", side_effect=AssertionError("dispatch")):
            report = suite.run_suite(FAKE_KEY, post=post)
        self.assertEqual(len(sent), 13)
        self.assertEqual([r["id"] for r in report["cases"]],
                         [case.id for case in suite.CASES])
        self.assertEqual(report["summary"], {
            "clear_correct": 10, "clear_total": 10, "clear_accuracy": 1.0,
            "review_correct": 3, "review_total": 3, "review_recall": 1.0,
            "review_false_owner_choices": 0, "provider_or_contract_failures": 0,
            "attempted": 13, "unrun": 0,
        })
        serialized = json.dumps(report)
        self.assertNotIn(FAKE_KEY, serialized)
        self.assertNotIn(suite.CASES[0].text, serialized)
        self.assertIn("not verified agents", report["label_basis"])
        self.assertIn("not a generalization benchmark", report["trial_limit"])
        self.assertEqual(report["fixture_version"], "2026-09-24.v1")

    def test_wrong_clear_and_review_choices_are_visible_in_metrics(self):
        rows = [{"expected": case.expected, "status": "ok", "choice": "media"}
                for case in suite.CASES]
        summary = suite.summarize(rows)
        self.assertEqual(summary["clear_correct"], 1)
        self.assertEqual(summary["clear_accuracy"], 1 / 10)
        self.assertEqual(summary["review_correct"], 0)
        self.assertEqual(summary["review_false_owner_choices"], 3)

    def test_invalid_response_shapes_are_rejected_without_body(self):
        bad = []
        wrong_model = response(); wrong_model["model"] = "different/model"; bad.append(wrong_model)
        wrong_choice = response("unknown"); bad.append(wrong_choice)
        wrong_type = response(); wrong_type["answers"]["route"]["type"] = "boolean"; bad.append(wrong_type)
        missing_probability = response(); del missing_probability["answers"]["route"]["probabilities"]["sop"]; bad.append(missing_probability)
        wrong_provider = response(); wrong_provider["providerMetadata"]["gateway"]["routing"]["finalProvider"] = "other"; bad.append(wrong_provider)
        for raw in bad:
            with self.subTest(raw=raw):
                result = suite.evaluate_case(suite.CASES[0].id, FAKE_KEY,
                                             post=lambda *_a, **_kw: json.dumps(raw).encode())
                self.assertEqual(result["status"], "invalid_response")
                self.assertIsNone(result["choice"])
                self.assertIsNone(result["probabilities"])

    def test_bad_probabilities_and_oversized_response_are_rejected(self):
        for bad_value in (float("nan"), float("inf"), -0.1, 1.1, True, "0.9"):
            raw = response()
            raw["answers"]["route"]["probabilities"]["UI"] = bad_value
            with self.subTest(bad_value=bad_value):
                result = suite.evaluate_case(suite.CASES[0].id, FAKE_KEY,
                                             post=lambda *_a, **_kw: json.dumps(raw).encode())
                self.assertEqual(result["status"], "invalid_response")
        for body in (b"not-json", b"[]", b"{\"model\":NaN}",
                     b"x" * (suite.gateway.MAX_RESPONSE_BYTES + 1)):
            with self.subTest(length=len(body)):
                result = suite.evaluate_case(suite.CASES[0].id, FAKE_KEY,
                                             post=lambda *_a, **_kw: body)
                self.assertEqual(result["status"], "invalid_response")

    def test_http_and_timeout_failures_have_generic_status(self):
        secret_marker = "sensitive-provider-error-body"

        def http_error(_request, *, timeout):
            raise urllib.error.HTTPError(suite.gateway.ENDPOINT, 429, secret_marker, {},
                                         io.BytesIO(secret_marker.encode()))

        def timeout(_request, *, timeout):
            raise TimeoutError(secret_marker)

        for post in (http_error, timeout):
            with self.subTest(post=post):
                result = suite.evaluate_case(suite.CASES[0].id, FAKE_KEY, post=post)
                self.assertEqual(result["status"], "provider_unavailable")
                self.assertIsNone(result["choice"])
                self.assertNotIn(secret_marker, json.dumps(result))

    def test_provider_or_contract_failure_stops_and_marks_later_cases_unrun(self):
        for bad_reply, status in ((TimeoutError("private-error"), "provider_unavailable"),
                                  (b"bad-json", "invalid_response")):
            sent = []

            def post(request, *, timeout):
                sent.append(request)
                if isinstance(bad_reply, Exception):
                    raise bad_reply
                return bad_reply

            with self.subTest(status=status):
                report = suite.run_suite(FAKE_KEY, post=post)
                self.assertEqual(len(sent), 1)
                self.assertEqual(len(report["cases"]), 1)
                self.assertEqual(report["cases"][0]["status"], status)
                self.assertEqual(report["summary"]["attempted"], 1)
                self.assertEqual(report["summary"]["unrun"], 12)
                self.assertIsNone(report["summary"]["clear_accuracy"])
                self.assertEqual(report["summary"]["clear_total"], 0)
                self.assertNotIn("private-error", json.dumps(report))

    def test_cli_rejects_arbitrary_text_before_credential_access(self):
        with mock.patch.object(suite.gateway, "load_key") as key_loader, \
             contextlib.redirect_stderr(io.StringIO()):
            for argv in ([], ["--text", "a live message"],
                         ["--run", "--target", "media"]):
                with self.subTest(argv=argv), self.assertRaises(SystemExit) as failure:
                    suite.main(argv)
                self.assertEqual(failure.exception.code, 2)
            key_loader.assert_not_called()

    def test_cli_prints_only_safe_report(self):
        out = io.StringIO()
        safe = {"mode": "synthetic_shadow", "cases": [],
                "summary": {"provider_or_contract_failures": 0}}
        with mock.patch.object(suite.gateway, "load_key", return_value=FAKE_KEY), \
             mock.patch.object(suite, "run_suite", return_value=safe), \
             contextlib.redirect_stdout(out):
            self.assertEqual(suite.main(["--run"]), 0)
        self.assertEqual(json.loads(out.getvalue()), safe)
        self.assertNotIn(FAKE_KEY, out.getvalue())


if __name__ == "__main__":
    unittest.main()
