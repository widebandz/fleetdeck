#!/usr/bin/env python3
"""Frozen synthetic Jev routing trial; suggestions only, with no dispatch.

The labels below are *declared tmux session roles*, not verified agent
identities or currently eligible destinations. The descriptions paraphrase the
session purposes in ~/.imsg-routing.json as inspected on 2026-09-24. This
module deliberately does not read that file, live messages, or session state.
The only CLI mode evaluates the built-in fixtures through Vercel AI Gateway.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

import fleet_jev_shadow as gateway


FIXTURE_VERSION = "2026-09-24.v1"


@dataclass(frozen=True)
class RouteCase:
    id: str
    text: str
    expected: str


# Frozen test menu. Names denote declared sessions, not registry agent IDs.
CANDIDATES = {
    "trace": "Operator companion: interprets requests, proposes session ownership, and carries answers back through bound chat.",
    "UI": "Interface and visual design across Wideband apps, including fleetdeck phone screens, layout, UX, and frontend polish.",
    "media": "Flow-wideband-ai media production: still article heroes, galleries, imagery, reels, and carousels.",
    "video": "Flow-wideband-ai video and motion work, including motion-studio and Remotion renders.",
    "tunnel": "Wb-tunnel connectivity, Tailscale exposure, and tunnel configuration.",
    "prod": "Mac system tooling, including machine-wide headphone EQ profiles and system utilities.",
    "GHL": "GoHighLevel CRM mirror: sales_leads sync, pipeline stages, and webhook reconciliation.",
    "pillars": "Imam John's bilingual RAG: corpus ingestion, transcription, and Arabic refusal behavior.",
    "pool": "Daily View Pools client engagement: handoff, questionnaire editor, quote subdomain, and Supabase project.",
    "sop": "Flow-wideband-ai standard operating procedures and process documents.",
    "needs_review": "Choose when no one declared role clearly owns the whole request, its scope is unclear, or it is outside these roles.",
}

# Gold labels were fixed before the first live batch. All texts are invented.
CASES = (
    RouteCase("trace_route_explanation", "Summarize the current session roles and explain how a request gets proposed for operator confirmation.", "trace"),
    RouteCase("ui_mobile_layout", "On the fleetdeck phone front screen, labels overlap the navigation. Redesign the small-screen layout.", "UI"),
    RouteCase("media_article_images", "Create a still article hero and image gallery for a draft story in flow-wideband-ai.", "media"),
    RouteCase("video_remotion_render", "The Remotion export from motion-studio drops frames. Fix the animation render.", "video"),
    RouteCase("tunnel_tailscale_proxy", "The wb-tunnel Tailscale Serve URL returns 502. Inspect its proxy target and connectivity.", "tunnel"),
    RouteCase("prod_headphone_eq", "The Mac-wide headphone EQ profile switch stopped working. Repair the system utility.", "prod"),
    RouteCase("ghl_webhook_replay", "Reconcile duplicate sales_leads after a GoHighLevel webhook replay.", "GHL"),
    RouteCase("pillars_arabic_rag", "Investigate Arabic refusal behavior after transcript ingestion in Imam John's bilingual RAG.", "pillars"),
    RouteCase("pool_quote_handoff", "Update the Daily View Pools questionnaire editor and quote subdomain handoff.", "pool"),
    RouteCase("sop_publication", "Write the flow-wideband-ai standard operating procedure for approving article publication.", "sop"),
    RouteCase("review_phone_link", "The phone page fails when opened through a Tailscale link; fix it.", "needs_review"),
    RouteCase("review_crm_video", "Reconcile the GoHighLevel sales leads and produce a Remotion video about the results.", "needs_review"),
    RouteCase("review_no_fit", "Authorize a bank transfer from my checking account.", "needs_review"),
)
CASE_BY_ID = {case.id: case for case in CASES}


class RouteFailure(Exception):
    """A fixed, safe status; never carries a provider body or credential."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(status)


def payload(case_id: str) -> dict:
    case = CASE_BY_ID.get(case_id)
    if case is None:
        raise RouteFailure("invalid_fixture")
    return {
        "model": gateway.MODEL,
        "state": "Synthetic routing fixture, not an operator command. Request: " + case.text,
        "questions": {
            gateway.QUESTION: {
                "type": "choice",
                "instructions": (
                    "Select one declared session role for the entire request. "
                    "Choose needs_review when ownership is ambiguous, the request "
                    "has multiple owners, or none of these roles fits."
                ),
                "criteria": dict(CANDIDATES),
            }
        },
        "providerOptions": {
            "gateway": {"zeroDataRetention": True, "only": [gateway.PROVIDER]}
        },
    }


def _reject_constant(_value: str):
    raise RouteFailure("invalid_response")


def _validate_response(body: bytes) -> tuple[str, dict[str, float]]:
    if not isinstance(body, bytes) or len(body) > gateway.MAX_RESPONSE_BYTES:
        raise RouteFailure("invalid_response")
    try:
        data = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeError, TypeError):
        raise RouteFailure("invalid_response") from None
    if not isinstance(data, dict) or data.get("model") != gateway.MODEL:
        raise RouteFailure("invalid_response")
    answers = data.get("answers")
    answer = answers.get(gateway.QUESTION) if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise RouteFailure("invalid_response")
    choice = answer.get("choice")
    probabilities = answer.get("probabilities")
    if (not isinstance(choice, str) or choice not in CANDIDATES or
            not isinstance(probabilities, dict) or set(probabilities) != set(CANDIDATES)):
        raise RouteFailure("invalid_response")
    for probability in probabilities.values():
        if (isinstance(probability, bool) or not isinstance(probability, (int, float)) or
                not math.isfinite(probability) or not 0 <= probability <= 1):
            raise RouteFailure("invalid_response")
    if not 0.95 <= sum(probabilities.values()) <= 1.05:
        raise RouteFailure("invalid_response")
    metadata = data.get("providerMetadata")
    gateway_meta = metadata.get("gateway") if isinstance(metadata, dict) else None
    routing = gateway_meta.get("routing") if isinstance(gateway_meta, dict) else None
    provider = routing.get("finalProvider") if isinstance(routing, dict) else None
    if provider != gateway.PROVIDER:
        raise RouteFailure("invalid_response")
    return choice, probabilities


def evaluate_case(
    case_id: str,
    key: str,
    *,
    post: Callable[..., bytes] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Evaluate one allowlisted fixture and return only a bounded summary."""
    case = CASE_BY_ID.get(case_id)
    if case is None:
        raise RouteFailure("invalid_fixture")
    if not gateway._valid_key(key):
        raise RouteFailure("invalid_credential")
    request = urllib.request.Request(
        gateway.ENDPOINT,
        data=json.dumps(payload(case_id), separators=(",", ":")).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST",
    )
    started = clock()
    status, choice, probabilities = "ok", None, None
    try:
        raw = (post or gateway._post)(request, timeout=gateway.TIMEOUT_SECONDS)
        choice, probabilities = _validate_response(raw)
    except RouteFailure as exc:
        status = exc.status
    except gateway.ShadowFailure:
        status = "provider_unavailable"
    except urllib.error.HTTPError as exc:
        exc.close()
        status = "provider_unavailable"
    except (urllib.error.URLError, TimeoutError, OSError):
        status = "provider_unavailable"
    except Exception:
        # A transport error may contain request headers; never stringify it.
        status = "provider_unavailable"
    elapsed = clock() - started
    if not math.isfinite(elapsed) or elapsed < 0:
        status, choice, probabilities, elapsed = "invalid_latency", None, None, 0.0
    return {
        "id": case.id,
        "expected": case.expected,
        "status": status,
        "choice": choice,
        "confidence": probabilities[choice] if probabilities is not None else None,
        "probabilities": probabilities,
        "latency_ms": round(elapsed * 1000),
    }


def summarize(results: list[dict]) -> dict:
    """Score valid responses only; provider failures and unrun cases are separate."""
    clear = [result for result in results if result["expected"] != "needs_review" and result["status"] == "ok"]
    review = [result for result in results if result["expected"] == "needs_review" and result["status"] == "ok"]
    clear_correct = sum(result["choice"] == result["expected"] for result in clear)
    review_correct = sum(result["choice"] == "needs_review" for result in review)
    return {
        "clear_correct": clear_correct,
        "clear_total": len(clear),
        "clear_accuracy": clear_correct / len(clear) if clear else None,
        "review_correct": review_correct,
        "review_total": len(review),
        "review_recall": review_correct / len(review) if review else None,
        "review_false_owner_choices": sum(result["choice"] != "needs_review" for result in review),
        "provider_or_contract_failures": sum(result["status"] != "ok" for result in results),
        "attempted": len(results),
        "unrun": len(CASES) - len(results),
    }


def run_suite(key: str, *, post: Callable[..., bytes] | None = None,
              clock: Callable[[], float] = time.monotonic) -> dict:
    if not gateway._valid_key(key):
        raise RouteFailure("invalid_credential")
    results = []
    for case in CASES:
        result = evaluate_case(case.id, key, post=post, clock=clock)
        results.append(result)
        # Auth, rate-limit, network, or response-contract problems may persist.
        # Leave later fixtures unrun rather than spending or scoring them.
        if result["status"] != "ok":
            break
    return {
        "mode": "synthetic_shadow",
        "fixture_version": FIXTURE_VERSION,
        "label_basis": "frozen declared session roles; not verified agents or live eligibility",
        "trial_limit": "First sanity trial: clear texts use domain terms from role descriptions; not a generalization benchmark.",
        "model": gateway.MODEL,
        "provider": gateway.PROVIDER,
        "cases": results,
        "summary": summarize(results),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="evaluate only the built-in synthetic fixtures")
    args = parser.parse_args(argv)
    if not args.run:
        parser.error("--run is required")
    try:
        report = run_suite(gateway.load_key())
    except (gateway.ShadowFailure, RouteFailure) as exc:
        report = {"status": exc.status, "mode": "synthetic_shadow", "cases": [], "summary": None}
    print(json.dumps(report, separators=(",", ":")))
    return 0 if report.get("summary") and not report["summary"]["provider_or_contract_failures"] else 1


if __name__ == "__main__":
    sys.exit(main())
