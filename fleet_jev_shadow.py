#!/usr/bin/env python3
"""Fixed, synthetic Jev routing rehearsal. No live messages or dispatch.

The HTTP shape follows Vercel's Evaluation API:
https://vercel.com/docs/ai-gateway/modalities/evaluation
Only the CLI's ``--synthetic-smoke`` mode exists. This module is not imported by
the fleet-map listener or the live iMessage router.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


ENDPOINT = "https://ai-gateway.vercel.sh/v1/evaluate"
MODEL = "typesafe-ai/jev"
PROVIDER = "typesafe-ai"
QUESTION = "route"
TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 32 * 1024

# Deliberately fictitious, short, and fixed. No live session names, card text,
# message bodies, inventory, or user-supplied strings reach the provider.
SYNTHETIC_STATE = (
    "Synthetic request: explain how a demo fleet map reaches a phone over a "
    "private network. This is test data, not an operator message."
)
CANDIDATES = {
    "trace_demo": "Demo coordinator: explains agent roles and request routing.",
    "network_demo": "Demo network specialist: explains private network access and proxies.",
    "needs_review": "Choose when the request does not clearly fit one demo role.",
}


class ShadowFailure(Exception):
    """A safe status code; provider errors and response bodies are never shown."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(status)


def _secret_file() -> Path:
    return Path.home() / ".config" / "wideband" / "secrets" / "ai-gateway.env"


def _valid_key(value: str | None) -> bool:
    return bool(value and 16 <= len(value) <= 512 and
                all(33 <= ord(char) <= 126 and char not in "\"'\\" for char in value))


def load_key(*, env: dict[str, str] | None = None, secret_file: Path | None = None) -> str:
    """Read one credential without evaluating shell syntax or revealing it.

    An environment variable takes precedence. The fallback file must be owned
    by the current user, regular, nonsymlinked, and inaccessible to the group
    or others. Only a literal AI_GATEWAY_API_KEY assignment is accepted.
    """
    env = os.environ if env is None else env
    value = env.get("AI_GATEWAY_API_KEY")
    if value:
        if not _valid_key(value):
            raise ShadowFailure("invalid_credential")
        return value

    path = _secret_file() if secret_file is None else secret_file
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_size > 2048):
            raise ShadowFailure("invalid_credential_source")
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise ShadowFailure("missing_credential") from None
    except (OSError, UnicodeError):
        raise ShadowFailure("invalid_credential_source") from None

    assignments = [line.strip() for line in lines if line.strip() and
                   not line.lstrip().startswith("#")]
    if len(assignments) != 1:
        raise ShadowFailure("invalid_credential_source")
    match = re.fullmatch(
        r"(?:export\s+)?AI_GATEWAY_API_KEY=(?:'([^']+)'|\"([^\"]+)\"|([^\s'\"]+))",
        assignments[0],
    )
    if not match:
        raise ShadowFailure("invalid_credential_source")
    value = next((part for part in match.groups() if part is not None), None)
    if not _valid_key(value):
        raise ShadowFailure("invalid_credential")
    return value


def synthetic_payload() -> dict:
    return {
        "model": MODEL,
        "state": SYNTHETIC_STATE,
        "questions": {
            QUESTION: {
                "type": "choice",
                "instructions": (
                    "Choose the best described demo role for this synthetic request. "
                    "Choose needs_review if unclear."
                ),
                "criteria": dict(CANDIDATES),
            }
        },
        "providerOptions": {
            "gateway": {"zeroDataRetention": True, "only": [PROVIDER]}
        },
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _post(request: urllib.request.Request, *, timeout: float) -> bytes:
    # A redirect must not forward the Authorization header to another host.
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ShadowFailure("provider_unavailable")
        body = response.read(MAX_RESPONSE_BYTES + 1)
    return body


def _reject_constant(_value: str):
    raise ShadowFailure("invalid_response")


def _validate_response(body: bytes) -> tuple[str, str]:
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise ShadowFailure("invalid_response")
    try:
        data = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeError, TypeError):
        raise ShadowFailure("invalid_response") from None
    if not isinstance(data, dict) or data.get("model") != MODEL:
        raise ShadowFailure("invalid_response")
    answers = data.get("answers")
    answer = answers.get(QUESTION) if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise ShadowFailure("invalid_response")
    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in CANDIDATES:
        raise ShadowFailure("invalid_response")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(CANDIDATES):
        raise ShadowFailure("invalid_response")
    for probability in probabilities.values():
        if (isinstance(probability, bool) or not isinstance(probability, (int, float)) or
                not math.isfinite(probability) or not 0 <= probability <= 1):
            raise ShadowFailure("invalid_response")
    if not 0.95 <= sum(probabilities.values()) <= 1.05:
        raise ShadowFailure("invalid_response")

    metadata = data.get("providerMetadata")
    gateway = metadata.get("gateway") if isinstance(metadata, dict) else None
    routing = gateway.get("routing") if isinstance(gateway, dict) else None
    provider = routing.get("finalProvider") if isinstance(routing, dict) else None
    if provider != PROVIDER:
        raise ShadowFailure("invalid_response")
    return choice, provider


def evaluate_synthetic(
    key: str,
    *,
    post: Callable[..., bytes] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Make one fixed evaluation request and return a privacy-safe summary."""
    if not _valid_key(key):
        raise ShadowFailure("invalid_credential")
    body = json.dumps(synthetic_payload(), separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT, data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST",
    )
    started = clock()
    try:
        raw = (post or _post)(request, timeout=TIMEOUT_SECONDS)
        choice, provider = _validate_response(raw)
    except ShadowFailure:
        raise
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ShadowFailure("provider_unavailable") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ShadowFailure("provider_unavailable") from None
    except Exception:
        # No raw exception text: a transport may include the URL or a header.
        raise ShadowFailure("provider_unavailable") from None
    elapsed = clock() - started
    if not math.isfinite(elapsed) or elapsed < 0:
        raise ShadowFailure("invalid_latency")
    return {"status": "ok", "choice": choice, "model": MODEL,
            "provider": provider, "latency_ms": round(elapsed * 1000)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic-smoke", action="store_true",
                        help="evaluate the built-in synthetic request only")
    args = parser.parse_args(argv)
    if not args.synthetic_smoke:
        parser.error("--synthetic-smoke is required")
    try:
        result = evaluate_synthetic(load_key())
    except ShadowFailure as exc:
        result = {"status": exc.status, "choice": None, "model": MODEL,
                  "provider": "unknown", "latency_ms": None}
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
