# Sharing clients with other Buzzowl installs (Matrix federation)

Two reps who each run their own Buzzowl can share a client and research it
together: research, OSINT, findings, signals and the company profile of that
client sync between the installs, **end-to-end encrypted over the Matrix
protocol**. Contacts, notes, meetings, mails, deals and tasks never leave an
install — they are personal.

The same sharing model works inside one deployment (two orgs on one server) without
Matrix; this page is about the cross-install case.

## How it works (one minute)

- Each org gets **one Matrix bot account** on a homeserver. The bot keeps its
  encryption keys under `data/federation/<org id>/` — keep that directory
  (losing it means re-pairing every partner).
- Two installs **pair**: admin A invites `@buzzowl-b:their-homeserver`; admin B
  accepts; both compare the bots' **device fingerprints on a call** and click
  Verify. From then on Buzzowl sends only to that pinned device — a new,
  unverified device on the partner's side stops sharing until you verify again.
- A rep shares a client with the partner from the client page; the partner's
  Home shows the invitation; on accept both sides sync (Matrix room per partner
  pair, encrypted `de.buzzowl.sync` events, larger documents as encrypted
  attachments). Updates, deletions, profile changes and monitoring handover
  follow automatically. Leaving stops the sync; copies already received stay
  (they are marked as detached).
- Everything a partner sends is stored with provenance (`shared by <partner>`,
  Matrix event id) and never overwrites your own documents.

## Which homeserver?

You need *a* Matrix homeserver both installs can reach. Three practical options:

1. **A shared homeserver** (simplest): one party runs the bundled Synapse
   publicly, or you use a homeserver you both trust. Both installs point
   `federation.homeserver_url` at it. No server-to-server federation is needed.
2. **Your own homeserver each**: real Matrix federation between the two
   homeservers. Works out of the box with Synapse but requires a public DNS name
   and TLS (443/8448) for each — standard Matrix operations, not covered here.
3. **Buzzowl Cloud** (later): the hosted platform runs a rendezvous homeserver.

Public homeservers such as matrix.org are not recommended for business data:
the operator sees the room metadata (who pairs with whom, when), never the content.

## Bundled Synapse (`--profile federation`)

```bash
# .env
SYNAPSE_SERVER_NAME=synapse                    # or your public DNS name
SYNAPSE_REGISTRATION_SHARED_SECRET=<long random>

# config.yaml
federation:
  enabled: true
  homeserver_url: http://synapse:8008
  registration_shared_secret: <the same secret>

docker compose --profile federation up -d
```

The first start generates `homeserver.yaml` (registration closed; Buzzowl creates
its bot accounts through the shared secret). Then, as an org admin: Settings →
*Sharing with other Buzzowl installs* → Connect (username e.g. `buzzowl-acme`,
"create the account" ticked). Your Matrix ID and fingerprint appear; give the
ID to your partner.

## Trust model in one paragraph

Identity = the partner's Matrix ID **plus** the pinned Ed25519 device key. Rooms
are invite-only, encrypted from the first event, `history_visibility: joined`,
and only your bot can invite — a third member cannot appear without your bot
doing it, and if one does anyway the partner is blocked. Data is sent to verified
devices only; hello (org name) is the sole message allowed before verification.
Received events are stored replay-safe by event id and applied only when they
came from the pinned device. Full details: `docs/spikes/matrix-federation/`
(threat model, GDPR position, event schema).

## Operations

- Status, partners, fingerprints, verify/disconnect: Settings (admin).
- `POST /api/federation/tick` drains the outbox/inbox immediately (debugging).
- Tables: `federation_identities`, `federation_partners`, `federation_outbox`
  (`sent_at`, `error`), `federation_inbox` (`applied_at`, `verified`, `error`).
- Disable entirely with `federation.enabled: false`.
