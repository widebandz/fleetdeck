# DQR test plan: prove the core works without Media

The **Media** tmux agent is separate from DQR. The `dqr-media` name refers to
a DQR-specific image script attached to its chat binding. The first test
therefore uses plain text only and removes the Media session from the fixture.
The current image path gets its own publication-safety test.

## 0. Record a read-only baseline

Capture the current Git commit and clean/dirty status, DQR pane working
directory, and the results of `imsg-chatbind --check`, `dqr-keeper --check`,
and `dqr-push --check`. Keep the full check output private because it can
contain contact details. The three checks passed on 2026-09-24; the pane was
outside its declared repo root. A passing push precondition check does not
test production approval or deploy.

## 1. Test the chat contract in an isolated fixture

Use fake contacts, a temporary inbox and held-draft directory, a fake clock,
and mocked tmux/iMessage/Claude calls. Import the handler functions or extract
the routing logic behind injected interfaces; do not run the installed daemon
against the real inbox. Leave the separate Media session absent and make the
image adapter unavailable.

| Input | Required result |
| --- | --- |
| Operator plain text in DQR group | One instruction to DQR; no held draft, outbound group reply, repository write, or push. |
| Other participant's plain text | One held draft and a private operator notice; no instruction to DQR, group reply, repository write, or push. |
| Approval-shaped text from wrong sender or chat, wrong/expired draft ID, or a replay | No group send and no draft release. |
| Exact approval from the configured private operator chat | One matching draft released after target revalidation; replay sends nothing. |
| Draft drop from that private chat | Pending draft removed; nothing sent to the group. |

This is the independence proof: all core text cases must pass with no Media
agent and no image script.

## 2. Run a controlled text-only live smoke test

After the isolated contract passes, Zayed can send one unique benign text in
the existing DQR group, asking only for an acknowledgement inside the DQR
session. Confirm the marker reaches the DQR pane and the Git commit stays the
same. If the other participant agrees, a second benign text should produce
one held draft in Zayed's private approval chat and **no group reply**. Drop
that draft to clean up. Record timestamps for routing and drafting latency.
This step requires real people to send texts; the test runner does not send
them.

## 3. Test publication separately before any live photo

First add a side-effect-mocked regression for an image from the other group
participant. Require **zero public repo writes, commits, pushes, and sends**
until explicit publication approval. The current handler calls the image
script before the sender split, so this test is expected to fail now.

After the approval boundary is implemented, factor the DQR image adapter so
fixtures can inject a temporary repo, bare Git remote, fake chat, and fake
publisher. Test one approved image/commit, replay rejection, wrong sender or
approval channel, rejected MIME, over-limit size, failed EXIF stripping, and
changed target identity. No installed production script should run in that
fixture: their paths and remotes are hardcoded to live resources.

Do not use `imsg-chatbind --dry` or `--once` as an isolation mode; both can
process live state, and dry mode can still stage images. `dqr-media --no-push`
still writes into the production repository. `dqr-keeper --test` sends a real
notice. Keep live photos and live publish commands out of the test until the
approval regression passes.

## Acceptance criteria

- DQR text routing and held-reply approval pass without a Media session.
- Zero unapproved group replies or production pushes.
- Plain-text tests leave the DQR Git commit unchanged.
- One approved draft or publication acts once; replay has no further effect.
- The runtime and registry report DQR's real identity and working boundary;
  any mismatch is visible in the fleet map.
