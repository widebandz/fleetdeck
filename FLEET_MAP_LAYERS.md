# Fleet map: live infrastructure layers

Audited 2026-09-24 against the live, redacted `/api/fleet-map` response. The
snapshot held **205 nodes and 203 semantic edges**. Counts and listener status
can change; the evidence labels on each object matter more than a fixed count.

## Progressive view

The overview puts Trace between its two evidenced sides:

```text
brainwave Mac/server ◀── runs on ── Trace session ◀── configured owner ── bound chat channel
```

The Mac placement is observed tmux structure. The chat is declared ownership
of a logical phone-side channel; physical handset, owner, message delivery,
and phone network path are unknown. The separate **tailnet map link → local
map service** is a configured HTTPS proxy route for viewing this page, not an
iMessage route or an observed visit. The map does not assert one phone used
both channels.

Clicking Trace, the computer, or the chat channel opens Identity, Runtime, or
Routing respectively. A tab selects one depth while keeping the focused
session; **Simpler** and **Deeper** step through the same tabs. A node or pipe
opens source, payload, evidence, and time in a right inspector on desktop or
a dismissible sheet on mobile. Service, network, and deployment layers show a
short initial set with a reveal control.

| Tab, in UI order | Evidence shown now | Limit |
| --- | --- | --- |
| Overview | Selected session, observed host placement, declared bound chat. | No physical phone or delivered-message claim. |
| Identity | Card role and three safely projected Trace responsibility bullets. | No verified registry agent or current occupant. |
| Routing | Bound-chat daemon and owner, router metadata, declared outbox sender route. | A configured or computed path is not delivery. |
| Runtime | Host → session → window → pane and process classification. | Process label is not stable agent identity. |
| Tools & skills | Card-listed tools and scripts. | Use unobserved; per-Trace skills not inventoried. |
| Memory & MD | Trace's present `AGENTS.md` and `CLAUDE.md`, card and standard metadata. | Contents and current process loading unobserved. |
| Services & APIs | Registered services and observed local port listeners, including map preview. | Listener does not prove Trace called an API or identify its process. |
| Network | Symbolic Serve endpoints and configured proxy targets; local/unchecked hosts. | Requests, phone identity, remote SSH reachability unobserved. |
| Data & state | Symbolic Trace outbox, declared writer/consumer relations, root checks. | No queued content, approved path claim, or current lease. |
| Deployment | Four local LaunchAgent declarations and configured targets. | Registration and configuration do not prove each run or release. |
| Terminal detail | Full branch and named connections. | Same evidence limits as focused tabs. |

Only show populated elements as nodes and pipes. When a layer has no supported
objects, keep the focused object visible and show a compact **not recorded**
or **unknown** explanation. Avoid filling the canvas with speculative boxes.
Within each layer, one click opens a nearby detail card on desktop or a
bottom sheet on mobile; it should not scroll the page to an inspector. Keep
the breadcrumb, selected object, and layer switches in view. A pipe detail
shows **from, to, what travels or what the relationship asserts, evidence,
source, and time**. Switching layers preserves the selected object when that
object exists there. A cross-layer chip can jump to its runtime or route.

Use solid lines for observed facts, dashed for declarations, dotted for
computed eligibility, and amber with the original timestamp for retained
last-known facts. Containment is structure, not message traffic. Some arrows
describe a configured actor → target relation rather than byte direction:
`outbox sender → outbox` means the sender consumes that queue. Repeated router
checks to one target do not prove multiple deliveries.

## Existing graph inventory and placement

| Current node | Count | Layer, meaning, and limit |
| --- | ---: | --- |
| `host` | 2 | Compute/network. Brainwave local and observed; one declared Mac unchecked remotely. |
| `session` | 40 | Identity/routing/runtime. Stable tmux name, separate from agent ID. |
| `window`, `pane` | 41 each | Runtime. Process classification is observation, not occupant proof. |
| `chat` | 5 | Routing. Four bound chats and a router command channel. |
| `tool`, `file` | 5 each | Capabilities. Exact card references; invocation unobserved. |
| `instruction` | 2 | Memory & MD. Trace instruction-file presence; content/load unobserved. |
| `service` | 36 | Services & APIs. Thirty-five registrations plus local map preview. |
| `endpoint` | 23 | Network. Symbolic tailnet HTTPS proxy endpoints, including map link. |
| `job` | 4 | Deployment. Chat binding, map preview, Trace keeper, Trace outbox. |
| `data` | 1 | Data & state. Symbolic Trace outbox queue; no content. |

The schema also permits `agent`, `skill`, `workspace`, and `channel` nodes;
none exist in this live graph. The registry has zero planned or verified agent
bindings, approved path claims, current occupants, or active work leases.

| Current edge | Count | From → to; payload or assertion | Evidence |
| --- | ---: | --- | --- |
| `parent_id` containment | structural | Host → session → window → pane; execution structure, not traffic. | Observed where live. |
| `chat_routes_to` | 4 | Bound chat → session; configured inbound owner. | Declared; delivery unobserved. |
| `handles_bound_chat` | 1 | Chat binding job → Trace bound chat; incoming events assigned to daemon. | Declared; delivery unobserved. |
| `describes_session` | 17 | Router channel → session; target metadata, no traffic. | Declared. |
| `router_addressable` | 39 | Router channel → session; candidate instruction eligibility. | Computed from on-disk policy. |
| `router_agent_pane_ready` | 28 | Router channel → session; candidate active-pane readiness. | Computed; running policy/delivery unverified. |
| `hands_off_to` | 1 | Media session → GHL session; handoff intent, content unspecified. | Declared. |
| `sends_chat` | 1 | Trace outbox sender job → bound chat; queued text messages. | Declared; send unobserved. |
| `uses_tool` | 5 | Session → tool; listed capability, invocation unobserved. | Declared. |
| `executes_file` | 5 | Session → script; listed script, execution unobserved. | Declared; UI should say “listed script.” |
| `has_instruction_file` | 2 | Trace session → MD file; instruction file available in declared root. | Declared/present; load unobserved. |
| `declares_service` | 35 | Host → service; service registration metadata. | Declared; listener checked separately. |
| `runs_service` | 33 | Host → service; local TCP listener at registered port. | Observed listener; process identity unverified. |
| `proxy_routes_to` | 24 | Tailnet endpoint → local service; HTTPS request forwarding. | Declared; traffic unobserved. |
| `writes_queue` | 1 | Trace session → outbox; queued text-file write target. | Declared; individual writes unobserved. |
| `consumes_queue` | 1 | Outbox sender job → outbox; queued text-file read target. | Declared; individual reads/sends unobserved. |
| `schedules_job` | 4 | Host → LaunchAgent job; job registration. | Declared; load status on job. |
| `launches` | 2 | Keeper job → Trace session; map job → preview service. | Declared launch targets; execution separate. |

The map HTTPS route is `endpoint:tailnet-map → service:fleet-map-local` with
payload **HTTPS requests**. Reverse HTTP responses are implied by the
protocol but are not observed graph edges. No physical phone viewer or
Trace → arbitrary API call follows from these facts.

## Trace, as a worked example

- `session:trace` is declared and observed on the local host. Its assigned
  card has three safely projected, **declared** duties: interpret and route
  requests, carry answers back to the bound chat, and write an outbox trail.
  There is no verified agent ID or attested current occupant.
- Trace has two observed tmux windows and panes, classified as Claude Code
  and Codex. Neither classification establishes a durable agent identity.
- The configured inbound chain is **chat-binding job → bound chat → Trace**.
  The configured outbound relations are **Trace → outbox**,
  **outbox-sender job → outbox** (reads queue), and **outbox-sender job →
  bound chat**. The outbox is symbolic; no edge records an individual message
  or successful send.
- Trace lists `imsg-chatbind` as a tool. Its `AGENTS.md` and `CLAUDE.md` nodes
  prove file presence in the declared root, not that a current pane read
  them. The card and session-standard roots agree and the observed cwd lies
  within them. The path is redacted; no approved ownership claim follows.
- Local configuration names an MCP server, but the map has no MCP or skill
  association and cannot assert availability or use. Neither the bound chat
  nor the tailnet map endpoint identifies a physical phone or current viewer.

## Read pipeline and remaining gaps

The preview reads local tmux/config/cards → optional remote SSH merge →
read-only registry join → runtime receipt/lease projection → bounded
infrastructure projection → strict browser allowlist → `/api/fleet-map`.
Remote SSH collection is not configured. Infrastructure sources include
service registrations, listeners, Trace workspace/card, LaunchAgents and
load status, and Tailscale Serve configuration. The local preview remains a
loopback LaunchAgent service fronted by tailnet-only HTTPS; it is not a public
site or released Setup build. The browser receives no raw paths, addresses,
ports, chat IDs, pane text, message bodies, instruction contents, or outbox
contents.

Remaining unknowns: physical phone identity/ownership/viewing, individual
chat or HTTP delivery, verified Trace agent/occupant, actual tool/skill/MCP
use, approved workspace claim and current lease, remote Mac reachability,
and any released deployment connection for this preview.
