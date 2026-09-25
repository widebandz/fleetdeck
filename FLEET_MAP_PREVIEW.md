# Local fleet map preview

The fleet map is a metadata-only preview with a local adoption draft creator.
Its listener cannot write to the registry, cards, tmux, or chat routes. It is
disabled on the normal Fleetdeck portal. Run it on a separate unused loopback
port. Tailnet access is an explicit, separately reviewed Tailscale Serve route.

From an isolated Fleetdeck checkout:

```sh
test -e services.json || printf '{"groups":[],"services":[]}\n' > services.json
COLLECTOR=/absolute/path/to/tm-fleet-snapshot
REGISTRY=/absolute/path/to/tm-fleet-registry
SESSIONS_CONF=/absolute/path/to/sessions.conf
HOST_ID=stable-local-host-id
FLEETDECK_HOST=sample.invalid \
FLEETDECK_BIND=127.0.0.1 \
FLEETDECK_PORT=18790 \
FLEETDECK_FLEET_MAP=1 \
FLEETDECK_FLEET_SNAPSHOT="$COLLECTOR" \
FLEETDECK_FLEET_REGISTRY="$REGISTRY" \
FLEETDECK_FLEET_HOST_ID="$HOST_ID" \
TM_SESSIONS_CONF="$SESSIONS_CONF" \
python3 portal_server.py
```

Open `http://127.0.0.1:18790/fleet-map`. The `services.json` above is an
ignored, synthetic registry needed to start Fleetdeck. The collector path is
supplied explicitly; the portal invokes the collector with the selected host
ID and `--json`, then pipes its JSON directly to the registry CLI's read-only
`join --snapshot - --json`. Both commands have fixed argument lists, timeouts,
and output limits. No snapshot temp file is written. The browser payload is
projected through a strict schema and privacy filter. If the registry join
fails, the current tmux graph remains visible, registry facts become stale,
and the creator cannot produce an applyable draft.

For mapped remote hosts, set `FLEETDECK_FLEET_REMOTE` to the absolute remote
collector executable, `FLEETDECK_FLEET_REMOTE_HOST_MAP` to a private host map,
and `FLEETDECK_FLEET_REMOTE_CACHE_DIR` to its private cache directory. The
reader pipes the local graph to the remote merge before registry join. Set
`FLEETDECK_FLEET_RUNTIME` to the absolute runtime projector executable to
attach fresh attested occupants and managed leases after registry join. These
are optional read stages; the host map, SSH targets, raw errors, and runtime
receipt files do not enter the browser payload.

Set `FLEETDECK_FLEET_INFRA` to the absolute `fleet_map_infra.py` executable
to add bounded, read-only infrastructure facts after the runtime stage. Set
`FLEETDECK_FLEET_SERVICES_FILE` to an existing service registry JSON if the
host has one. The enrichment inventories safe service names and port-listener
status, selected LaunchAgent jobs, Trace's declared duties, instruction-file
presence, a symbolic outbox queue, and configured Tailscale Serve routes.
It does not read message bodies, instruction contents, or raw paths into the
browser. A listener on a registered port does not verify which process owns
it; a configured route or sender does not prove a request or message was
delivered.

### Optional responsibility briefs

The infrastructure stage can read a locally authored scope brief from
`~/.config/agent-session-memory/scope-briefs/<session>.json` (or the private
directory named by `FLEETDECK_FLEET_SCOPE_BRIEF_DIR`). Its schema is
`fleet-map.scope-brief.v1`: `session`, `role`, `mission`, `source`, `as_of`,
`status: "declared"`, `association: "session_name_only"`, and bounded `items`.
Each item has a fixed `kind`, `text`, `source`, `evidence`, and `as_of`.
Handoffs can name an incoming or outgoing direction and a counterparty label.
Completion checks and evidence can carry an `outcome` and shared `check_key`
so a dated result points to its required check. Approval gates can carry an
explicit `decision` and `approver`; a pending decision is not approval. The supported
kinds cover owned work, inputs, outputs, boundaries, dependencies, approval
gates, tool requirements and observed availability, handoffs, completion
checks, and completion evidence. Keep the file private and write only facts
safe to show on the tailnet map. The browser allowlist rejects private values,
invalid kinds, and oversized briefs.

A brief attaches only to an observed session with the matching name. It is a
dated declaration, not an identity card or verified agent binding. A handoff
counterparty label is not a verified identity. The brief does not add graph
pipes or certify that a requirement was met. A required
completion check stays separate from dated evidence that a check actually
ran. If an existing brief becomes unreadable or invalid, the map labels a retained
prior brief as last known. Removing a brief removes its model.

To enable those stages, export the following before running the preview
command above:

```sh
export FLEETDECK_FLEET_REMOTE=/absolute/path/to/tm-fleet-remote
export FLEETDECK_FLEET_REMOTE_HOST_MAP=/private/remote-hosts.json
export FLEETDECK_FLEET_REMOTE_CACHE_DIR=/private/remote-cache
export FLEETDECK_FLEET_RUNTIME=/absolute/path/to/tm-fleet-runtime
export FLEETDECK_FLEET_INFRA=/absolute/path/to/fleet_map_infra.py
export FLEETDECK_FLEET_SERVICES_FILE=/private/services.json
```

Select a local observed session or pane, then choose **+ New Agent**. Existing
assigned cards default to **Preserve existing card exactly**. Authoring an
assigned card requires Owner, concern, Owns, and Refuses. A live-only session
without a card or standard declaration also needs an absolute workspace root.
The review shows the planned registry binding and card action, with the
registry revision pinned. Download its unique
`fleet-adoption-<agent-id>-r<revision>-<nonce>.json`, then run the
displayed `~/bin/tm-fleet-registry adopt --draft-file ... --dry-run --json`
command over your operator SSH session. Read the CLI's authoritative card diff
and registry change before copying and running the displayed `--apply --json`
command. If the browser renames or moves the download, replace the path in
both commands with its actual saved path.
The CLI performs validation, compare-and-swap, backup, and read-back. Refresh
the map after Apply to see the planned binding attached to that session. A
planned binding does not verify the current pane occupant. The downloaded
draft can contain a local root and owner, so keep it private.

On this preview listener, GET is limited to `/healthz`, `/fleet-map`, and
`/api/fleet-map`; all POSTs are rejected. Legacy terminal, chat, and board
routes are unavailable. The collector never supplies pane text to the map.
If a source fails, the map keeps its last valid collection time and marks
retained facts as last known. Computed router pipes describe eligibility under
the on-disk routing file and tmux at collection time; active router policy and
message delivery remain unverified.

Run the offline checks with:

```sh
python3 -m unittest -v test_fleet_map.py
python3 -m unittest -v test_fleet_map_infra.py
python3 test_fleetdeck.py --unit
```

The local reviewed instance fronts loopback port `18790` with an additive,
tailnet-only Tailscale Serve HTTPS route on `18970`. It does not use Funnel or
publish the map. Before adding a route on another machine, review the redacted
browser payload, use an unused HTTPS port, confirm existing Serve routes stay
unchanged, and fetch the exact tailnet hostname URL. Keep the map flag off on
the installed portal until that integration is reviewed separately.
