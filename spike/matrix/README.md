# Matrix federation spike (Phase 5)

Runnable prototype behind the go/no-go report in
[`docs/spikes/matrix-federation/`](../../docs/spikes/matrix-federation/README.md).

```bash
cd spike/matrix
./run.sh            # dev Synapse + two bot installs exchange one E2EE client card; prints proof
./run.sh clean      # wipe containers + data/
# optional hand-off into a running Buzzowl (host):
BUZZOWL_URL=http://host.docker.internal:8000 AGENT_SERVICE_TOKEN=… BUZZOWL_ORG_ID=8 ./run.sh
```

- `federation_bot.py` — sender/receiver roles (matrix-nio 0.26 + vodozemac, all in Docker)
- `EVENT_SCHEMA.md` — event/room schema draft derived from the measurements
- `ux_mock.html` — static consent-UX mock (partners, per-client share scope, review queue)
- `data/` (gitignored) — Synapse state, bot stores, `proof.json`, received card + attachment

Nothing here is production code; the only change to the app is the token-gated
`POST /api/internal/federation/inbound` endpoint used to land a received card as a
`shared_external` document.
