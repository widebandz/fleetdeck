# DQR: the focused registry record

Select **DQR** in the [live fleet map](https://brainwave.tailacfa70.ts.net:18970/fleet-map).
Its overview has four fields. The [DQR test plan](DQR_TEST_PLAN.md) uses this
same boundary.

| Field | Source-backed meaning |
| --- | --- |
| DQR | A configured, live tmux session with an assigned identity card. A stable agent ID and current occupant receipt are not verified. |
| Change requester | The DQR group label names Imam El as the client contact. The other participant's handle has not been independently matched to that person; current observed DQR inbox records came from Zayed, the configured operator. Attribute each new request to its actual sender. |
| Bound chat | Local Messages chat ID **21**. The card, chat binding, and a read-only live Messages lookup agree on the ID and participant set. The map shows the local ID while hiding the GUID, phone numbers, and email handles. |
| Application stack | The repo's package manifest declares Next.js 16, React 19, TypeScript 5, Tailwind 4, and Supabase SDKs. Deeper layers show the DQR repository, its `haqzy` Git identity, and the linked Vercel project. Dependencies and a local project link do not prove an API call or a specific release. |

The focused record describes DQR, its application stack, its bound chat, and
its configured requester. No other agent is part of this record. The requester
is a declared contact badge, with no traffic wire because the handle-to-person
mapping is unverified. The two source-backed wires are **bound chat → DQR**
(`chat_routes_to`, configured operator instructions; external text held) and
**DQR → repository** (`uses_workspace`, a declared root rather than an
observed edit). The package stack sits inside the repository object. Select a
card or wire to inspect its evidence beside the map, then open a deeper layer
if needed.

## Current behavior that the record must not overstate

- Configured operator text can target the DQR session. Text from the other
  group participant currently enters a held-reply draft path; it is not
  evidence that DQR's live pane received or acted on a site-change request.
- The existing identity card still mentions image work, and chat binding can
  invoke a DQR-specific image script. That automation sits outside the
  requested DQR profile and remains a configuration drift to resolve. The
  current image script can push the live branch without prepublication
  approval, despite the card rule asking for it.
- The observed DQR pane is outside its declared repo root and has broad
  permissions. A card root and Git credential do not confine the process.
- `package.json` declares Next.js 16; older README/unit text says 14. The
  package manifest is the source for the displayed stack version.

The map is a read-only tailnet view. It exposes the bounded host-local chat
number requested for the profile, while keeping contact handles, credentials,
messages, and filesystem paths out of browser JSON.
