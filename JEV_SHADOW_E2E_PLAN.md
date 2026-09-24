# Jev gateway: first integration and end-to-end plan

Status: 2026-09-24. This plan keeps Jev as a **shadow recommendation** until
the existing router can be changed and verified from versioned source. The
tailnet fleet-map preview remains read-only.

## What is connected now

- The Vercel AI Gateway credential is stored outside this repository at
  `~/.config/wideband/secrets/ai-gateway.env` (directory `0700`, file `0600`).
  No browser code or fleet-map LaunchAgent receives it.
- A synthetic request to `POST https://ai-gateway.vercel.sh/v1/evaluate` with
  `model: typesafe-ai/jev` returned HTTP 200, the named `weather` choice, a
  complete probability distribution, and the TypeSafe provider. It sent no
  operator message or fleet data and triggered no route.
- The standalone `fleet_jev_shadow.py` probe is the versioned client for this
  call. It accepts only its built-in synthetic fixture from the CLI. Its
  client validates the fixed candidate set and provider response; it cannot
  dispatch to tmux, chat, or the registry. It is a contract test, not a live
  shadow router yet.

The suggested `npx vercel ai-gateway setup` configures coding agents to route
their model traffic through Gateway and can rewrite Codex/Claude settings.
The installed Vercel CLI has no `setup` subcommand. Direct Gateway evaluation
is the relevant integration for Jev in the fleet software.

## Target architecture

```text
machine-readable agent definition (desired)
  → validated builder and registry/card/runtime configuration
  → observed fleet graph (what exists)
  → request events and measured timings (what happened)
  → Jev typed routing/review suggestion + code-enforced policy
  → map of intended, observed, and measured evidence
```

The proposed agent definition should identify its stable ID, owner, mission,
responsibilities and boundaries, runtime/session, workspace policy, tools,
skills, instruction sources, allowed inbound/outbound routes, dependencies,
and expected event types. A candidate field marked **unknown** must not be
filled by visual inference. Code validates required fields and permissions;
Jev handles bounded judgments such as choosing among eligible agents.

## First use case: shadow routing

1. Build the candidate set from the live router's authoritative policy and
   current session readiness, with a `needs_review` option. The map's
   computed routing edges are informative snapshot evidence, not a dispatch
   allowlist. Add recent project/worktree ownership as sourced context when
   available; an agent's own report is a claim, not a verified binding.
2. Send a minimal, redacted task summary and candidate descriptions to Jev.
   Ask one `choice` question. Require `zeroDataRetention` and the TypeSafe
   provider in Gateway options. Record only model, candidate IDs, chosen ID,
   probabilities, response time, and source/freshness metadata in a private
   shadow event; omit raw chat text, tokens, and provider error bodies.
3. Compare Jev's suggestion with the current router's proposal and a human
   label. Every decision remains advisory. Explicit session commands,
   operator confirmation for inferred routes, and the final live tmux/pane
   recheck continue under the existing router.
4. Calibrate an abstention threshold from labeled cases before any suggestion
   influences the router. An invalid, unknown, stale, or unavailable candidate
   yields `needs_review`.

The installed `~/bin/imsg-router` has the current proposal/confirmation logic,
but its checked-in source was not found in the audited workspaces. The `req`
ticket shim points to a missing ledger program, so a new live ticket cannot
yet be relied on as outcome telemetry. Give both a versioned, working path
before changing live routing or claiming end-to-end completion timing.

## Implementation sequence

1. Define and validate versioned agent records. Keep declared roles separate
   from observed session/process bindings and mark missing bindings unknown.
2. Put the live router and ticket ledger under a working versioned path. Add a
   private request event trail with a correlation ID, event type, timestamp,
   agent/session ID when verified, source, and outcome. Capture intake,
   proposal, confirmation, dispatch, completion, failure, and retry events.
3. Build a shadow adapter beside the router. At decision time, derive eligible
   candidates from router policy and live session readiness, redact the task,
   invoke Jev, and persist a bounded decision record. If candidate readiness
   changes, the suggestion expires. Provider failure returns `needs_review`.
4. Replay human-labeled clear, ambiguous, no-fit, stale, and provider-failure
   cases. Compare Jev with the current proposal, report agreement and
   abstention, and calibrate a review threshold from those cases.
5. Compute measured stage durations and retry counts from the event trail;
   show them on the map only where the request ID links to verified entities.
   Keep declared, observed, and measured evidence visibly distinct.

## Test gates

| Gate | What to exercise | Pass condition |
| --- | --- | --- |
| Credential | Private file and live synthetic Gateway call | Mode `0600`; HTTP/TLS success; expected model/provider; no key in repo, output, or browser API. |
| Contract | Mock success, missing key, 401/429/5xx, timeout, malformed JSON, invalid choice/probabilities | Only allowed choices accepted; generic failure/abstention; no route side effect. |
| Shadow cases | Human-labeled clear owner, ambiguous owner, no fit, stale/empty candidate set | Agreement and abstention measured; no probabilistic result treated as certain. |
| Router boundary | Explicit target, inferred target, refusal/timeout, confirmation, stale pane | Existing target override and approval rules hold; no Jev suggestion sends chat or tmux keys. |
| Map boundary | Desktop/mobile tailnet URL, HTML/API, POST and legacy paths | Layers remain readable; GET map/API 200; POST 405; no credential, task text, or provider response in JSON. |
| Efficiency | Synthetic request event sequence, then real instrumented runs | Code computes intake→proposal, approval wait, dispatch→completion, retry counts; Jev labels review reasons separately. |

The current fleet map can show configuration and live tmux/process state.
It does not yet collect per-request events, tool calls, costs, or completion
outcomes. Ticket files contain coarse lifecycle timestamps, but the broken
ticket shim and absent spans prevent a trustworthy live efficiency claim.

The first frozen, synthetic declared-role batch and its one ambiguous-route
miss are recorded in [JEV_ROUTE_TRIAL_20260924.md](JEV_ROUTE_TRIAL_20260924.md).
It tests classification only; the next gate is independently labeled,
operator-style texts and live readiness checks.

References: [Vercel evaluation API](https://vercel.com/docs/ai-gateway/modalities/evaluation),
[Gateway authentication](https://vercel.com/docs/ai-gateway/authentication-and-byok),
[Vercel setup command scope](https://vercel.com/ai-gateway).
