# DQR: a scoped agent as a worked example

For a text-only independence check and a separate publication-safety test,
see [DQR test plan](DQR_TEST_PLAN.md).

The fleet map uses DQR to show the difference between an agent's declared
scope, its configured routes, and what the running system actually enforces.
Select **DQR** in the branch index, then move through the tabs. Every named
pipe has a direction, payload, evidence class, source, and check time. Clicking
an object or pipe opens its detail beside the map (in a sheet on a phone).

| Layer | What DQR exposes | Boundary of the claim |
| --- | --- | --- |
| Overview | DQR session, host, bound chat, engagement label, operator, reply gate, and publication mismatch. | The logical chat channel is not a verified handset or delivered message. |
| Identity | Card duties, engagement label, configured operator, and repository Git user. | These are different roles. The engagement label is not verified legal ownership; there is no verified registry agent binding or current occupant receipt. |
| Routing | Bound group, configured participant count, held reply drafts, separate private approval chat, and the media input route. | Participant handles are not verified people; raw IDs and handles are hidden. The arrows show configured behavior, not individual deliveries. |
| Runtime | Live tmux hierarchy and pane working-root check. | The current pane is outside the declared repo root. A process label is not a confinement or identity proof. |
| Tools & skills | Card-listed DQR scripts. | Listing a tool does not prove every invocation or constrain other tools. Skills remain unverified. |
| Data & state | Declared DQR repository and sanitized image write target. | The repository path is hidden; a card root does not create a filesystem sandbox or an approved registry ownership claim. |
| Deployment | Media script → push wrapper → GitHub `main` → linked Vercel project. | The scripts and project link are checked; a particular production build or release is not observed. |

## The two pipelines

```text
External group text → draft held by chat handler
Private operator chat → exact approval command → matching draft released to group
```

The separate approval chat is configured for **external text replies**. The
chat handler rechecks the target before release. This is a configured gate;
the map does not inspect message contents or claim that any particular reply
was delivered.

```text
Group image → sanitizer → local repo asset → dqr-push → GitHub main
                                                    → linked Vercel project
```

The media path stages an image before checking whether the sender is the
operator or the other group participant. The media script calls the push
wrapper by default. The wrapper checks its repository and Git identity,
commits and pushes `main`, then notifies the operator. The project declares a
GitHub-to-Vercel deployment path. **There is no prepublication approval gate
on that path**, despite the identity card's instruction to ask before
publishing to the live site. The graph marks this as a mismatch.

`dqr-media` is a DQR-specific image script invoked by the chat binding. It is
not the separate **Media** tmux agent, and the live graph has no DQR → Media
session edge. Plain-text DQR routing does not depend on this image path. The
current DQR card still declares image work as a DQR responsibility; that
declaration should be revised if image intake becomes a separately owned
service.

## Accounts and identity

- **Operator:** Zayed is configured in the chat binding. The map shows the
  operator role, while hiding the operator's phone and email handles.
- **Engagement owner:** the card names the dailyquranreading.com / Imam El
  engagement. This is a card label, not proof of domain or legal ownership.
- **Group participants:** three configured handles: two operator aliases and
  one other handle. The exact mapping of that other handle to a person is not
  independently verified. The approval chat is separate.
- **Git account:** the repository and push wrapper select `haqzy` for DQR.
  The machine's global GitHub CLI is signed in as `widebandz`; these must not
  be conflated. The map does not query or expose the credential.
- **Agent identity:** the tmux session and identity card exist, but the fleet
  registry has no verified DQR agent binding or current occupant receipt.

## Scope gaps to address before calling DQR tightly confined

1. Add an explicit approval gate before the image pipeline can push the live
   branch. Keep the reply and publication approvals as distinct controls.
2. Launch the runtime in its declared repo and enforce repository access at
   the process or OS boundary. The observed pane currently runs from `/` with
   broad permissions, so the repo-local hook is not a confinement boundary.
3. Bind and attest DQR's stable agent identity in the registry. Decide whether
   the operator-only view should reveal exact chat and account identifiers
   behind authentication. The shared tailnet map keeps them redacted.
4. Verify the actual Vercel deployment and any downstream API privileges
   separately. A local project link and script comments do not prove a
   specific release or remote permission.

The browser snapshot is a bounded, read-only projection of the card, chat
binding, inspected scripts, Git configuration, tmux metadata, and Vercel
project link. It does not include raw chat IDs, phone numbers, email
addresses, credentials, message text, or filesystem paths.
