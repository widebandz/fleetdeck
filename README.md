# fleetdeck — `1.3.0`

**Your Mac's servers and launchd agents, as one launcher board on your phone.**
Plus the tmux fleet as a chat, and a direct line to the fleet steward. Tailnet-only,
stdlib Python, no build step.

Two browser surfaces for one Mac, reachable from a phone over Tailscale:

| surface | port | what it is |
|---|---|---|
| **portal** | 8790 | One tile per local **server** and per **launchd agent** — the board (`/board`) or the six-key **simple screen** (`/phone`), whichever this device chose. Installs to the home screen as a PWA. |
| **chat** | 8783 | The whole **tmux fleet as a Messages-style thread list**. Tap a session, get a live writable terminal. |
| **skin** | per app | Optional. Fronts an app you *cannot* edit — a container — so it installs with your icon and palette. Idle until you configure one. |
| **adopt** | 8793 | Paste a repo URL, read the plan, press Adopt. Loopback only by default — it installs software. |
| **fleet map** | opt-in, own port | A read-only, metadata-only picture of the tmux fleet. Off unless `FLEETDECK_FLEET_MAP=1`, and meant to run as its own listener. |

New in 1.3.0, both halves of what this machine actually serves:

- **The phone surface behaves like one installed app.** Every key stays inside
  the standalone shell instead of handing the phone to a Custom Tab —
  [One installed app](#one-installed-app).
- **Two new surfaces of its own**, Notes and Cashflow, served from this origin
  for that same reason.
- **A chat input dock** — the keys a software keyboard does not have, routed
  through tmux rather than faked into the terminal.
- **The fleet map** — [The fleet map](#the-fleet-map).
- **CALL opens the native Messages thread.** It used to open a voice surface on
  the cockpit; Trace is talked to over iMessage, so a second inbox meant
  reading half a conversation in each. The handle is the one the Mac sends
  *from*.

1.2.0 brought the simple screen (`/phone`) and the per-device home toggle —
[Two front doors](#two-front-doors).

Plus one internal: **:8784**, a loopback-only `ttyd` that is the chat's terminal
backend. It is never reachable directly — `chat_server.py` proxies it under `/t`.

Stdlib Python only. No pip, no node, no build step.

```bash
./install.sh          # reconcile this machine with this repo (idempotent)
fleetdeck doctor      # full inventory: services, agents, drift, binding, urls
```

---

## Install

**1. Put the repo somewhere launchd can read.**

```bash
git clone <this-repo> ~/.config/fleetdeck && cd ~/.config/fleetdeck
```

> `install.sh` **refuses to run** from `~/Documents`, `~/Desktop` or
> `~/Downloads`. Those are TCC-protected: launchd cannot read them without a
> Full Disk Access grant, and the jobs fail in a way that looks like a bug in
> this tool. `~/.config` or `~/srv` is safe.

**2. Dependencies.**

```bash
brew install tmux ttyd
```

Tailscale.app is required for remote reach. Without it both surfaces still run,
but only on loopback.

**3. Edit `config.json`** — set `brand` and `label_prefix` at minimum:

```json
{
  "brand": "ACME",
  "machine": "",
  "label_prefix": "com.acme",
  "ports": { "portal": 8790, "chat": 8783, "ttyd": 8784 }
}
```

Leave `machine` empty. It is resolved from Tailscale at boot — a hardcoded
tailnet name is the single thing most likely to be wrong on someone else's box.

**4. Run it.**

```bash
./install.sh
```

It renders the plists with this repo's path, this machine's `$HOME`, your
`python3`, and your prefix; installs `~/bin/fleetdeck`; bootstraps both agents;
sets up the `tailscale serve` front; and ends by running `doctor`.

**5. Curate the board.** Open the portal. Everything listening that isn't in
`services.json` appears under **unregistered** at the bottom with its process
name and port. Promote the ones that matter:

```json
{ "id": "api", "group": "apps", "port": 3000, "icon": "server",
  "name": "API", "blurb": "staging api" }
```

Hot-reloaded — no restart. `services.json` ships nearly empty on purpose: a wall
of offline tiles for apps this machine doesn't run is worse than an honest
discovery list.

---

## The honesty rule

**A tile never shows a link the portal has not resolved from live system state.**
Every scan reads three sources fresh:

| source | answers |
|---|---|
| `lsof -nP -iTCP -sTCP:LISTEN` | is it listening, and what did it bind? |
| `tailscale serve status --json` | is a loopback service proxied to a real URL? |
| `launchctl list` + `~/Library/LaunchAgents/*.plist` | what is scheduled, and how did it last exit? |

Nothing about a URL is hardcoded. Move a service to a new port, add a serve
mapping, kill a container — the next scan tells the truth without an edit.

A service bound to `127.0.0.1` with no serve mapping gets a **`host only`**
badge, not a dead link. A service that answers HTTP but has no browser UI is
marked **`api`** and is deliberately **not clickable**, so a tap can never land
on a 404.

---

## Two front doors

The board shows everything on the machine, which is what a board is for.
`/phone` is the opposite surface: a clock and six keys sized for a thumb, for
the times you already know where you are going.

| path | always renders |
|---|---|
| `/board` | the board |
| `/phone` | the simple screen |
| `/` | whichever one this device chose |

The **⠿ button in the board header** switches between them, and the
**`set as home`** link in the simple screen's footer does the same from the
other side. Either way it writes one cookie, `fd_home`, and lands you on the
surface you picked.

The choice is per **device**, not per machine — the board belongs on the desk
where there is room for forty tiles, the simple screen belongs on the phone, and
a setting in `config.json` would force one answer onto both. It is a cookie for
the same reason it is a redirect and not a script: the simple screen is
server-rendered and carries almost no JS, and a preference that needs JS to
stick is a preference that fails on the surface most likely to be opened when
something is already wrong.

`/` is the installed tile's `start_url`, so this is what "change to the simple
UI" actually changes. The canonical paths never follow the cookie — that is
what keeps a route back from a phone that has chosen the simple screen. Any
value that is not `simple` — absent, junk, or invented by a newer build — means
the board, because the board is the surface that can reach everything.

Which six keys appear is one line, `PHONE_APPS`, resolved against the same
registry the board uses. A registered service that is down still gets its key,
dimmed and labelled: hiding it would make a dead service and an unregistered one
look identical from the one screen most likely to be opened when something is
wrong.

---

## One installed app

An origin is scheme **plus host plus port**, so every service on this machine is
cross-origin to the portal. Installed to a home screen, that is not a detail:
Android answers a cross-origin tap by dropping the app out of standalone mode
into a Custom Tab with an address bar, and the surface you installed is gone
until you come back and re-open it.

`/app/<id>` keeps the top-level document on this origin and frames the service
inside it. No proxying and no path rewriting — a frame resolves its own URLs
against its own origin, so the service never learns it is framed.

```
/app/chat  /app/messages  /app/cockpit  /app/graph  /app/terminal  /app/pm
/app/netmap
```

The set is **closed and keyed by registry id** (`SHELLED_APPS`), so nothing in
a request names a URL. Permissions a framed document cannot inherit are granted
by name and per app: `cockpit` gets `microphone` because that is where Trace
listens, and it is the only one that does.

All seven were loaded in a real cross-origin frame before being added, including
the one most likely to fail — `chat` frames its own `ttyd` terminals, so it is a
frame inside a frame with a live websocket at the bottom.

Two surfaces here are this server's own, for the same reason:

| path | what it is |
|---|---|
| `/notes` | Capture for voice-note transcripts and half-formed ideas. Four fields, a closed status set, and the original text kept verbatim on every path in. One validating door, written for a caller that may be wrong. |
| `/cashflow` | The accountant session's cash view, read from `~/finance-ops/cashflow.html` **per request** so it is never a copy taken at boot. The Fleetdeck bar is injected at serve time and never written back into the agent's file. |

Also on the simple screen: the moving Wideband mark under the clock
(`assets/wb-logo-256.mp4`, re-encoded to 235KB rather than the 6MB master), and
cross-document **view transitions** — three declarations of CSS, no JavaScript,
and inert where unsupported.

---

## The fleet map

A read-only picture of the tmux fleet: sessions, panes, what each one declares
versus what is actually observed, and the links between them.

**It is off by default** and gated on `FLEETDECK_FLEET_MAP=1`, because the
portal is visible to every permitted tailnet peer and this page reveals private
session topology even though it only reads. It is meant to run as a **separate
listener on its own port** — in that mode the process refuses every inherited
portal route, including the ones that can read pane and chat content, and
accepts exactly one POST: `/api/fleet-explain`, which answers questions about
the snapshot already on screen.

Setup, environment variables and the collector contract are in
[`FLEET_MAP_PREVIEW.md`](FLEET_MAP_PREVIEW.md); the layer model is in
[`FLEET_MAP_LAYERS.md`](FLEET_MAP_LAYERS.md).

`frame-ancestors` names exactly one origin — this portal, on this machine,
behind the same tailnet gate. It was `'none'`, which correctly refuses *every*
framer including this server's own, and that is why the map was the last surface
breaking the installed app out of fullscreen. Everything else in that header is
untouched: `default-src 'none'`, `connect-src 'self'`, `base-uri 'none'`,
`form-action 'none'`.

---

## Agents

This is the half a port scan cannot see. Most of an operator's fleet is
scheduled work — nudges, watchdogs, backups, digests — and none of it listens on
a socket, so `lsof` reports it as simply absent.

Reading **both** the plist (installed) and `launchctl list` (loaded) is what
makes `unloaded` visible, which is the most common way a launchd job is quietly
doing nothing at all.

| state | lamp | meaning |
|---|---|---|
| `run` | green | has a PID right now |
| `ok` | slate | loaded, last exit clean — **a resting periodic job is healthy** |
| `off` | amber | plist on disk, never bootstrapped → **doing nothing** |
| `fail` | red | last run exited non-zero |

A negative exit status is a **signal** (`-15` = SIGTERM on reload), not a
failure, and is folded into `ok`. Getting that wrong paints a healthy board red,
which is the fastest way to make an operator stop reading it.

Filtering, in `config.json`:

```json
"agents": {
  "show": true,
  "actions": false,
  "include": ["com.acme."],
  "exclude": []
}
```

`include` empty = every non-vendor agent (Apple, Google, Adobe, Homebrew, Docker
and friends are filtered out — they're the OS, not the fleet).

### `actions` — read this before turning it on

`actions: true` adds **run/stop** buttons to every agent tile. These surfaces
have **no password** — the tailnet bind is the only boundary. Read-only, that's
a defensible posture. Start/stop is a much bigger claim, so it is **off by
default**.

The `POST /api/agent` route additionally refuses any label the scan did not
discover, so a caller cannot name an arbitrary launchd job even with actions on.

---

## Home-screen icons

The portal installs to an iPhone home screen with a real icon. Left alone,
nothing else does — `apple-mobile-web-app-capable` buys standalone mode, not an
icon, so Add to Home Screen falls back to a screenshot of the page and a row of
your services comes out as a row of grey rectangles.

`make-icons.py` fixes that. It rasterises the **same glyphs the board draws**,
from `glyphs.json`, so a tile and its installed icon cannot drift apart:

```bash
fleetdeck icons            # every service with an icon
fleetdeck icons chat pm    # just these
```

Output lands in `icons/` and is served at `/icons/<id>-<size>.png`. Set
`icon_dest` on a service and the PNGs are copied into that app's own static
directory too — better, because then the icon does not depend on the portal
being up or the tailnet keeping its name:

```json
{ "id": "myapp", "port": 4000, "icon": "rocket",
  "icon_dest": "~/my-app/public/icons", "install": true }
```

Then link it from that app's `<head>` and it installs cleanly:

```html
<link rel="apple-touch-icon" href="/icons/myapp-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="My App">
```

`"install": true` lists it in the board's **Add to Home Screen** sheet — the
button next to fullscreen. That sheet is a checklist, not an installer:
`beforeinstallprompt` does not exist on iOS and Safari only offers Add to Home
Screen from the Share menu, so it opens each surface in turn and remembers
which ones you have done. Set the flag only once the app really serves that
icon, or the sheet promises a tile that still comes out as a screenshot.

Icons are PNG on purpose — iOS ignores SVG for `apple-touch-icon`. The board
itself renders the glyph as inline SVG; both come from the one glyph.

Adding a glyph is a line in `glyphs.json`: draw on a 24×24 grid, stroke only,
no fill. The renderer measures each path with `getBBox()` and scales it so its
longest side fills the same fraction of every canvas, recomputing stroke-width
per glyph — otherwise a flat glyph looks shrunken beside a round one.

**Every glyph ships pre-rendered** in `assets/glyphs/`, so a fresh clone has
the whole icon set on arrival. With no browser installed, `fleetdeck icons`
copies those instead of rasterising — you still get real icons, they are
byte-identical to a live render, and only a *custom* glyph or a palette change
needs a browser after that.

With one, it rasterises live: headless Chromium (`FLEETDECK_CHROME` if yours
lives somewhere unusual) plus macOS `sips` for the downscales. Nothing else.
Changed a glyph, or `BG`/`INK`/`STROKE`/`FILL`? Rebuild the shipped set with
`./make-icons.py --library` and commit it.

## Skinning an app you don't own

Half a real board is third-party apps in containers. They will never ship an
icon that matches your fleet, and an image tagged `:latest` throws away
anything you write into it on the next pull.

So dress them from outside. Give a service a `skin` block:

```json
{ "id": "tts", "port": 8880, "install": true,
  "skin": { "name": "Script", "port": 8871, "css": "script.css",
            "replace": { "TheirBrand": "Script" },
            "recolor": { "99, 102, 241": "79, 227, 193" } } }
```

`skin_server.py` binds `skin.port`, proxies the app **verbatim**, and rewrites
only its `<head>`: title, apple-touch-icon, manifest, theme-color, and a
stylesheet from `skins/` injected last so it wins specificity ties. Add a
`tailscale serve` mapping for that port and the board resolves the tile to the
front automatically — falling back to the bare app if the front is down, so a
dead proxy degrades to a working link rather than a broken one.

`replace` and `recolor` are literal substitutions, on HTML and CSS
respectively. They exist because a stylesheet cannot reach a name an app paints
into its own markup, nor a colour it hardcoded past its own variables. Prefer
them to selector-specific rules: a colour map survives a redesign that moves
every element, and a rule that assumes where a button sits does not. Start from
`skins/example.css`.

One side effect worth knowing: the front rewrites `Host` to `127.0.0.1`. Some
dev servers — Vite among them — reject an unrecognised host and return 403 from
behind `tailscale serve` even though the port is fine. Proxying through the
skin fixes that without binding the app to `0.0.0.0` or patching a file you do
not own.

The agent idles harmlessly when nothing is skinned, and picks up a new `skin`
block within a few seconds. No restart.

---

## Adopting a repo

Standing up an app by hand is a dozen steps and four traps, and the steps are
the same every time. `adopt` does the mechanical part:

```bash
fleetdeck adopt https://github.com/owner/thing        # prints the plan, runs nothing
fleetdeck adopt https://github.com/owner/thing --yes  # and installs it
```

…or paste the URL into the **adopt** surface and press a button.

**Two phases, and the split is the point.** `inspect` clones to a scratch
directory and only *reads*: the stack, the entry command, the port the project
declares, the environment variables with no value in its example file, whether
it has install lifecycle scripts, its licence, its remote — plus a port this
machine can actually give it, checked against both the registry and everything
currently listening.

`adopt` is the half that executes, and only after you have seen that report.
This matters more than it looks: `npm install` runs lifecycle scripts from
every transitive dependency, and these surfaces have **no password**. A
one-click installer that skipped the report would be a remote-code-execution
button for anything that can reach the board.

Then it does the rest of the runbook: move into `~/srv/<name>`, install deps,
write `.env`, render and bootstrap a launchd agent, **check the exit status**,
add a `tailscale serve` mapping, register a tile, generate the icon.

It refuses to install into `~/Documents`, `~/Desktop` or `~/Downloads` (TCC —
launchd cannot read them), to overwrite an existing directory, or to take a
port something already holds. It never pushes.

**Docker repos are detected, not adopted.** A container is daemon-managed with
its own restart policy, not a launchd job, and pretending otherwise would
produce a tile that lies about what runs it. A repo that ships a Dockerfile
*next to* a normal start script is adopted natively, and says so.

> **The adopt agent binds loopback and `install.sh` does not give it a
> `tailscale serve` front.** Every other surface here reads state; this one
> installs software. Reach it with an SSH tunnel, or add the mapping yourself
> once you have decided every tailnet device should be able to install things
> on this Mac.

---

## Security posture

### Installer-backed customer phone

When `config.json` contains `onboarding` with `os_name` and `agent_name`,
`/phone` shows Agent, First project, a built-in starter graph, a read-only
terminal viewer, and Notes β. If `onboarding` is absent, the portal reads
only `os_name`, `agent_name`, and `first_goal` from the current user's
`~/.wideband/setup/state.json` metadata. That file must be private to the
user. An explicit config entry takes precedence. Example:

```json
"onboarding": {
  "os_name": "Brainwave",
  "agent_name": "Trace",
  "first_goal": "website",
  "first_project_url": "https://your-project.example",
  "head_session": "wb-head"
}
```

`first_goal` is `research`, `website`, or `proposal`. The project key
uses a live, reachable `first-project` registry entry first, then a verified
HTTPS `phone_url` from `~/.wideband/first-goal/status.json` when its status
is `ready`, then an explicit HTTPS config URL. A loopback `local_url`
never becomes a phone link. `/graph`
shows the setup map even before the full corpus graph service is installed.
`/watch` captures only the configured tmux session; it has no keyboard,
WebSocket, or command route. `head_session` defaults to `wb-head`.

In this mode, `./install.sh` installs only the portal, skips ttyd, and unloads
and disables older Fleetdeck chat/adopt/skin jobs. `chat_server.py` also
refuses to start.
The customer phone does not link to writable services. Portal operator pages
and non-Notes POST routes are disabled unless `FLEETDECK_CONTROL_TOKEN` is set
to at least 16 characters; then HTTP Basic auth unlocks them. This portal gate
does not govern a separately installed ttyd or other service exposed on its
own port. A customer installer must leave those off.

Notes uses `~/.fleetdeck-notes.json` by default. Set
`FLEETDECK_NOTES_PATH=/path/to/private-test-notes.json` for tests or a separate
instance. The test suite uses a temporary path and never edits live Notes.

In the existing operator mode, there is no password in the request path. What
protects those surfaces:

- **portal** binds `127.0.0.1` and is fronted by `tailscale serve` with a real
  Let's Encrypt cert. Requests arrive from loopback; the tailnet boundary is
  enforced by Tailscale at the proxy. The per-request CGNAT check
  (`100.64/10`, `fd7a:115c:a1e0::/48`) stays as defence in depth.
- **chat** binds the tailnet address directly and **degrades closed to
  loopback** if Tailscale is down. That bind is the only thing between the
  tailnet and a writable shell, so it must never fall open.
- **ttyd** on `:8784` keeps a credential even on loopback, because it runs with
  `-a` (a URL can shape its command line) and must answer nothing but the chat
  server's proxy.

`FLEETDECK_OPEN=1` lifts the portal's IP check. Don't, except to debug.

`/home?ui=` is a GET that writes a cookie — a state change behind no method
guard, on a surface with no auth. Consistent with everything else here (a
tailnet visitor can already open any tile), and worth naming rather than
letting a reviewer discover it: this is the shape CSRF advice usually flags,
accepted here for the same reason the rest of the surface has no login.

**This is not production-grade.** It is tailnet-gated single-operator tooling.
Before putting it in front of anyone else you want real auth (Cloudflare Access
or Tailscale Serve + an identity header), an audit of the `/t` proxy, and rate
limiting.

---

## The traps

Every one of these cost real debugging time upstream. They're in the code as
comments too.

**Serve the portal on plain HTTP and iPhone Safari breaks three ways at once**,
all of which the phone reports identically as "it won't load": Safari upgrades
typed hostnames to HTTPS first and takes a hard TLS failure; a plain-HTTP origin
isn't a secure context so Add to Home Screen degrades; and off-tailnet requests
get a bare 403. The `tailscale serve` front fixes all three. Don't remove it.

**`launchctl bootout` is asynchronous.** Bootstrapping before the old job has
finished tearing down fails, and a `|| kickstart` fallback cannot rescue it —
there's nothing bootstrapped to kickstart. Both `install.sh` and the CLI poll
until the job is actually gone.

**TCC grants attach to a binary's resolved real path.** That's why `install.sh`
**copies** rather than symlinks, and why a `brew upgrade python` can silently
void a Full Disk Access grant. It's also why the chat server's shebang is
`/usr/bin/python3` (Apple's path never moves) and not the Homebrew one.

**`tmux` renders one window at one size.** A phone attaching to a session that
is also tiled on a monitor is a genuine conflict, not a bug. Two modes, toggled
in the chat's list header:

| mode | attach | effect |
|---|---|---|
| **LIVE** (default) | `new-session -A -s <name>` | browser becomes newest client, `window-size latest` hands it the size. A desktop tile reshapes to the phone while the tab is open and **snaps back when it closes**. |
| **PEEK** | `attach -f ignore-size,read-only -t <name>` | session keeps its geometry, drops keystrokes. Watch an agent without touching it. Cost: tmux pads unused rows with `·`. |

**An iOS software keyboard has no terminal in it.** No Esc, no Tab, no Ctrl, no
arrows, nothing to scroll `xterm` with, and a clipboard the page mostly cannot
read — so the chat was readable on a phone and barely operable. The **⌨ button**
adds a key bar, a Messages-style composer and scrollback buttons under the
thread: on by default below 720px, off above it, and **hidden entirely in peek**,
because peek's promise is that looking costs the session nothing and a send
button would break it.

Every key goes **through tmux** (`/api/send`, `/api/scroll`), never as a
synthetic keystroke into the iframe. Faking events into `ttyd`'s `xterm` would
bind this to the internals of a bundle that moves on every upgrade; tmux is
already how this server reads the fleet. Key names are allowlisted, and the
session name is matched **exactly** against `list-sessions` — tmux treats an
unmatched `-t` as a pattern, which is how a typo reaches the wrong session.

**Don't hardcode `/opt/homebrew`.** Intel Macs put brew in `/usr/local`, and
launchd runs with a PATH containing neither. `chat_server.py` resolves `tmux`
and `ttyd` via `shutil.which()` with both as fallbacks; the plists set an
explicit PATH.

**`install.sh` substitutes `__ROOT__` everywhere in the CLI.** If you add a
literal `__ROOT__` to a comparison or a comment it will be rewritten too. The
drift check builds the token at runtime (`printf '__%s__' ROOT`) for exactly
this reason.

**A POST refused before its body is read leaves those bytes in the socket.**
Direct, nothing notices — the client is closing the connection anyway, which is
why this went unseen. Through `tailscale serve` the proxy keeps the backend
connection alive and sends the next request down the same one, where it lands on
the tail of the last request's JSON: the server parses `"history": []}` as a
request line, answers `400`, and the failure is blamed on whichever innocent
page happened to be next. **Every early refusal must call `_discard_body()`
first** — and only the ones that refuse *before* reading, since draining a body
already consumed blocks on a socket with nothing left to give. There is a
structural check for this in `test_fleet_map.py`: two requests down one
connection, asserting the second is answered on its own terms.

**A connect timeout stays on the socket as a read timeout.**
`create_connection`'s timeout is a CONNECT deadline, but it remains as a
per-read deadline afterwards — so the 10s meant for "is anything listening" was
also being applied to every gap between chunks, and the skin front was hanging
up on a local LLM that thinks for tens of seconds before its first token. The
socket now gets an explicit read timeout once the connection is through:
900s, overridable with `FLEETDECK_SKIN_READ_TIMEOUT`. Finite on purpose — a
wedged upstream must not be able to pin a thread forever.

---

## Layout

```
config.json          identity, ports, agent filters      ← edit this
services.json        the tile registry, hot-reloaded     ← and this
glyphs.json          the line-glyph library              ← and this, to add icons
assets/glyphs/       every glyph, pre-rendered           ← so a clone needs no browser
portal_server.py     discovery + page + PWA + agent API + the framed surfaces
adopt.py             clone, read, plan, install  (engine + CLI)
adopt_server.py      the paste-a-URL surface
chat_server.py       tmux thread list + ttyd proxy + the input dock
skin_server.py       dresses apps you cannot edit
fleet_map.html       the fleet map page (opt-in, metadata only)
fleet_map_reader.py  validates a collector snapshot before it is served
fleet_map_infra.py   the layered infrastructure projection
fleet_explainer.py   answers questions about the snapshot on screen
assets/wb-logo-256.mp4  the moving mark under the clock (235KB)
make-icons.py        glyphs.json → home-screen PNGs
skins/*.css          per-app palette overrides
icons/               generated PNGs (git-ignored)
audit.py             proves each tile's claim separately
bin/fleetdeck        CLI (rendered into ~/bin on install)
launchagents/*.tmpl  plist templates
assets/icon-*.png    the portal's own home-screen icon   ← replace with theirs
ttyd-index.html      ttyd's index + the viewport meta it omits
install.sh           reconcile machine with repo
auth                 loopback ttyd credential (git-ignored, minted on install)
```

## CLI

```
fleetdeck start [portal|chat|skin] bootstrap + set up the serve front
fleetdeck stop  [portal|chat|skin]
fleetdeck restart
fleetdeck status                  up/down, pids, urls
fleetdeck doctor                  status + deps + serve map + drift + agent health
fleetdeck url                     the two links (portal is https, chat is http)
fleetdeck log [n]
fleetdeck edit                    $EDITOR services.json
fleetdeck icons [id...]           rasterise glyphs into home-screen PNGs
fleetdeck adopt <url> [--yes]     inspect a repo; --yes installs it
fleetdeck audit                   verify every tile's claim
```

## Uninstall

```bash
UID_N=$(id -u); PREFIX=$(python3 -c 'import json;print(json.load(open("config.json"))["label_prefix"])')
for l in $PREFIX.fleetdeck-portal $PREFIX.fleetdeck-chat; do
  launchctl bootout "gui/$UID_N/$l" 2>/dev/null
  rm -f ~/Library/LaunchAgents/$l.plist
done
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --https=8790 off
rm -f ~/bin/fleetdeck
```
