# Trace: a tour of the live fleet map

Open the [live fleet map](https://brainwave.tailacfa70.ts.net:18970/fleet-map)
while connected to the tailnet. The map is a read-only, redacted snapshot of
the fleet. Selecting a box or arrow explains it in place; the layer tabs
change how deep you look without losing the selected object. On a phone, the
details open in a compact sheet.

The tabs run from **Overview → Identity → Routing → Runtime → Tools & skills →
Memory & MD → Services & APIs → Network → Data & state → Deployment →
Terminal detail**. Use **Simpler** or **Deeper** to move one layer at a time.

## 1. Start with the overview

Check the **Live snapshot** indicator and collection time. A fresh timestamp
means the sources were recently read; it does not mean every message or tool
call was observed. Use **Refresh** if you are unsure. If the map says partial
or last known, read the source notice before interpreting a line as current.

Find **Trace**. Read the two top relationships in their arrow directions:

```text
Bound chat channel ──configured inbound owner──▶ Trace session
Trace session ──runs on──▶ brainwave Mac/server
```

The bound chat is Trace's logical phone-side communication channel. The map
does not identify the physical handset or prove a particular message arrived.
The separate **Tailscale map-view route** carries HTTPS page/API requests to
the server and responses back to a viewer. It is not the iMessage channel,
and the map does not know whether the same phone used both.

Click either arrow to see its source, time, and exact meaning. Click Trace to
open **Identity**, the computer/server to open **Runtime**, or the chat
channel to open **Routing**. A line labeled “runs on” is execution placement,
not a message pipe. A configured chat arrow means ownership in the binding
file, not a delivered text.

## 2. Click Trace: identity and responsibilities

Open the **Identity** layer with Trace selected. `trace` is an observed tmux
session with an **assigned** identity card. Its card describes three duties:
interpret and route the operator's request, carry the result back to the
bound chat, and write a trail to the outbox. Those duties are **declared card
text**; they are not proof that a particular request was completed.

Look for **verified agent ID: unknown**. The registry currently has no Trace
binding, so the stable session name and the running process must not be
presented as a verified agent identity. The current pane occupant is also
unknown until a valid runtime receipt exists.

## 3. Follow routing, one pipe at a time

Switch to **Routing**. The bound chat → Trace pipe is a declared inbound owner.
The router also describes Trace and, at snapshot time, computed that the
session was addressable and its active pane looked ready for an agent prompt.
These are routing eligibility checks, not evidence that instructions landed.
The running router's loaded policy and actual delivery are not verified by
this map.

Now follow the source-backed **outbound configuration**. Trace → Trace outbox
means its card declares a queued text-file write target. Outbox sender →
Trace outbox means that job is configured to *consume* files from the queue;
this arrow names the consumer and its target, not the direction bytes move.
Outbox sender → bound chat means its script and card target that channel for
queued text. The map observes no individual file write, queue read, or
successful message send. A separate chat-binding job → bound chat edge shows
the configured inbound handler. Trace's card describes routing work to an
owning session, but the browser has no specific Trace → target-session
handoff pipe. Do not read a connection to every session into the router's
eligibility list.

## 4. Open the runtime branch

Switch to **Runtime** and expand Trace. The observed structure is
**brainwave → Trace session → two windows → two panes**. At this audit, one
pane was classified as Claude Code and the other as Codex. Pane process
classifications can change and do not establish a durable Trace agent ID.
Click a pane to see the classification basis and observation time.

## 5. Inspect tools, skills, and instructions

In **Tools & skills**, Trace has one exact declared tool link: `imsg-chatbind`. A
listed tool is a capability reference, not an observed invocation or a full
inventory of everything the process can call. Trace's per-session skill
inventory is **unknown** in this map. Local configuration names an MCP server,
but the map cannot verify its availability or use.

In **Memory & MD**, Trace has separate `AGENTS.md` and `CLAUDE.md` nodes and
arrows from its session to both. They prove the files exist in the declared
session root. Their contents are not sent to the browser, and the map cannot
prove the current process loaded them. The identity card also exists; its
short responsibility summary is marked declared.

## 6. Follow services, network, data, and deployment

The **Services & APIs** layer now contains registered services on brainwave,
including the local fleet-map preview. A host → service declaration says the
service is registered; a separate observed edge says a local port has a
listener. At this audit, 35 services were registered and 32 had listeners;
the preview makes a 36th service node with its own observed listener. These
facts do **not** say Trace called any API or prove which process owns a
listener. Open **Show more** to see beyond the first rows.

The map itself reads local tmux/config/cards → optional remote metadata →
read-only registry join → runtime receipt check → bounded infrastructure
projection → redacted map API. Its preview listener serves health, map HTML,
and map JSON GET routes; it cannot write to tmux, chat, or the registry.

The **Network** layer shows symbolic tailnet HTTPS endpoints and the local
services they are configured to proxy to. The **Tailnet HTTPS map link → Fleet
map preview** relation is the one for this page: it carries *configured HTTPS
request forwarding*, with no individual request observed. It appears first;
use **Show more** to inspect other routes. A second Mac is declared in device
configuration but has not been remotely checked, so its reachability is
unknown. The link and any phone peers do not identify a map viewer or Trace's
physical phone.

In **Data & state**, open the symbolic **Trace outbox** queue and its declared
writer/consumer relations. The queued messages and file names are hidden.
Trace's identity-card workspace root agrees with the session standard, and
the observed working directory was within both at collection time. The path
stays private. There is **no approved workspace path claim or active managed
work lease** for Trace in the current graph. A configured root is context,
not ownership of the directory. Trace's work status is not declared here.

The **Deployment** layer shows four LaunchAgent jobs: the chat-binding
service, map preview, Trace session keeper, and Trace outbox sender. The
first two showed a running process at this audit; the keeper and sender were
registered without a current PID. Job → Trace or job → map arrows name
configured launch targets, not a recorded execution. The preview runs on
loopback, and Tailscale Serve exposes it only to the tailnet. This preview
has not become a released Setup app or a public website.

Use **Terminal detail** when you want the exact Trace window and pane branch.
The **Simpler** and **Deeper** buttons step through the same layers; the tabs
let you jump directly. Selecting a node or pipe opens a nearby inspector on
desktop or a dismissible sheet on mobile, so the diagram stays in view.

## Read the evidence labels

| Label | What it supports | What it does not support |
| --- | --- | --- |
| **Observed** | The collector saw tmux structure, a process classification, or a service port listener at the shown time. | A stable agent identity, API call, process owner, or future state. |
| **Declared** | A card, binding, LaunchAgent, Serve mapping, or other configuration says the relationship exists. | Execution, message delivery, HTTP traffic, or tool use. |
| **Computed** | A rule matched current tmux metadata and on-disk policy at collection time. | The running router's loaded policy or delivered keystrokes. |
| **Last known** | A previous fact is retained with its earlier timestamp after a source failed. | A current observation. |
| **Unknown** | The map has no sufficient evidence or the source was unavailable. | Proof that the thing is absent. |

For every actual pipe, read **from → to**, then its payload or relationship,
evidence label, source, and timestamp. The most important unknowns for Trace
today are physical phone identity, message delivery, verified agent/occupant
identity, per-session skills, and approved workspace ownership.
