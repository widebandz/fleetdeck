# Test DQR as one scoped change agent

This test covers only **DQR → its application stack**, **Messages chat 21**,
and **the person who requests a change**. The requester must be attributed to the actual sender of
each test message: the group label names Imam El, but his handle has not been
independently verified; the current DQR inbox records observed Zayed only.

## 1. Baseline without side effects

Record the DQR repo commit and status, the live pane's working directory, and
the results of `imsg-chatbind --check`, `dqr-keeper --check`, and
`dqr-push --check`. Keep raw check output private because it can contain
contact details. All three checks passed on 2026-09-24. The pane was outside
its declared repo root. `dqr-push --check` verifies Git preconditions, not an
approval gate or an actual deployment.

## 2. Prove request routing in an isolated fixture

Use fake sender identities, a temporary inbox, and mocked tmux/iMessage
calls:

| Text request | Required result |
| --- | --- |
| Configured operator sends a labeled change request in the bound chat | DQR receives one instruction; no outbound group reply, repo write, or push. |
| Other group participant sends a labeled request | The handler records the real sender and holds a draft for operator review. Verify whether the request actually reaches DQR; current code routes it to the draft path, not the live DQR pane. |
| Wrong sender, chat, or reused approval token | No group send or change to the repository. |

This test establishes who can request work and what the bound chat actually
delivers. A group label alone is not proof of a person's identity or of agent
delivery.

## 3. Exercise one harmless site change off production

Once the requester and route are confirmed, run DQR in a disposable checkout
with its network publisher and live Git credential unavailable. Give it a
unique text request for a small copy change. DQR should produce a diff, run
the app's lint and build checks, and present the diff and test results to
Zayed. Verify the production `main` commit and live site remain unchanged.
For the deployment leg, use a temporary bare Git remote or an injected
publisher; require explicit approval before exactly one push.

Only after that boundary works should a separately authorized live publish
test verify GitHub `main` and the Vercel site. The current push wrapper checks
identity but has no prepublication approval gate, so a live publish is not a
safe first test.

## Pass criteria

- Every request has an evidenced sender, bound chat, and intended DQR target.
- The proposed change is reviewable as a diff, and lint/build pass.
- No group reply, Git push, or production change occurs before approval.
- One approval releases one action; a replay has no effect.

Do not use the installed chat daemon's `--dry` mode as a sandbox: it can still
process live state. Keep the first live smoke test text-only and read-only.
