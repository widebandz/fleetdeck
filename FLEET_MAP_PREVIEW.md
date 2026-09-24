# Local fleet map preview

The fleet map is a read-only, metadata-only preview. It is disabled on the
normal Fleetdeck portal. To use it, run a separate listener on an unused
loopback port that is **not** mapped through Tailscale Serve.

From an isolated Fleetdeck checkout:

```sh
test -e services.json || printf '{"groups":[],"services":[]}\n' > services.json
COLLECTOR=/absolute/path/to/tm-fleet-snapshot
SESSIONS_CONF=/absolute/path/to/sessions.conf
FLEETDECK_HOST=sample.invalid \
FLEETDECK_BIND=127.0.0.1 \
FLEETDECK_PORT=18790 \
FLEETDECK_FLEET_MAP=1 \
FLEETDECK_FLEET_SNAPSHOT="$COLLECTOR" \
TM_SESSIONS_CONF="$SESSIONS_CONF" \
python3 portal_server.py
```

Open `http://127.0.0.1:18790/fleet-map`. The `services.json` above is an
ignored, synthetic registry needed to start Fleetdeck. The collector path is
supplied explicitly; the portal invokes only `--host-id local --json`, with a
timeout and output limit. Its browser payload is projected through a strict
schema and privacy filter. The page contains only an illustrative tour until
the local API returns a valid snapshot.

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
