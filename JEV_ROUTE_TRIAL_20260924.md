# Jev declared-role routing trial — 2026-09-24

The fixture suite in `fleet_jev_route_suite.py` was committed before the live
run. It sent 13 invented texts and a fixed menu of paraphrased session-purpose
descriptions to Jev through Vercel AI Gateway. Gold labels were fixed first.
The response record is `JEV_ROUTE_TRIAL_20260924.json` (case IDs, labels,
choices, probabilities, and latencies; no raw texts or credential). No
iMessage, tmux, ticket, or production router action occurred.

## Result

- Clear declared owner: **10/10** matched the predeclared session label.
- Expected `needs_review`: **2/3** selected review. The ambiguous phone page
  through Tailscale case selected `tunnel` at reported probability **0.54**;
  `needs_review` had probability **0.28**.
- Gateway/contract failures: **0**. Median call latency: **271 ms**; total of
  measured per-call latencies: **3,721 ms**.

| Fixture | Expected | Jev choice | P(choice) | Current pane |
| --- | --- | --- | ---: | --- |
| Trace routing explanation | `trace` | `trace` | 0.43 | Ready |
| Fleet map mobile layout | `UI` | `UI` | 0.98 | Ready |
| Article still images | `media` | `media` | 0.98 | Ready |
| Remotion render | `video` | `video` | 0.99 | Bare shell |
| Tailscale proxy | `tunnel` | `tunnel` | 0.99 | Bare shell |
| Mac headphone EQ | `prod` | `prod` | 0.96 | Bare shell |
| CRM webhook replay | `GHL` | `GHL` | 0.97 | Ready |
| Arabic RAG refusal | `pillars` | `pillars` | 0.98 | Bare shell |
| Pool quote handoff | `pool` | `pool` | 0.98 | Bare shell |
| Publication SOP | `sop` | `sop` | 0.98 | Ready |
| Phone page through Tailscale | `needs_review` | `tunnel` | 0.54 | Bare shell |
| CRM plus video | `needs_review` | `needs_review` | 0.81 | N/A |
| Bank transfer | `needs_review` | `needs_review` | 0.97 | N/A |

Pane readiness was read from the installed router's `live_sessions()` and
`pane_has_agent()` immediately after the trial. Its final pane check would hold
the five clear choices aimed at bare shells and the ambiguous `tunnel` choice.
That check is separate from Jev's semantic choice and may change at any time.
Read-only policy checks also showed `media: ...` takes the explicit-target path
and `look up ...` takes the question path, each bypassing the classifier.

## What this test establishes

Jev distinguished clear purposes when the synthetic text contained the same
domain terms as the role descriptions. It made one false owner suggestion on
an ambiguous request, and its `trace` choice was close to `needs_review`
(0.43 versus 0.40). The reported probabilities are model outputs, not a
calibrated guarantee. This is a first sanity trial, not a measured routing
accuracy for real operator messages.

The candidate labels are declared **tmux sessions**, not verified agents. The
registry currently has zero verified agent bindings. The fixture menu also
omits many live sessions whose router descriptions are missing, and it does
not test delivery, completion, or efficiency.

## Next trial

Freeze labels for 10–20 anonymized operator-style texts before evaluating
them. Use natural wording that omits session names and exact role keywords;
include single-owner tasks, UI/network and media/video overlaps, multi-owner
requests, no-fit requests, and a request for an unavailable owner. Score clear
matches, review recall, and every confident wrong choice. Compare with the
current local router on the same texts, then test a review rule on a separate
holdout set rather than tuning it against this batch. Keep explicit targets,
question prefixes, confirmation, and live pane checks under router policy.
