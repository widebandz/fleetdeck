# Changelog

Releases are named in commit subjects (`fleetdeck <version> — …`); there are no
git tags, so the commit is the reference. Dates are the release commit's date.

---

## `1.3.0` — 2026-10-04

Two branches that had both been running on the operator's machine for a week,
brought under `main`. Versions 1.0–1.2 were merged on the day they were cut;
1.3.0 documents what was already serving.

### The phone surface behaves like one installed app

- **`/app/<id>` framing.** An origin is scheme plus host plus port, so every
  service on the machine was cross-origin and Android answered each tap by
  dropping the installed app into a Custom Tab with an address bar. The
  top-level document now stays on this origin and the service is framed inside
  it — no proxying, no path rewriting. The set is closed and keyed by registry
  id, so no request names a URL. Framed permissions are granted per app;
  `cockpit` alone gets `microphone`.
- **Notes** (`/notes`) — capture for voice-note transcripts. Four fields, a
  closed status set, the original text kept verbatim on every path in, one
  validating door.
- **Cashflow** (`/cashflow`) — the accountant session's cash view, read from
  disk per request, with the Fleetdeck bar injected at serve time and never
  written back to the agent's file.
- **CALL opens the native Messages thread** at the handle the Mac sends *from*,
  replacing a voice surface on the cockpit. Trace already had an inbox; a second
  one meant reading half of each conversation in each place.
- The moving Wideband mark under the clock, a Live Terminal Network key, and
  cross-document view transitions — three declarations of CSS, inert where
  unsupported.
- The phone screen fits again without scrolling (it was 105px over), verified on
  eight viewport sizes.

### A chat input dock

- A key bar, a Messages-style composer and scrollback buttons behind the **⌨**
  button: on below 720px, off above it, hidden entirely in peek — peek's promise
  is that looking costs the session nothing.
- Everything routes **through tmux** (`/api/send`, `/api/scroll`), never a
  synthetic keystroke into the iframe. Key names are allowlisted and the session
  name is matched exactly, since tmux treats an unmatched `-t` as a pattern.

### The fleet map

- A read-only, **metadata-only** picture of the tmux fleet: declared versus
  observed state, layered infrastructure projections, agent responsibility
  briefs, and `/api/fleet-explain` for questions about the snapshot on screen.
- Off unless `FLEETDECK_FLEET_MAP=1`, and meant to run as its own listener,
  which refuses every inherited portal route — including the ones that can read
  pane and chat content.
- `frame-ancestors` names exactly one origin (this portal, behind the same
  tailnet gate) instead of `'none'`, which refused *every* framer including this
  server's own and was the last thing breaking the installed app out of
  fullscreen. The rest of the header is unchanged.

### Fixes

- **Undrained POST bodies.** A refusal that answered before reading left those
  bytes in the socket; through `tailscale serve` the next request was parsed
  from the tail of the last one's JSON and answered `400`, which got blamed on
  whichever page came next. All early-refusal sites now drain first — and only
  those that refuse before the read. Covered by a structural check that sends
  two requests down one connection.
- **The skin front no longer hangs up on a slow upstream.**
  `create_connection`'s timeout stays on the socket as a per-read deadline, so
  10s meant for "is anything listening" was also applied to every gap between
  chunks. Explicit read timeout once connected: 900s, `FLEETDECK_SKIN_READ_TIMEOUT`.

### First-run onboarding

Installer-backed customer phone mode: setup state, first-goal status, an
`onboarding` config block, a control token gating operator pages and non-Notes
POSTs, and `test_onboarding.py`. This arrived in the working tree alongside the
phone-surface work and could not be separated from it — it lives in the same
file.

### Tests

34 → 132 in `test_fleetdeck.py`, plus 6 onboarding, 26 fleet map, 19 map infra,
11 Jev routing, 9 Jev shadow and 6 explainer.

---

## `1.2.0` — 2026-09-03

The simple screen (`/phone`), a per-device `fd_home` toggle so the desk keeps
forty tiles and the phone keeps six, and a CALL key reaching the fleet steward.

## `1.1.0` — 2026-08-28

Home-screen icons rasterised from the same glyphs the board draws, and skinning
an app you don't own — a front that rewrites only the `<head>` of a container
you cannot edit.

## `1.0.0` — 2026-08-25

Client-neutral fork of `wb-portal` and the `wb-tunnel` chat: the board, the
tmux thread list, the honesty rule, and the launchd agent half that a port scan
cannot see.
