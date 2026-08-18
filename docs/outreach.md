# Supervised outreach

Agents draft emails. A human approves every one. A background worker sends
only what was approved, inside guardrails the org admin controls. Nothing
leaves the building without a person clicking Approve.

An outreach item is a `documents` row with `type = 'outreach'`. Its lifecycle
lives in `metadata.state`, and `outreach.py` is the only place that decides
which state transitions are legal and who may cause them — routers, the send
worker and the IMAP poller all call `outreach.transition()` rather than
writing `state` directly.

## The state machine

```
draft ──▶ pending_approval ──▶ approved ──▶ queued ──▶ sent ──▶ replied
  │              │                 │           │          └──▶ bounced
  │              └──▶ rejected     │           │          └──▶ followup_due
  └────────────────── cancelled ◀──┴───────────┘
                       (from any pre-sent state; rejected/cancelled → draft)
```

States (`outreach.py` `STATES`): `draft`, `pending_approval`, `approved`,
`queued`, `sent`, `replied`, `bounced`, `followup_due`, `rejected`,
`cancelled`.

Who may cause each transition (`actor` in `outreach._TRANSITIONS`):

| From | To | Actor |
|---|---|---|
| draft | pending_approval | human |
| pending_approval | approved | human (admin, or the sender) |
| pending_approval | rejected | human |
| pending_approval | draft | human ("send back for edits") |
| approved | pending_approval | human ("un-approve") |
| approved | queued | worker |
| queued | sent | worker |
| queued | approved | worker (send failed → back for retry) |
| sent | replied | IMAP or human |
| sent | bounced | IMAP or worker |
| sent | followup_due | worker or human |
| followup_due | replied | IMAP or human |
| followup_due | sent | human |
| draft / pending_approval / approved / queued | cancelled | human |
| rejected / cancelled | draft | human |

The hard rules this encodes:

- **The agent can only create drafts.** The Pi `draft_outreach` tool writes a
  `draft` document and stops; it never calls `transition()`. Drafting also
  requires org autonomy level 3 ("act + draft outreach", `autonomy.py`
  `LEVEL_OUTREACH`) — below that level the agent cannot draft at all.
- **Only a human moves `draft → pending_approval → approved`.** Approving
  needs the `admin` role or to be the original sender
  (`routers/outreach.py::transition_outreach`).
- **Only the send worker moves `approved → queued → sent`.** It claims one
  approved item at a time with a row lock (`db.claim_next_approved_outreach`),
  which is also the point where the item flips to `queued`.
- **Only IMAP ingestion, or a human, sets `replied`.** Only IMAP or the
  worker sets `bounced` (a worker-detected SMTP hard bounce also counts).

`followup_due` is defined in the state machine but nothing in this codebase
sets it automatically — no scheduled job, no button in the `/outreach` UI.
It's reachable only by calling the transition API by hand. Treat it as
reserved, not implemented.

## Setup

Outreach reuses one org-wide SMTP account for everyone — there are no
per-rep SMTP credentials. Set these in `.env` (read by `docker-compose.yml`
into the `server` container):

```bash
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
SMTP_FROM=
SMTP_FROM_NAME=Buzzowl
```

Worked example for Gmail (an App Password, not your login password — Google
shows it as `xxxx xxxx xxxx xxxx`; `mailer.py` strips the spaces for you):

```bash
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=sales@example.com
SMTP_PASS=abcd efgh ijkl mnop
SMTP_FROM=sales@example.com
SMTP_FROM_NAME=Acme Sales
```

Restart after changing `.env`: `docker compose up -d server`. Outreach is
disabled by default even with SMTP configured — see Guardrails below.

**Sender identity.** `mailer.build_message()` sets `From` to
`"<rep display name> via <SMTP_FROM_NAME>" <SMTP_FROM>` — always the one
org mailbox, so SPF/DKIM keep matching your domain — and `Reply-To` to the
rep's own address, so replies land in their inbox instead of the shared one.

The rep's display name and reply-to come from `db.get_user_identity()`,
which reads `users.settings` keys `outreach_display_name`,
`outreach_reply_to` and `outreach_signature`, falling back to the user's
account display name / login email / no signature when those keys are
unset.

Each rep sets their own three values in **Settings → Your outreach
identity**. Empty fields fall back to the account values (display name, login
email, no signature). The same data is available over the API:

```bash
curl -s http://localhost:8000/api/auth/identity -H "Authorization: Bearer $TOKEN"

curl -s -X PATCH http://localhost:8000/api/auth/identity \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"display_name":"Anna Weber","reply_to":"anna@example.com","signature":"Anna Weber\nAccount Executive"}'
```

An admin can additionally change a user's login email
(`PATCH /api/users/{id}`), which moves the reply-to fallback with it.

## The approval queue

`/outreach` in the web UI is the queue view (`static/outreach.html`, backed
by `GET /api/outreach/items`).

- **Filter by state** with the tabs across the top: To approve (default),
  Drafts, Approved, Sending, Sent, Replied, Follow-up due, Bounced,
  Rejected, Cancelled. "mine only" restricts to items where you're the sender.
- **Edit before approving.** While an item is `draft`, `pending_approval` or
  `approved` its Edit button is live — subject, recipient email and body
  (`PATCH /api/outreach/items/{id}`). Editing an `approved` item does not
  un-approve it.
- **Approve** moves `pending_approval → approved` and queues it for the
  worker, with a confirm dialog. Only an admin or the original sender can do
  this.
- **Reject** moves `pending_approval → rejected` and prompts for an optional
  reason, stored in the transition history.
- Every card shows its last 3 history entries and, if the worker held it
  back, the `last_error` reason inline.

Creating a draft by hand: the "+ New draft" form posts to
`POST /api/outreach/items` with `client`, `to_email`, `to_contact`,
`subject`, `body`. The client name must already exist in Buzzowl.

## Guardrails

All guardrails are org-wide settings (`autonomy.DEFAULT_SETTINGS`), read
and enforced in `routers/outreach.py::_guardrails()` on every send attempt,
and editable by an admin from Settings → outreach (`POST /api/org/settings`,
`static/settings.html`).

| Guardrail | Setting key | Default | Where configured |
|---|---|---|---|
| Master switch | `outreach_enabled` | `false` | Settings page "Enable outreach sending" checkbox |
| Emergency stop | `outreach_kill_switch` | `false` | Settings page "kill switch" checkbox |
| Daily send cap | `outreach_max_per_day` | `25` | Settings page, org-wide, per UTC calendar day |
| Per-contact frequency floor | `outreach_contact_floor_days` | `7` | Settings page, minimum days between two mails to the same address |
| Quiet hours | `outreach_quiet_hours` | `[20, 7]` | Settings page, `[start_hour, end_hour)` UTC — sending is held inside this window |

Outreach ships **off by default** — `outreach_enabled` starts `false` even
if SMTP is fully configured, so a fresh install can't send until an admin
turns it on deliberately. Note `kill_switch` (no `outreach_` prefix) is a
separate, broader autonomy kill switch for all agent action; the
outreach-specific one is `outreach_kill_switch` — don't confuse the two in
`/api/org/settings`.

When a guardrail blocks a send, the send worker doesn't drop the mail: it
moves the item back from `queued` to `approved`, writes the reason to
`metadata.last_error` (logged and shown inline on the card in
`/outreach`), and retries it on a later worker tick once the guardrail
clears — e.g. once quiet hours end or the next UTC day resets the cap. It
stops being retried only if you cancel it or the settings change.

Check the live guardrail status (also shown top-right on `/outreach`):

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/outreach/items/guardrails | python3 -m json.tool
```

## Replies and bounces (IMAP)

The IMAP poller (`imap_sync.py`) is pure ingestion — it never sends and
never touches drafts. It reads one mailbox and moves matching outreach
documents `sent → replied` or `sent → bounced`. It runs every 5 minutes
(APScheduler job `imap_sync`, `routers/pipeline.py`).

Configure it in `.env` (passed into the `server` container by
`docker-compose.yml`):

```bash
IMAP_HOST=
IMAP_PORT=993
IMAP_USER=
IMAP_PASS=
IMAP_FOLDER=INBOX
```

Leave `IMAP_HOST`/`IMAP_USER` empty and the poller no-ops cleanly — replies
just never get auto-detected; you can still mark an item `replied` by hand
from the queue.

**Matching.** Every send carries a generated `Message-ID`. A reply counts
when its `In-Reply-To`/`References` header contains one of those IDs *and*
the ID's domain matches your SMTP-From domain (or
`outreach_message_id_domains` in config, or `buzzowl.local`) — this stops
the poller matching an unrelated thread. A **bounce** is a delivery-status
notification (`multipart/report; report-type=delivery-status`, or From
`mailer-daemon`/`postmaster`, or a subject like "Undeliverable"/"Mail
delivery failed") whose quoted headers or body mention one of your
Message-IDs. Only `UNSEEN` messages are fetched, read-only, so the poller
never marks mail as read in your mailbox.

## Testing it safely

Point Buzzowl at a local SMTP catcher instead of a real mail server before
you touch a live inbox.

**aiosmtpd** (ships with Python) — `pip install aiosmtpd && python -m aiosmtpd -n -l localhost:1025`.

**or MailHog** (web UI at `localhost:8025` to read the caught mail):

```bash
docker run -d -p 1025:1025 -p 8025:8025 mailhog/mailhog
```

Either way, point the server's SMTP env at it. Because the catcher runs on
your host and `server` is on the compose network, use
`host.docker.internal` (already wired via `extra_hosts` in
`docker-compose.yml`), not `localhost`, then restart:

```bash
SMTP_HOST=host.docker.internal
SMTP_PORT=1025
SMTP_USER=
SMTP_PASS=
SMTP_FROM=test@buzzowl.local
SMTP_FROM_NAME=Buzzowl Test
```

```bash
docker compose up -d server
```

Create a draft addressed to your own email, approve it, then either wait up
to a minute for the send worker or trigger it immediately as an admin:

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/outreach/items/worker/tick | python3 -m json.tool
```

Confirm from the database — outreach items are `documents` rows with
`type = 'outreach'`:

```bash
docker compose exec -T db psql -U whisper -d whisper -c "
  SELECT id, title, metadata->>'state' AS state, metadata->>'to_email' AS to_email,
         metadata->>'sent_at' AS sent_at, metadata->>'last_error' AS last_error
    FROM documents
   WHERE type = 'outreach'
   ORDER BY id DESC
   LIMIT 10;"
```

With MailHog, also check `http://localhost:8025` — the caught message shows
the real `From`/`Reply-To`/`Message-ID` headers as they'll go out in
production.

Once it looks right in the catcher, switch `SMTP_*` back to your real
provider and send to yourself once more before approving anything for an
actual contact.

## Troubleshooting

**Nothing sends.**
1. `GET /api/outreach/items/guardrails` — `ok: false` names the exact reason
   (kill switch, `outreach_enabled=false`, SMTP not configured, daily cap,
   quiet hours, or contact floor).
2. Confirm the item is actually `approved` — `draft`/`pending_approval` are
   never picked up by the worker.
3. Check `metadata.last_error` on the card — a held item goes back to
   `approved` with the reason attached, it isn't silently dropped.
4. Confirm SMTP env reached the container: `docker compose exec server env | grep SMTP_`.

**Mail lands in spam.** Buzzowl doesn't manage SPF/DKIM/DMARC — that's your
mail provider and DNS, not this app. If sends are flagged, verify SPF/DKIM
are published for `SMTP_FROM`'s domain and that `SMTP_FROM_NAME` isn't
spoofing a domain you don't control.

**Replies aren't detected.**
1. Confirm IMAP env reached the container: `docker compose exec server env | grep IMAP_`.
2. Give it up to 5 minutes — that's the poll interval.
3. The reply must quote the thread (`In-Reply-To`/`References`) — a
   brand-new message with no threading headers won't match.
4. If `SMTP_FROM`'s domain changed since the mail was sent, `_our_domains()`
   may no longer include the domain the original Message-ID used — old
   sends won't match retroactively.
5. Fallback: mark it `replied` by hand from `/outreach` — IMAP is a
   convenience, not the only path to that state.
