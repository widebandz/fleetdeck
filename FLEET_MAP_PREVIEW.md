# Local fleet map preview

The fleet map is a metadata-only preview with a local adoption draft creator.
Its listener cannot write to the registry, cards, tmux, or chat routes. It is
disabled on the normal Fleetdeck portal. Run it on a separate unused loopback
port that is **not** mapped through Tailscale Serve.

From an isolated Fleetdeck checkout:

```sh
test -e services.json || printf '{"groups":[],"services":[]}\n' > services.json
COLLECTOR=/absolute/path/to/tm-fleet-snapshot
REGISTRY=/absolute/path/to/tm-fleet-registry
SESSIONS_CONF=/absolute/path/to/sessions.conf
HOST_ID=stable-local-host-id
# Optional: REMOTE=/absolute/path/to/tm-fleet-remote
# Optional: REMOTE_MAP=/private/remote-hosts.json
# Optional: REMOTE_CACHE=/private/remote-cache
# Optional: RUNTIME=/absolute/path/to/tm-fleet-runtime
FLEETDECK_HOST=sample.invalid \
FLEETDECK_BIND=127.0.0.1 \
FLEETDECK_PORT=18790 \
FLEETDECK_FLEET_MAP=1 \
FLEETDECK_FLEET_SNAPSHOT="$COLLECTOR" \
FLEETDECK_FLEET_REGISTRY="$REGISTRY" \
FLEETDECK_FLEET_HOST_ID="$HOST_ID" \
# Add FLEETDECK_FLEET_REMOTE, FLEETDECK_FLEET_REMOTE_HOST_MAP,
# FLEETDECK_FLEET_REMOTE_CACHE_DIR, and FLEETDECK_FLEET_RUNTIME only together
# when those private sources are configured.
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

Select a local observed session or pane, then choose **+ New Agent**. Existing
assigned cards default to **Preserve existing card exactly**. Authoring an
assigned card requires Owner, concern, Owns, and Refuses. A live-only session
without a card or standard declaration also needs an absolute workspace root.
The review shows the planned registry binding and card action, with the
registry revision pinned. Download `fleet-adoption-draft.json`, then run the
displayed `~/bin/tm-fleet-registry adopt --draft-file ... --dry-run --json`
command over your operator SSH session. Read the CLI's authoritative card diff
and registry change before running the displayed `--apply --json` command.
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
python3 test_fleetdeck.py --unit
```

Do not add this port to Tailscale Serve or enable this flag on the installed
portal until tailnet exposure of the redacted topology is reviewed.
