# Fleet chip architecture: a source-backed visual contract

Audited 2026-09-24 against the live map, the installed router/chat services,
the registry/runtime tools, and the Setup/Fleetdeck source. This describes the
current machine and a design for the map; it is not a claim that every proposed
event is already collected.

## What the software actually knows today

| Plane | Authoritative source | Present browser claim |
| --- | --- | --- |
| Physical/runtime structure | `tm-fleet-snapshot` reads tmux and configuration | `host → session → window → pane` containment; a pane command is a process classification, not agent identity. |
| Durable identity | `tm-fleet-registry` joins explicit agent IDs, bindings, and path claims | Registry revision 2 has zero agent bindings and zero path claims. The 40 live sessions are session chips, not verified agent chips. |
| Current occupant/work | `tm-fleet-occupant` receipt and `tm-fleet-lease` projection | Zero current receipts and leases. A configured root does not prove current occupancy, access, or editing. |
| Routing | Chat binding and the installed iMessage router | Four bound-chat ownership edges and computed target eligibility; neither is a delivered message. DQR's external text is held outside its pane. |
| Infrastructure | Fleetdeck service registry, listener scan, LaunchAgents, Tailscale Serve | Registered services and configured HTTPS proxy routes; a listener or proxy rule is not an API call. |
| Local request outcome | Request ledger and per-stage events | No working current local event trail. The installed router's `req` shim points at a missing ledger target, and it continues untracked when that call fails. |
| Website agent runtime | `.claude/agents` specs, `lib/agents/run.ts`, fixed tool grants, `agent_runs` | Real admin-initiated web runs and run-level status/tokens exist. They are a separate runtime with no verified ID join to the tmux fleet. |

The map's current read path is local snapshot → optional remote merge → registry
join → runtime projection → bounded infrastructure enrichment → privacy-filtered
`/api/fleet-map`. Remote collection is not configured. The live graph has 207
nodes and 205 semantic edges: 105 declared, 67 computed, and 33 observed. It
has no `agent` nodes or observed handoff events. The browser polls a snapshot
every 12 seconds; polling is not a record of work moving through a wire.

Jev is a synthetic shadow routing probe, not a live router connection. The
installed router currently uses local proposal logic and an operator
confirmation step for inferred targets. A Jev-to-agent pipe would be false in
the live view.

The wider Wideband software has more representations that must remain
distinct from local runtime until an ID joins them. The website's
`lib/docs/fleet.ts` describes the governed core team from agent spec files;
its Trace relationship is **governance**, not dispatch. The site's
`/admin/signal` reads `agent_runs` and has real-time run status for the website
fleet. The web runtime loads those agent specs and Constitution/context files,
uses fixed tool grants, and parks mutating tools for operator confirmation.
Those run rows use agent names and have no verified join to local tmux sessions
or the private fleet registry. The `/docs/agent-fleet` composition
pipeline is explicitly a roadmap. The existing OS chip in `lib/os-tiers.ts`
and `components/site/os-chip.tsx` is a selectable product/module illustration,
not a live topology schema. Its Machine/Brain/Surface bands, die geometry, and
pins are useful visual references; live chips and wires must still come from
the fleet snapshot and event sources. In particular, local `session:trace` has
a card that declares routing duties, but it has not been identified as the
website's governed `agent-trace` by a shared registry binding.

For web agents, the editor's `tools` frontmatter is a **declaration** while
`AGENT_TOOL_GRANTS` is the runner's executable allowlist. The chip must show
these as separate pins and flag a mismatch rather than treating the spec as an
enforced permission. Web context files are loaded by the runner; local tmux
instruction-file presence has no equivalent load proof.

The catalogs themselves differ: the website docs draw nine core agents,
`/admin/signal` hardcodes eight roster entries, and the repository has 17 agent
spec files. A unified board should show catalog provenance and a reconciliation
gap rather than silently merging those lists by similar names.

## Visual model

The board should resemble a circuit diagram because each visual element has a
specific software meaning:

1. **Host substrate:** a bounded area for one checked computer. A declared but
   unchecked host appears as an unverified outline, with no live process chips.
2. **Session shell:** the tmux session and its observed pane live inside the
   host. Show the assigned card role as a declaration. Label it `session` until
   the registry supplies an agent ID.
3. **Agent chip:** use this name only when a stable registry agent ID exists.
   Keep the durable binding and current occupant indicators separate; the
   latter needs a fresh matching launcher receipt.
4. **Ports:** bound chat, router input, tools, repository, outbox, service, and
   deployment are typed ports on or next to the relevant chip. A port appears
   only when a source names it.
5. **Routed wires:** draw a continuous, clickable SVG trace from a source port
   to its real target port, with an arrowhead at the target. Keep layout stable
   between refreshes. Every wire shows its type, what it carries or asserts,
   source, evidence state, and last check in the inspector.

Color answers **what kind of relationship** a wire represents: magenta for
chat/intake, blue for routing/control, cyan for runtime execution, violet for
tools and repositories, green for deployment/output, and amber for approval.
Line pattern answers **how well it is known**: solid for observed facts, dashed
for declarations, dotted for computed eligibility, and muted/dated for stale
facts. Containment is a nested chip or structural rail, never a traffic wire.
Unknowns get an explicit disconnected socket instead of a guessed edge.

The default fleet board should show only the selected session and its sourced
ports; broad service and router inventories remain behind layer toggles. A
click expands the same chip in place to expose identity, routing, runtime,
capabilities/instructions, state, network, and deployment. A second click on a
port or wire opens its evidence beside the canvas. The mobile view uses a
vertical route with the same ports and directions; it does not shrink a wide
desktop graph to illegible text.

Use the existing product's **Machine / Brain / Surface** grouping to orient
people: the machine holds runtime and state; the brain holds identity, rules,
tools, and memory; the surface holds chat, APIs, and outputs. Keep this
grouping separate from the site's agent tier colors and from wire-type colors.
The local fleet, website agent runs, and design-time agent catalog can be
toggleable planes; cross-plane wires require a verified shared ID or event
reference rather than a matching display name.

The current preview still has 11 linear tabs that rerender different card and
row layouts, so node positions do not persist through depth. Terminal detail
is its only older general SVG topology and limits related endpoint placement
to nine. A stable chip floorplan with orthogonal layer toggles is the intended
next UI architecture, not a capability of the current preview.

### DQR, using only the current sources

`Bound chat 21 → DQR session` is the declared `chat_routes_to` edge. Label its
payload `operator instructions; external text held outside DQR`. `DQR session
→ DQR repository` is the declared `uses_workspace` association. The package
manifest's Next.js/React/TypeScript/Tailwind/Supabase stack belongs inside that
repository chip, not at the endpoint of an invented application-traffic pipe.
Imam El is a **declared change contact badge** next to the chat: the handle to
person match is unverified, so there is no requester-to-chat traffic wire.
GitHub and Vercel are a deeper configured deployment branch, with release
outcome unobserved. No DQR-to-Media-agent connection exists.

### Trace, using only the current sources

Trace's observed session belongs inside the brainwave host. Its bound chat
ownership, declared outbox, outbox sender, and their typed relations can be
shown as separate wires. The router's candidate edges are dotted eligibility,
not dispatches. The configured Tailscale route ends at the local map service,
not at Trace or an inferred physical phone. The phone/handset remains unknown.

## Movement and efficiency require an event plane

Animate a **request token** only after a producer records a redacted event at
that exact boundary. Keep agent chips stationary unless registry binding and
fresh occupant receipts prove a relocation. Do not animate a configured wire,
computed candidate, polling refresh, or process label as traffic.

A minimal private event record should contain `event_id`, `correlation_id`,
`parent_event_id`, `occurred_at`, `kind`, `source_id`, `from_ref`, `to_ref`,
`actor_ref` (verified agent ID or explicitly unverified session), `outcome`,
and bounded timing metadata. It must omit message text, contact handles,
credentials, raw paths, and provider response bodies. Public map projection
may expose only redacted IDs, stage names, status, counts, and timing ranges.

Instrument the existing boundaries in order: chat intake/hold → router
proposal and confirmation → dispatch attempt and pane receipt → work start/end
→ managed tool/lease activity → push/deploy result → outbound reply. Restore a
versioned working request ledger first; the current `req` shim cannot provide
reliable outcomes. Join events by correlation ID and source generation, then
play one request through the existing wires with pause/step controls and a
timeline. A missing stage must appear as `unobserved`, not as a zero-duration
success.

The website's `agent_runs` may already support run duration, status, and token
views for web agents. Bring it in through a separate adapter and explicit
identity cross-reference. Do not infer that a web run happened in a local tmux
pane because the display names resemble each other.

Only then compute stage time, approval wait, queue time, retries, completion
rate, and cost when a provider supplies reliable usage. Show the sample count
and observation window next to every efficiency metric. Static topology alone
cannot measure efficiency.

## Implementation order

1. **Current preview:** replace detached overview borders/arrows with anchored
   SVG wires for actual edges. Keep DQR's requester as a badge and its stack
   inside the repository chip. This DQR slice is implemented and verified on
   desktop/phone; the same source-backed component should become the shared
   floorplan for other focused sessions.
2. **Identity:** adopt explicit stable IDs for chosen sessions through the
   existing registry draft/review flow, then use verified bindings and fresh
   launcher receipts before labeling a chip as a current agent. Record unknown
   identities rather than assigning them from tmux names.
3. **Events:** restore the request ledger and add the private event contract at
   local live boundaries. Add a separate, read-only adapter for website
   `agent_runs` with an explicit ID mapping. Build synthetic replay tests and
   a privacy-filtered event API before any moving token appears in the live
   map.
4. **Measured view:** add per-request playback and efficiency panels tied to
   actual correlated events. Keep configured, observed, and measured layers
   independently toggleable.

Relevant source contracts: [snapshot](../wb-setup-fleet-system/docs/FLEET-SNAPSHOT.md),
[registry](../wb-setup-fleet-system/docs/FLEET-REGISTRY.md),
[runtime](../wb-setup-fleet-system/docs/FLEET-RUNTIME.md),
[current map layers](FLEET_MAP_LAYERS.md), and
[Jev shadow boundary](JEV_SHADOW_E2E_PLAN.md). The website's distinct sources
are its [OS chip floorplan](../../flow-wideband-ai/lib/os-tiers.ts),
[web agent runner](../../flow-wideband-ai/lib/agents/run.ts),
[Signal projection](../../flow-wideband-ai/lib/signal.ts), and
[fleet docs model](../../flow-wideband-ai/lib/docs/fleet.ts).
