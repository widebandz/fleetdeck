"""Offline contract checks for the synthetic, non-dispatching Jev client."""

import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import fleet_jev_shadow as J


FAKE_KEY = "fixture-key-never-used-live"


def response(*, choice="network_demo"):
    return {
        "model": J.MODEL,
        "answers": {"route": {
            "type": "choice", "choice": choice,
            "probabilities": {"trace_demo": 0.1, "network_demo": 0.8,
                              "needs_review": 0.1},
        }},
        "providerMetadata": {"gateway": {"routing": {"finalProvider": J.PROVIDER}}},
    }


class JevShadowTests(unittest.TestCase):
    def test_fixed_request_contract_and_safe_result(self):
        calls = []

        def post(request, *, timeout):
            calls.append((request, timeout))
            return json.dumps(response()).encode()

        ticks = iter((10.0, 10.125))
        result = J.evaluate_synthetic(FAKE_KEY, post=post, clock=lambda: next(ticks))
        self.assertEqual(result, {"status": "ok", "choice": "network_demo",
                                  "model": J.MODEL, "provider": J.PROVIDER,
                                  "latency_ms": 125})
        self.assertEqual(len(calls), 1)
        request, timeout = calls[0]
        self.assertEqual(request.full_url, "https://ai-gateway.vercel.sh/v1/evaluate")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, J.TIMEOUT_SECONDS)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_KEY)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        body = json.loads(request.data)
        self.assertEqual(body, J.synthetic_payload())
        self.assertEqual(body["model"], "typesafe-ai/jev")
        self.assertEqual(body["questions"]["route"]["type"], "choice")
        self.assertEqual(set(body["questions"]["route"]["criteria"]),
                         {"trace_demo", "network_demo", "needs_review"})
        self.assertEqual(body["providerOptions"]["gateway"],
                         {"zeroDataRetention": True, "only": ["typesafe-ai"]})
        self.assertNotIn(FAKE_KEY, json.dumps(result))

    def test_private_credential_file_and_env_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ai-gateway.env"
            path.write_text("# local only\nexport AI_GATEWAY_API_KEY='" + FAKE_KEY + "'\n")
            os.chmod(path, 0o600)
            self.assertEqual(J.load_key(env={}, secret_file=path), FAKE_KEY)
            other = "different-fixture-key-123"
            self.assertEqual(J.load_key(env={"AI_GATEWAY_API_KEY": other},
                                        secret_file=path), other)
            os.chmod(path, 0o644)
            with self.assertRaises(J.ShadowFailure) as failure:
                J.load_key(env={}, secret_file=path)
            self.assertEqual(failure.exception.status, "invalid_credential_source")

    def test_missing_credential_never_contacts_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(J.ShadowFailure) as failure:
                J.load_key(env={}, secret_file=Path(directory) / "missing.env")
            self.assertEqual(failure.exception.status, "missing_credential")
        with self.assertRaises(J.ShadowFailure) as failure:
            J.evaluate_synthetic("short", post=lambda *_args, **_kw: self.fail("network"))
        self.assertEqual(failure.exception.status, "invalid_credential")

    def test_invalid_response_shapes_are_rejected(self):
        cases = []
        wrong_model = response(); wrong_model["model"] = "another/model"; cases.append(wrong_model)
        wrong_type = response(); wrong_type["answers"]["route"]["type"] = "boolean"; cases.append(wrong_type)
        unknown = response(choice="unlisted"); cases.append(unknown)
        missing_prob = response(); del missing_prob["answers"]["route"]["probabilities"]["needs_review"]; cases.append(missing_prob)
        wrong_provider = response(); wrong_provider["providerMetadata"]["gateway"]["routing"]["finalProvider"] = "other"; cases.append(wrong_provider)
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(J.ShadowFailure) as failure:
                    J._validate_response(json.dumps(raw).encode())
                self.assertEqual(failure.exception.status, "invalid_response")

    def test_nonfinite_out_of_range_or_boolean_probabilities_are_rejected(self):
        for invalid in (float("nan"), float("inf"), -0.1, 1.1, True, "0.8"):
            raw = response()
            raw["answers"]["route"]["probabilities"]["network_demo"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(J.ShadowFailure):
                J._validate_response(json.dumps(raw).encode())

    def test_malformed_and_oversized_response_are_rejected(self):
        for body in (b"not-json", b"[]", b"{\"model\":NaN}",
                     b"x" * (J.MAX_RESPONSE_BYTES + 1)):
            with self.subTest(length=len(body)), self.assertRaises(J.ShadowFailure) as failure:
                J._validate_response(body)
            self.assertEqual(failure.exception.status, "invalid_response")

    def test_http_and_timeout_errors_never_include_provider_body(self):
        secret_marker = "sensitive-provider-error-body"

        def http_error(_request, *, timeout):
            raise urllib.error.HTTPError(J.ENDPOINT, 401, secret_marker, {},
                                         io.BytesIO(secret_marker.encode()))

        for post in (http_error,
                     lambda _request, *, timeout: (_ for _ in ()).throw(TimeoutError(secret_marker))):
            with self.subTest(post=post), self.assertRaises(J.ShadowFailure) as failure:
                J.evaluate_synthetic(FAKE_KEY, post=post)
            self.assertEqual(failure.exception.status, "provider_unavailable")
            self.assertNotIn(secret_marker, str(failure.exception))

    def test_cli_rejects_arbitrary_input_before_loading_credentials(self):
        with mock.patch.object(J, "load_key") as key_loader, contextlib.redirect_stderr(io.StringIO()):
            for argv in ([], ["--text", "live message"],
                         ["--synthetic-smoke", "--target", "trace"]):
                with self.subTest(argv=argv), self.assertRaises(SystemExit) as failure:
                    J.main(argv)
                self.assertEqual(failure.exception.code, 2)
            key_loader.assert_not_called()

    def test_cli_prints_only_safe_summary(self):
        out = io.StringIO()
        with mock.patch.object(J, "load_key", return_value=FAKE_KEY), \
             mock.patch.object(J, "evaluate_synthetic", return_value={
                 "status": "ok", "choice": "needs_review", "model": J.MODEL,
                 "provider": J.PROVIDER, "latency_ms": 7,
             }), contextlib.redirect_stdout(out):
            self.assertEqual(J.main(["--synthetic-smoke"]), 0)
        printed = json.loads(out.getvalue())
        self.assertEqual(set(printed), {"status", "choice", "model", "provider", "latency_ms"})
        self.assertNotIn(FAKE_KEY, out.getvalue())


if __name__ == "__main__":
    unittest.main()
