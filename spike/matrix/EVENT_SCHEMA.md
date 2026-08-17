# Buzzowl federation — event schema draft (Phase 5 spike, v0)

Status: **draft for the go/no-go report** — not implemented beyond the spike bot.
Measured constraints that shaped it (see `run.sh` output / proof.json):

| Fact (measured on Synapse latest, matrix-nio e2e) | Consequence |
|---|---|
| Timeline events with a custom `type` are Megolm-encrypted; the server stores `m.room.encrypted` + ciphertext only | All data goes in **timeline** events with custom types |
| **State events are plaintext** on the homeserver (confirmed via raw API) | Never put client data in state; state only for room plumbing |
| Event cap 65 536 bytes → 70 KiB card rejected `M_TOO_LARGE`; ciphertext ≈ 1.5× plaintext (848 B → 1 299 B) | Plaintext payload budget **≤ 40 KiB**; bigger content → encrypted media |
| Encrypted attachments (AES-CTR + SHA-256, `file` key) round-trip; 130 KB verified | Documents ride as encrypted media referenced from the card |
| `m.relates_to` / `m.replace` works for custom types | Card updates = replace events; receiver keeps latest per `card_id` |
| Sender sends to **unverified** devices (`verified: false` on receipt) | Device pinning/verification is a product requirement (threat model) |

## Envelope (inside the encrypted payload)

Every Buzzowl event carries these top-level keys next to the payload:

```jsonc
{
  "schema": 1,                       // integer, bump on breaking change; receiver rejects unknown majors
  "kind": "client_card",             // client_card | finding | document | retract | ack
  "card_id": "uuid-v5(buzzowl:<sender-instance>:client:<slug>)",   // stable per sender+client
  "sender_org": "Acme Sales GmbH",   // display name only — identity is the Matrix sender + pinned device key
  "shared_at": "2026-08-17T08:39:12Z",
  "share_scope": ["profile", "contacts", "findings_summary"],      // what the sender chose to include
  "provenance": { "instance": "acme-crm.example", "generated_by": "buzzowl/1.x" }
}
```

Receiver-side rules: unknown `kind` → ignore + log; unknown top-level keys → keep
(forward compatible); any string field is text (no HTML), length-capped, and stored
with `untrusted_remote: true` metadata.

## Event types

### `de.buzzowl.client_card` — the shareable client card (≤ 40 KiB plaintext)

```jsonc
{
  "…envelope…",
  "client": {
    "name": "Acme Corp",              // required
    "industry": "…", "website": "https://…", "location": "Berlin, DE",
    "summary": "…"                    // ≤ 2 000 chars, plain text
  },
  "contacts": [                       // only when scope includes "contacts"; ≤ 50
    { "name": "Jane Doe", "role": "CTO", "email": "jane@…", "linkedin": "https://…" }
  ],
  "findings_summary": [               // only when scope includes "findings_summary"; ≤ 50
    { "title": "…", "type": "pain_point|opportunity|risk|news", "date": "YYYY-MM-DD", "source_url": "https://…" }
  ],
  "documents": [                      // pointers to encrypted media sent in the same room (optional)
    { "doc_id": "…", "title": "Research: Acme Corp", "doc_type": "research", "mxc": "mxc://…", "sha256": "…", "bytes": 130039 }
  ]
}
```

**Never included** (hard-excluded on the sender, regardless of scope): private
notes, meeting transcripts, personal remarks, outreach drafts/mail bodies, deals,
agent reasoning, contact phone numbers unless the sender ticks "contacts:full".

**Update** = same type with `m.relates_to: {rel_type: "m.replace", event_id: <original>}`
and the full new card in `m.new_content` (spec-conformant). Receiver replaces its
`shared_external` document content and appends to the provenance history.
Note: `m.relates_to` is **cleartext by spec** even in E2EE rooms — homeservers learn
"event X replaced event Y" (never the content). Keep relation metadata free of any
client identifier; only opaque event ids.

### `de.buzzowl.document` — large content as encrypted media

Standard `m.room.message` / `msgtype: m.file` with the encrypted `file` block
(so any Matrix client can at least see "a file was shared") plus a `de.buzzowl`
extension `{kind: "document", card_id, doc_id, doc_type, sha256}` — exactly what the
spike sent. Receiver: download → decrypt → verify sha256 → store as
`documents.type='shared_external'` linked to the card via `metadata.card_id`.
Size cap for v1: 5 MB per document (Synapse default `max_upload_size` 50 MB).

### `de.buzzowl.finding` — incremental finding after the card (Phase 6+)

```jsonc
{ "…envelope with kind: finding…", "finding": { "title", "type", "date", "summary", "source_url" } }
```
Small, frequent; receiver appends to the card's shared timeline. Not needed for v1.

### `de.buzzowl.retract` — sender asks the receiver to delete

```jsonc
{ "…envelope with kind: retract…", "target": { "card_id": "…", "doc_ids": ["…"] }, "reason": "erasure_request|unshared|error" }
```
Sent when the sharing user flips the toggle off or when A receives an Art. 17/16
request (Art. 19 notification duty). Sender **also redacts** the original events
(`m.room.redaction`), which removes the ciphertext from both homeservers — but
the receiver's decrypted copy exists in B's database; retract is a *request* the
receiving Buzzowl honours automatically (delete `shared_external` rows for that
`card_id`, keep an audit stub "retracted by sender on <date>"). This limit is
documented to users honestly.

### `de.buzzowl.ack` (optional, v1.1) — receipt/decision feedback

`{ kind: "ack", target: {card_id, event_id}, decision: "received|linked|dismissed|deleted" }`
lets the sender see "partner linked your card" without any content flowing back.

## Room conventions

- One room per **org pair**, created by whoever connects first: `preset: private_chat`,
  `m.room.encryption` in `initial_state` (encrypted from the first event),
  `m.room.history_visibility: joined` (a bot that joins later must not read old
  cards), `m.room.join_rules: invite`, power levels: both bots 50, `invite: 100`
  (nobody can add a third member — a partner pair stays a pair; a third org = new room).
- Room name/topic are visible to homeservers → use neutral names
  (`Buzzowl share`), never client names.
- **No `de.buzzowl.*` state events** carrying data. Only room-plumbing state.
- Room upgrades (`m.room.tombstone`) → follow only if the successor was created by
  the pinned partner; otherwise alert the admin.

## Size + rate budgets (v1)

| Item | Cap | Why |
|---|---|---|
| Card plaintext | 40 KiB (hard) / 24 KiB (warn) | 64 KiB event cap incl. ~1.5× ciphertext overhead |
| Contacts per card | 50 | minimisation |
| Documents per card | 20, each ≤ 5 MB | media/storage |
| Sends per partner room | 60/h burst 100 (per install) | Synapse `rc_message` defaults would throttle anyway |
| Inbound accepted per hour | 500 (then queue) | DoS from a partner |

## Identity & keys (from the threat model — recorded here for the schema)

- Partner identity = Matrix user ID **and** the pinned Ed25519 device key of the
  partner bot; the first-connect UI shows both sides' fingerprints (SAS/emoji or hex)
  and the admin confirms out-of-band. After pinning: `ignore_unverified_devices=False`
  and refuse to send to new unverified devices → alert.
- Every inbound event stores `sender`, `sender_key`, `device verified` in provenance;
  events from unverified devices land in the review queue **flagged**, never trusted.
