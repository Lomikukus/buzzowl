# Matrix federation — design spike report (Phase 5)

Date: 2026-08-17 · Status: **spike complete — conditional GO** (see §7)
Artifacts: [`spike/matrix/`](../../../spike/matrix/) (runnable prototype + `EVENT_SCHEMA.md` + `ux_mock.html`),
[threat-model.md](threat-model.md), [gdpr-position.md](gdpr-position.md), [matrix-facts.md](matrix-facts.md).

## 1. Question

Can two independent Buzzowl installs share selected client cards with each other
over the Matrix protocol — end-to-end encrypted, with an explicit consent handshake,
without a central Buzzowl service — and should we build it? Deliverable per plan
(D5): two local instances + a dev Synapse exchange one client card E2EE, plus a
threat model, event-schema draft, consent-UX mock and a GDPR lawful-basis position.

## 2. What was built and measured

`spike/matrix/run.sh` brings up a dev Synapse and two bot "installs" (Python,
`matrix-nio[e2e]`, everything in Docker) and runs the full exchange. Two clean runs,
both green. Measured facts (proof.json on both sides):

| # | Step | Result |
|---|---|---|
| 1 | Install A creates an invite-only room, encrypted from the first event, invites `@buzzowl-b` | ok — `preset private_chat`, `m.room.encryption` in `initial_state` |
| 2 | Install B accepts the invite (= consent step; in the product an admin click) | ok — A observes the join before sending anything |
| 3 | A sends `de.buzzowl.client_card` (848 B JSON) as an **encrypted custom timeline event** | ok — homeserver stores `type: m.room.encrypted`, `algorithm: m.megolm.v1.aes-sha2`, ciphertext 1 299 B; the string "Acme" does not appear anywhere in the server's copy |
| 4 | B decrypts, validates schema, records provenance, POSTs to `/api/internal/federation/inbound` | ok — stored as `documents` row `type=shared_external`, `source=federation`, `review_status=pending`, `doc_id=fed:<room>:<card_id>` |
| 5 | A sends an update (`m.relates_to` / `m.replace`) | ok — B receives `m.new_content` |
| 6 | A sends a `de.buzzowl.index` **state** event | **plaintext on the server and via API** — state events are never encrypted |
| 7 | A sends a ~70 KiB card | rejected `M_TOO_LARGE` — 65 536 B event cap incl. envelope confirmed |
| 8 | A uploads a 130 KB markdown as **encrypted media** (`m.file` + `file` key) | ok — B downloads, decrypts, SHA-256 matches |
| 9 | Device trust | B's device was **unverified** at send time (`ignore_unverified_devices=True`); receipt shows `verified_device: false` |
| 10 | Crypto backend | image resolved to **matrix-nio 0.26.0 + vodozemac 0.10.0** (Rust); no libolm needed |

Bot code: ~330 lines. Not tested: two *separate* homeservers federating over TLS
(deployment concern; Synapse handles server-to-server signing/TLS — see §5).

## 3. Design consequences (schema)

Full draft in [`spike/matrix/EVENT_SCHEMA.md`](../../../spike/matrix/EVENT_SCHEMA.md). The measurements fix these rules:

- **All data in encrypted timeline events** with custom types (`de.buzzowl.client_card`,
  `…document` as `m.file`, `…retract`, later `…finding`, `…ack`). **No data in state
  events**, opaque room names/topics (metadata is plaintext for both homeservers).
- **Plaintext budget ≤ 40 KiB per card** (65 536 B cap on the whole PDU incl. envelope
  and signatures, ~1.5× ciphertext overhead measured; the fact sheet's independent
  estimate is 32–45 KiB); larger content → encrypted media (≤ 5 MB in v1, Synapse
  default `max_upload_size` 50 M), referenced from the card.
- Updates = `m.replace`; receiver keeps the latest per `card_id`; edits accepted only
  from the original sender. **`m.relates_to` is cleartext by spec even in E2EE rooms**
  (fact sheet §10) — homeservers see "event X replaced event Y", never the content;
  acceptable, but no client identifiers may ever appear in relation metadata.
- Retract = request event + redaction of the originals; the receiver's Buzzowl deletes
  its copy automatically, but the sender cannot force it — UI says so.
- Envelope carries `schema`, `kind`, `card_id`, `sender_org`, `share_scope`; every
  string is plain text, length-capped, stored with `untrusted_remote: true`.
- Room: `history_visibility: joined`, `join_rules: invite`, power levels that make
  inviting a third member noisy; two-member invariant enforced by the bot.

## 3b. External facts that shape the design (fact sheet, spec v1.19)

Full sheet: [matrix-facts.md](matrix-facts.md) (Sonnet research, every item sourced).

- State events can never be encrypted (no encrypted-state mechanism in the spec) —
  matches measurement §2 row 6. Custom timeline types are encrypted (payload `type`
  inside the ciphertext) — matches row 3.
- 65 536-byte cap applies to the whole PDU, ciphertext included — matches row 7.
- **`m.relates_to` must be sent in cleartext** even in encrypted rooms (edits,
  threads, reactions) → relationship metadata is visible to homeservers.
- **matrix-nio has no cross-signing and no server-side key backup** (README) — only
  manual/SAS device verification. Consequence: device pinning is per device; a partner
  bot that rotates its device must re-verify (or we would need a standing
  `ignore_unverified_devices=True`, which the threat model rules out).
- matrix-nio 0.26.0 (2026-07-23) moved to vodozemac; ~21-month gap before that.
  mautrix-python is maintained but still libolm-based; matrix-rust-sdk Python
  bindings are archived → nio is the only reasonable Python choice today.
- Federation between homeservers is **TLS-only** by design (no HTTP shortcut even on
  one Docker network) — the untested cross-homeserver run needs certificates
  (`federation_verify_certificates: false` + custom CA for a lab).
- No backward secrecy: keys already delivered stay usable; redaction strips only the
  server copy. Synapse retention policies are experimental and per-homeserver.
- `history_visibility` defaults to `shared` → must be set to `joined` explicitly.

## 4. Threat model — headline findings

Full document: [threat-model.md](threat-model.md) (Opus review, verified against the code).

- **Central risk = rogue device injection.** With `ignore_unverified_devices=True`
  a malicious/compromised partner homeserver can add a device to the partner bot and
  receive every future Megolm session. v1 must do **TOFU + pinning**: show both
  fingerprints (hex + SAS emoji) at pairing, admin confirms out-of-band, then refuse
  to send to any unpinned device and alert.
- **Highest inbound risk = prompt injection into agents.** `db.hybrid_search`
  (`db.py:2434`) has only a positive `doc_type` filter, so `shared_external` docs
  would flow into chat/knowledge/MCP context (4 call sites) and from there into the
  persisted prompt log. Exclusion must live inside `hybrid_search`; remote content
  wrapped with provenance when a human opts it in; write actions traceable to
  federated input stay human-confirmed.
- **`documents UNIQUE(org_id, doc_id)`** → a partner-chosen id could overwrite a local
  doc; fixed in the spike endpoint already (`fed:<room_id>:<card_id>` namespacing).
- Consent semantics: membership can change (third member, power levels, tombstone,
  history visibility) → treat any deviation as "pause + alert", never follow a room
  upgrade automatically.
- Revocation is a request, deletion unenforceable across federation; partnership graph
  and timing are visible to both homeserver operators → self-hosted/dedicated
  homeserver only, opaque room metadata.
- Residual risks that no design removes: post-delivery misuse by the partner,
  unenforceable erasure, partner-side compromise, metadata visibility, prompt
  injection being mitigable-not-solvable.
- The reviewer's transport opinion: for pure bot-to-bot, **signed JSON over HTTPS is
  simpler and safer**; Matrix earns its complexity only for reachability/store-and-
  forward, third-party homeservers, humans-in-the-loop, or many-party rooms. See §7.

## 5. GDPR position — headline

Full memo: [gdpr-position.md](gdpr-position.md) (research memo, not legal advice).

- Lawful basis for A → B disclosure of business-card-grade contact data:
  **Art. 6(1)(f)** is defensible for discrete, human-decided shares within an active
  business relationship; bulk/automatic sharing drifts toward "address trading",
  which German practice (DSK, post-Listenprivileg) rejects. UWG §7 governs how B may
  then contact the person — separate from the disclosure.
- Roles: **independent controllers** (EDPB 07/2020 patterns) — no Art. 26, no
  processor; an in-product controller-to-controller acknowledgement at "connect".
- **Art. 14** notice duty on B (first contact / ≤ 1 month) — the product must nudge
  with a template + provenance; **Art. 19** notification on erasure/rectification →
  recipient list + retract pipeline + audit; honest copy that deletion cannot be
  verified. EU/EEA-only partners in v1. Self-hosted/dedicated homeserver under a
  processor agreement; ROPA entry template; Art. 21 objection path via A.
- Verdict of the memo: **go, conditioned on the 12-item checklist being in v1.**

## 6. Cost of operating Matrix (what a self-hoster takes on)

- A Synapse (own Postgres, TLS cert, DNS `.well-known`/SRV, federation port), patched
  continuously — a second production service; or a dedicated managed homeserver
  with a DPA (no public/shared servers for tenant data).
- Bot credential + E2EE store hygiene (dedicated 0600 volume, encrypted store,
  revoke/re-pair flow).
- matrix-nio release cadence is slow (0.25.2 → 0.26.0 took ~21 months) for a
  security-critical dependency; vodozemac backend is current.

## 7. Recommendation

**Conditional GO — Matrix as the v1 transport, built behind a transport interface, with
the must-have controls in scope from day one.**

Why go: the prototype proves the whole path (consent → E2EE card → update → media →
review-queue doc) with ~330 lines; Matrix gives self-hosters exactly what a plain
peer-to-peer API cannot without building it: **reachability behind NAT, store-and-
forward while a peer is offline, server-to-server identity/TLS/signing already
solved**, and a road to humans-in-the-loop (partner reps or Element clients can join
the same rooms later). The controls that dominate the risk (share-scope discipline,
review queue, no-HTML rendering, doc-id namespacing, agent-context exclusion,
provenance, retract pipeline) are transport-independent and must be built either way.

Why conditional: the threat model's transport critique is right for the *narrow* v1
(bot-to-bot only). We keep that option open cheaply — the federation module gets a
`Transport` seam (`send_card`, `send_document`, `send_retract`, `on_inbound`,
`pair`, `unpair`) so a signed-JSON-over-HTTPS transport can replace Matrix without
touching the card model, review queue or UI if Synapse operation proves too heavy in
beta.

**Must-have before real data crosses a room** (merged threat-model + GDPR lists):

1. Device pinning at pairing (fingerprint + SAS emoji in the UI); refuse unpinned devices; alert. nio has no cross-signing, so a partner device rotation = explicit re-verification flow in the UI (design it, don't hide it).
2. Two-member invariant, `history_visibility: joined`, opaque room names, baseline power levels; tombstone = disconnect.
3. `shared_external` excluded from agent/LLM context inside `hybrid_search`; not embedded until human-accepted; provenance-wrapped when opted in; prompt-log masking.
4. Allowlist schema validation, ≤ 40 KiB cards, media caps, per-room rate limits in the bot; dedupe on event id; edits only from original sender.
5. `doc_id = fed:<room>:<card_id>`; `org_id` from the receiving bot session, never from the payload.
6. Remote content rendered as text/sanitised markdown, no HTML, no remote images.
7. Per-client, per-partner share toggle as a discrete human action; **minimum default scope** (profile only); contacts/findings opt-in; notes/transcripts/mails/deals hard-excluded; outbound preview + audit "what we shared".
8. Review queue: badged, read-only, link/copy/dismiss by a human — never auto-merged.
9. Retract pipeline (event + redaction) + recipient list per record + honest "cannot force deletion" copy; Art. 19 hook on erasure/rectification.
10. Art. 14 nudge with template on the receiving side; provenance on every card.
11. Partner terms (independent controllers) accepted at connect; EU/EEA-only partners in v1; self-hosted or dedicated homeserver with federation allow-list.
12. Credential hygiene: dedicated encrypted volume, no payloads in logs, revoke/re-pair; matrix-nio ≥ 0.26 (vodozemac) pinned; Synapse patch process owned.

Nice-to-have (v1.1+): cross-signing instead of per-device pinning, aggressive Megolm
rotation + homeserver retention, `de.buzzowl.ack` receipts, `de.buzzowl.finding`
stream, application-layer Ed25519 signatures as defence in depth, padding/batching.

## 8. Sized implementation plan (Phase 5b — after the Phase 6 hosted decision, or interleaved)

| # | Ticket | Size | Notes |
|---|---|---|---|
| 1 | `federation/` service container (nio bot, sync loop, persistent device, encrypted store volume) + `Transport` interface + internal API contract with the server | M | reuse spike bot; APScheduler-free, own container like agent-pi |
| 2 | Pairing UX: Settings › Partners (invite by MXID, incoming invites, fingerprint/SAS verify, pin, disconnect) + `partners` table (org, mxid, room_id, pinned device keys, terms accepted, status) | M | must-haves 1, 2, 11 |
| 3 | Outbound: per-client share toggle + scope + preview, card builder from in-scope docs only, `m.replace` updates on change, retract on unshare/erasure, "what we shared" audit | M | must-haves 7, 9 |
| 4 | Inbound: review queue page (`/inbox/shared`), link/copy/dismiss, Art. 14 nudge, provenance panel, unverified-device hold, media download/verify | M | must-haves 6, 8, 10 |
| 5 | Agent safety: `hybrid_search(exclude_types=…)` default, provenance wrapper for opted-in docs, prompt-log masking, tests | S | must-have 3 |
| 6 | Hardening: schema allowlist + caps + rate limits + dedupe, doc-id namespacing (done in spike), membership/PL/tombstone watchdog | S–M | must-haves 4, 5 |
| 7 | Deployment: `--profile federation` (Synapse + its Postgres) with reverse-proxy TLS + `.well-known` guidance, or "bring your own homeserver" docs; second-homeserver federation test | M | not yet tested in the spike; federation is TLS-only, so the lab needs a private CA |
| 8 | GDPR pack: partner terms text, Art. 14 template, ROPA template, honest UI copy | S | needs a lawyer/DPO pass before launch |

Total ≈ **L** (comparable to Phase 3). Gate before ticket 3 ships to real users: an
external security review of tickets 1–2 (device trust) and a DPO sign-off on ticket 8.

## 9. Open questions for the user

1. Ordering: run 5b before Phase 6 (hosted) — federation is a self-host differentiator —
   or after? (Recommendation: after the hosted *decision*, before hosted *build*, so
   the partner model is designed once for both.)
2. Homeserver stance for v1: ship a `federation` compose profile with Synapse, or
   documentation-only "bring your own homeserver"? (Recommendation: profile, because
   most self-hosters won't operate Synapse by hand.)
3. Is human-in-the-loop (partner reps in the room / Element clients) a real goal? If
   clearly not, the HTTPS transport should be reconsidered before 5b starts.

## 10. Addendum — vision clarified by the product owner (2026-08-17, after the spike)

The sharing model is **collaboration between individual reps**, not read-only cards
between partner firms: two users (each on their own instance, or hosted tenants)
share a *client* and from then on **both research and monitor it**, so the same
research is not repeated. Consequences for this report:

- The shared object becomes the client's **company knowledge** (profile,
  research/osint/finding documents, signals, monitored sources), synced both ways
  with provenance; **contacts, notes, meetings, outreach, deals, tasks stay private
  by default**. That keeps most shared data non-personal (GDPR simplifies; §5
  contact rules apply only when a user opts contacts in). The review queue shrinks
  to a conflict/dispute view; "never auto-merge" narrows to *personal* fields.
- Schema: `client_card` stays as the bootstrap, plus a `de.buzzowl.document`
  stream per finding/research doc (already sketched) and a light coordination event
  (`de.buzzowl.monitor` — who runs monitoring for the shared client, so heartbeats
  are not duplicated). Everything else in EVENT_SCHEMA.md holds.
- Transport: the reps' instances are typically **behind NAT** (laptop/home server) —
  direct HTTPS push cannot reach them; a relay is required, and a Matrix homeserver
  *is* a relay with E2EE built in. The hosted Buzzowl platform can run that
  homeserver as the rendezvous point (operator sees metadata, never content — state
  this in the terms). Hosted↔hosted sharing needs no protocol (same DB, org-to-org
  share). ⇒ **Matrix confirmed as transport**; the `Transport` seam stays for
  hygiene, the HTTPS fallback is no longer the likely path.
- Decisions: ship the `federation` compose profile (Synapse) **and** document
  bring-your-own homeserver. Order: 6a shared-client model + multi-tenant hosted +
  billing tiers → 5b Matrix transport.
- All 12 must-have controls remain (device pinning, agent-context exclusion for
  remote content, doc-id namespacing, size/rate caps, honest retract copy …).
