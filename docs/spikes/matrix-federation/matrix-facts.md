# Matrix Federation Design Spike — Fact Sheet

Research date: 2026-08-17. Current Matrix Client-Server spec version confirmed live at fetch time: **v1.19** (spec.matrix.org "latest" resolves to v1.19).

---

## 1. E2EE scope — state events vs. custom timeline events

State events can **never** be encrypted in current Matrix — there is no "encrypted state event" mechanism defined by the spec. The `m.room.encrypted` event type is defined only as `Event type: Message event` (it cannot carry a `state_key`), and the spec's Image Packs security-considerations section says outright: *"Encryption of image packs depends on encrypted state events, which are not currently defined by the Matrix specification."* So any custom **state** event type is always plaintext, fully readable by both homeservers.

Custom **timeline/message** event types (e.g. `de.buzzowl.client_card`) absolutely can be sent encrypted: the client sends the outer event as `type: "m.room.encrypted"`; the real `type` and `content` live inside the Megolm-encrypted `content.ciphertext`. Confirmed directly by spec text (Relationships API): *"Note that in encrypted rooms this will typically always be `m.room.encrypted` regardless of the event type contained within the encrypted payload."* The homeserver only ever indexes/sees `m.room.encrypted` + `algorithm`/`sender_key`/`device_id`/`session_id` (the last two deprecated-but-still-sent for Megolm) — never the real type or content.

Caveat: `m.relates_to` (used for edits/`m.replace`, threads, reactions) is explicitly required to be sent in the **cleartext** part of `content`, even in an encrypted event — see §10.

Confidence: high

- https://spec.matrix.org/latest/client-server-api/ (v1.19; see "End-to-End Encryption", "m.room.encrypted", "Image Packs" security considerations, "Relationships API")

## 2. Event size limits

The complete event (PDU, federation event format, including signatures, encoded as Canonical JSON) **MUST NOT exceed 65536 bytes**. This is a hard protocol limit and there is no carve-out or larger allowance for encrypted events — the base64 `ciphertext` string counts toward the same 65536-byte budget as everything else in the event.

Practical budget for a JSON "client card": subtract federation-envelope overhead (event_id, room_id, sender, auth_events/prev_events refs, hashes, signatures — typically several hundred bytes, more in a busy DAG) and then account for Megolm/AES + base64 expansion (~1.33x plus a small MAC/IV/version header) on whatever plaintext JSON remains. A safe practical ceiling is meaningfully **below** 64 KiB of plaintext JSON — plan for roughly 32–45 KiB, not the full 64 KiB assumed in the design brief. Anything larger should go through encrypted media (§3) instead of being inlined in the event content.

Confidence: high on the 65536-byte figure; medium on the exact "safe" practical number (reasoned estimate, not a spec-stated figure).

- https://spec.matrix.org/latest/client-server-api/ (Appendices / "Size limits")
- https://github.com/matrix-org/matrix-spec-proposals/issues/1021 (history of the size-limit clarification)

## 3. Encrypted attachments / media

Spec module "Sending encrypted attachments": client generates a single-use 256-bit AES key + 64-bit random IV (128-bit counter block), encrypts the file with AES-CTR, uploads the ciphertext through the normal `/upload` endpoint (gets an `mxc://` URI), and embeds an `EncryptedFile` object (`url`, `key` as a JWK with `alg: A256CTR`, `iv`, `hashes.sha256`, `v: "v2"`) in the event's `content.file` (replacing plaintext `content.url`). The homeserver stores/serves only ciphertext and can verify the SHA-256 hash, but never sees the key or plaintext.

matrix-nio supports this fully: uploads can be encrypted via `AsyncClient.upload(..., encrypt=True)`, and inbound encrypted media in encrypted rooms arrives as distinct classes `RoomEncryptedFile` / `RoomEncryptedImage` / `RoomEncryptedAudio` / `RoomEncryptedVideo` (all subclassing `RoomEncryptedMedia`) instead of the plaintext `RoomMessage*` classes. `nio.crypto.attachments.decrypt_attachment(ciphertext, key, hash, iv)` decrypts the downloaded bytes using the `key`/`hashes.sha256`/`iv` pulled from `event.source["content"]["file"]`.

Size limit: Synapse's `max_upload_size` config, **default `"50M"` (50 MiB)** — independent of, and much larger than, the 65536-byte inline-event cap, since the file body is stored as separate media content referenced by `mxc://`, not inlined in the event.

Confidence: high

- https://spec.matrix.org/latest/client-server-api/ ("Sending encrypted attachments", `EncryptedFile` schema)
- https://matrix-nio.readthedocs.io/en/latest/nio.html (crypto.attachments, RoomEncryptedMedia classes)
- https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html (`max_upload_size`, defaults to `"50M"`)

## 4. matrix-nio E2EE requirements in 2025/2026

Major recent development: **matrix-nio 0.26.0, released 2026-07-23**, replaced `libolm`/`python-olm` with **vodozemac** (PR #555, "Replace olm with vodozemac"). Verified directly against PyPI package metadata and the `main`-branch `pyproject.toml`: the `[e2e]` extra now requires `vodozemac>=0.9.0.post2` instead of `python-olm`. CHANGELOG: *"The `e2e` extra now depends on `vodozemac` instead of `python-olm`, and `libolm` is no longer required as a system dependency."* Because `vodozemac` ships as prebuilt Python wheels (Rust + PyO3, PyPI package `vodozemac` v0.10.0, also released 2026-07-23), `pip install "matrix-nio[e2e]"` on 0.26.0+ **no longer needs the libolm C library or `apt-get install libolm-dev`** — a real Docker-base-image simplification versus every existing nio tutorial.

Caveat / doc lag: matrix-nio's own `README.md` on the `main` branch (as fetched during this research) still describes the old libolm/`python-olm` install instructions — stale documentation not yet updated for 0.26.0; the authoritative source is `pyproject.toml` + `CHANGELOG.md` + PyPI's `requires_dist`, all of which agree on the vodozemac switch.

Migration note: existing libolm-pickled encryption stores auto-migrate on load; stores using a pickle format older than version 4 (roughly pre-December-2021) need `python-olm>=3.2.7` present *only* at the moment of upgrade.

Maintenance status: real but bursty. Prior release **0.25.2 was 2024-10-04**; the next release, **0.26.0, was 2026-07-23** — a ~21-month gap with zero releases, ended specifically to land this migration (the community ask, issue #518, was filed 2024-08-26 — it took ~23 months to land). Treat matrix-nio as "recently revived and currently active," not "continuously, steadily maintained."

Upstream libolm itself: gitlab.matrix.org's official README now opens with *"IMPORTANT: libolm is now deprecated,"* citing CVE-2021-34813 and CVE-2021-44538, superseded by vodozemac (Rust rewrite, audited by Least Authority, May 2022); the Matrix Foundation says it will "continue to support and maintain libolm for now... until the majority of folks have switched to vodozemac."

Confidence: high

- https://pypi.org/project/matrix-nio/ (v0.26.0, uploaded 2026-07-23T10:10:52Z)
- https://raw.githubusercontent.com/matrix-nio/matrix-nio/main/CHANGELOG.md
- https://raw.githubusercontent.com/matrix-nio/matrix-nio/main/pyproject.toml
- https://github.com/matrix-nio/matrix-nio/issues/518 (libolm deprecation tracking issue, opened 2024-08-26)
- https://github.com/matrix-nio/matrix-nio/pull/555 ("Replace olm with vodozemac")
- https://gitlab.matrix.org/matrix-org/olm/-/blob/master/README.md (official deprecation notice)
- https://pypi.org/project/vodozemac/ (v0.10.0, 2026-07-23)

## 5. Alternatives to matrix-nio for a Python E2EE bot

**mautrix-python** — actively maintained (v0.21.1, 2026-07-05, per GitHub tags; regular releases through 2025–2026), the framework underlying most mautrix bridges (WhatsApp, Signal, etc.). Offers a higher-level asyncio framework (`mautrix.client`, `mautrix.crypto`) than nio's sans-I/O style. As of this check its `mautrix.crypto` module still runs on `python-olm`/libolm (no evidence found of a vodozemac migration), so it still needs the libolm system library at present — on that specific axis nio 0.26+ is currently ahead. Maturity: high (battle-tested in production bridges handling large device fan-out), but a heavier, more opinionated dependency than a plain client library.

**matrix-rust-sdk Python bindings** — not a realistic option today. The old generic multi-language bindings repo, `matrix-org/matrix-rust-sdk-bindings`, has been **archived since 2022**. Current binding infrastructure (`matrix-sdk-ffi` via UniFFI, inside `matrix-rust-sdk/bindings/`) only ships prebuilt targets for Swift (`apple/`) and, separately, Kotlin/Android for Element X — there is no official Python package (`matrix-sdk-ffi` does not exist on PyPI, verified directly). There's active tooling work on generic UniFFI Python-wheel generation in the PyO3/maturin project (a multi-crate UniFFI test case landed ~Feb 2026), but that's build-tooling infrastructure, not a shippable, pip-installable Matrix client for Buzzowl to consume. Building on it today would mean rolling your own bindings — a real Rust/PyO3 engineering project, not a dependency add. Maturity: low for direct consumption.

**vodozemac (Python bindings)** — worth naming as a building block, not a client alternative: PyPI package `vodozemac` (v0.10.0, 2026-07-23) exposes only the low-level Olm/Megolm cryptographic primitives (what matrix-nio 0.26+ now wraps internally); it has no room/sync/HTTP layer, so it isn't a substitute for nio/mautrix by itself.

Confidence: medium-high (nio/mautrix status well sourced; matrix-rust-sdk-python assessment is based on confirmed repo-archival + PyPI absence, reasonably confident but bindings landscape moves fast).

- https://github.com/mautrix/python (tags: v0.21.1 @ 2026-07-05)
- https://pypi.org/project/mautrix/
- https://github.com/matrix-org/matrix-rust-sdk-bindings (archived: true, last pushed 2022-07-04)
- https://github.com/matrix-org/matrix-rust-sdk (active: last pushed 2026-08-17, 2244 stars)
- https://github.com/PyO3/maturin/pull/2208 (UniFFI multi-crate bindings work)
- https://matrix.org/ecosystem/sdks/ (Python SDK listing: mautrix-python and matrix-nio both marked "Stable")

## 6. Synapse dev setup in Docker

**Image**: official images exist at both `hub.docker.com/r/matrixdotorg/synapse` and `ghcr.io/element-hq/synapse` per the current install docs. A GitHub issue (element-hq/synapse#18329, opened ~April 2025) proposes deprecating the `matrixdotorg` Docker Hub namespace in favor of `ghcr.io/element-hq/synapse` as the canonical go-forward image (rationale: Element shouldn't publish under the separate matrix.org org namespace). Recommend defaulting new compose files to `ghcr.io/element-hq/synapse`.

**Generate config**: `docker run -it --rm --mount type=volume,src=synapse-data,dst=/data -e SYNAPSE_SERVER_NAME=my.matrix.host -e SYNAPSE_REPORT_STATS=yes ghcr.io/element-hq/synapse:latest generate` writes `homeserver.yaml` + a signing key into `/data`. `SYNAPSE_SERVER_NAME` and `SYNAPSE_REPORT_STATS` are the two mandatory env vars.

**Enable registration for dev**: two supported paths — (a) `enable_registration: true` + `enable_registration_without_verification: true` opens self-registration with no email/captcha check (Synapse's own docs call this "not recommended" outside dev, a known spam/abuse vector); (b) set `registration_shared_secret` and use the bundled `register_new_matrix_user` CLI script (or the admin API) to mint accounts out-of-band while keeping public registration closed — better for CI/dev since it never exposes an open registration endpoint.

**Two homeservers, one Docker network, federation**: real Matrix federation (Server-Server API) is TLS-only by protocol design — there is no config path for plain-HTTP federation. `federation_verify_certificates: false` and `federation_custom_ca_list` only relax/replace *certificate verification* (self-signed certs, a private CA) — TLS transport itself stays mandatory (`federation_client_minimum_tls_version` further confirms Synapse always negotiates a TLS version for outbound federation, default minimum "1"). Practical dev pattern: distinct `server_name`s, TLS certs on each container (self-signed, sharing a CA baked into both containers' `federation_custom_ca_list`, or per-container certs + `federation_verify_certificates: false`), and reachability on 8448 (or `.well-known/matrix/server` delegation) between containers on the shared Docker network. This is exactly the pattern Matrix's own "Complement" federation test-suite automates — worth reusing that approach rather than hand-rolling certs.

Confidence: high on the config options/behavior; medium on "no plain-HTTP federation path exists at all" (inferred from the absence of any such switch across the full config-reference text plus long-standing community consensus, not one single explicit spec sentence).

- https://element-hq.github.io/synapse/latest/setup/installation.html
- https://github.com/element-hq/synapse/blob/develop/docker/README.md
- https://github.com/element-hq/synapse/issues/18329 (matrixdotorg Docker Hub deprecation proposal)
- https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html (`federation_verify_certificates`, `federation_custom_ca_list`, `federation_client_minimum_tls_version`, `enable_registration_without_verification`, `registration_shared_secret`)
- https://matrix-org.github.io/synapse/develop/delegate.html (well-known delegation)

## 7. Device verification and trust in bot-to-bot E2EE

nio's `AsyncClient.room_send(room_id, message_type, content, tx_id=None, ignore_unverified_devices=False)` — default `False`: if the room contains devices nio doesn't consider verified, `room_send` (and the underlying `share_group_session`) will raise rather than send. Passing `ignore_unverified_devices=True` lets it proceed — unverified devices still **receive** the Megolm room key and can decrypt, the flag only bypasses the "refuse until verified" guardrail (nio's equivalent of Element's "never send to unverified sessions," but with an inverted default: nio ships closed, you opt into permissive).

Design implication for a one-bot-per-org-install model: since each org runs a single bot account/device, there's normally only one device per side to verify (unlike human multi-device fan-out), but nio still won't send at all without either completing verification (SAS/emoji, which nio supports) or setting `ignore_unverified_devices=True` — the design needs to pick one: fold a verification handshake into the invite-accept flow, or standardize on `ignore_unverified_devices=True` and accept that "authenticity" reduces to "is this the expected Matrix user ID," not "is this cryptographically the same device as last time."

**Cross-signing is explicitly not supported by nio** — its own README feature table lists "❌ cross-signing support" and "❌ server-side key backups," only manual + emoji (SAS) per-device verification (and no built-in in-room emoji-verification UX). Consequence: rotating a bot's device/session (e.g., redeploying it) has no cross-signing trust delegation to fall back on — every org-pair relationship needs re-verification (or a standing `ignore_unverified_devices=True` policy) after such a rotation.

Confidence: high

- https://matrix-nio.readthedocs.io/en/latest/nio.html (`Api.room_send`, `AsyncClient.share_group_session` signatures and docstrings)
- https://github.com/matrix-nio/matrix-nio (README feature checklist: cross-signing ❌, key backups ❌)

## 8. Room membership as consent

Accepting an invite = `POST /_matrix/client/v3/join`; from then on the client is allowed to see "all current state events... and all subsequent events... until the user leaves the room."

`m.room.history_visibility` (state event, empty `state_key`) values: `invited | joined | shared | world_readable`. **Default when unset is `shared`** — the spec explicitly flags this as a security consideration: *"Clients need to be aware that by not setting this event they are exposing all of their room history to anyone in the room"* (i.e. to anyone who later joins). For a 2-member org-pair room, explicitly set `history_visibility: invited` or `joined` at room creation rather than relying on the `shared` default.

"Leaving loses future access only" is true in a narrow, important sense: Matrix's Megolm has **no backward secrecy**. Once a room key has been distributed to a device (an `m.room_key` to-device event sent whenever that device is present for a session), that device can decrypt every message encrypted under that same session forever — leaving the room stops the *sender* from including that device in future key distribution and stops the *leaver's* client from seeing new timeline events (server-side), but it does **not** retroactively revoke keys already handed out. This is a cryptographic limitation, not a policy switch.

Redaction (`PUT /_matrix/client/v3/rooms/{roomId}/redact/{eventId}/{txnId}`, materializes as `m.room.redaction`): *"Strips all information out of an event which isn't critical to the integrity of the server-side representation of the room. This cannot be undone."* For an `m.room.encrypted` event, redaction removes the ciphertext from the **server's** copy, so a re-syncing client or a homeserver operator can no longer retrieve it from the server. It has **zero** effect on any client that already downloaded and decrypted the event before redaction — there is no remote wipe. "Unsharing" a client card via redaction can only mean "stop future/late retrieval from the server," never "claw back what a device already has."

Confidence: high

- https://spec.matrix.org/latest/client-server-api/ ("Room membership", "m.room.history_visibility", "Redactions")

## 9. Retention

Synapse supports an (its own, non-spec) message retention feature: a server-level `retention:` config block (`allowed_lifetime_min`/`allowed_lifetime_max`) plus an optional room-level `m.room.retention` state event (`max_lifetime`, in ms) that room admins/mods can set within the server's allowed bounds. Synapse's own docs describe this as **experimental** and explicitly note it is **not (yet) part of the Matrix spec** (it implements the semantics of MSC1763, which hasn't landed in the C-S spec). Retention **does not apply to state events**. Purging is a same-homeserver background job (DB compaction) — there's no protocol mechanism forcing a different homeserver (i.e., the *other* org's install) to honor or even be aware of your retention policy; it only governs your own server's copy of the room.

Confidence: medium (feature and defaults well documented; did not independently confirm whether it has been promoted beyond "experimental" status more recently than the current docs page, which still labels it experimental).

- https://element-hq.github.io/synapse/latest/message_retention_policies.html
- https://github.com/matrix-org/synapse/blob/master/docs/message_retention_policies.md

## 10. Useful spec/Synapse features for this design

`m.relates_to` + `rel_type: m.replace` is the right primitive for "update the client card" (spec sections "Event replacements" / "Editing encrypted events"). It works in encrypted rooms: `m.new_content` lives inside the encrypted payload — but the `m.relates_to` block itself (`rel_type` + target `event_id`) **must** be sent in the cleartext `content.m.relates_to` of the outer `m.room.encrypted` event (spec: *"As with all event relationships, the `m.relates_to` property must be sent in the unencrypted (cleartext) part of the event"*), because the server needs it for server-side aggregation (`GET /rooms/{roomId}/relations/{eventId}`). So the server can see "event X was edited/updated by event Y" and the update timeline shape, even though it can't see what changed.

Threads (`m.thread`) and reactions/annotations (`m.annotation`, e.g. `m.reaction`) follow the same pattern: real content encrypted, `m.relates_to` cleartext, with server-side aggregation available via the Relationships API — which explicitly documents that the queried `eventType` "will typically always be `m.room.encrypted` regardless of the event type contained within the encrypted payload."

A small unencrypted state-event "index" (e.g. `de.buzzowl.shared_clients` listing shared client IDs) is a reasonable pattern for cheap, always-current, rejoin-safe room bookkeeping (`GET /state`, last-write-wins per `state_key`, no timeline replay needed) — but per §1, state events can never be encrypted, so it is plaintext to **both** homeservers by construction. It must carry only opaque IDs, never company/contact names, and the design should explicitly decide whether leaking room-membership/count metadata (even as opaque IDs) to both homeserver operators is acceptable.

Confidence: high

- https://spec.matrix.org/latest/client-server-api/ ("Forming relationships between events", "Event replacements", "Editing encrypted events", "Threading", "Relationships API")

---

## Surprises / design implications

- **State events can never be encrypted** — any "index of shared client IDs" modeled as a state event is inherently plaintext to both homeservers; only the timeline/message-type custom events wrapped in `m.room.encrypted` get real Megolm confidentiality.
- **`m.relates_to` always travels in cleartext**, even inside E2EE rooms — so both homeservers can see the shape of the relationship graph (which event updates which card, thread structure, reaction counts) even though they can't read the content itself. Card-update and finding graphs leak metadata by design.
- **The 65536-byte event cap is not relaxed for encrypted events**, and Megolm/base64 overhead + federation-envelope overhead eats meaningfully into it — the design's "≤64 KiB" card budget is optimistic; plan for ~32–45 KiB of actual plaintext JSON, with anything bigger routed through encrypted media instead of inlined.
- **No backward secrecy**: leaving a room, redacting an event, or any other "unshare" action can only stop *future* delivery — it can never retroactively revoke a card/finding from a device that already decrypted it. Product copy around "revoke access" needs to be honest about this limitation.
- **matrix-nio just dropped its libolm dependency** (v0.26.0, 2026-07-23, replaced by vodozemac) — this removes the classic `apt-get install libolm-dev` Docker pain point that most existing tutorials still describe, but the project had a ~21-month release gap before this landed, so it's "recently revived," not continuously active — worth a bus-factor/support-risk note, and worth double-checking nio's README (which is currently stale on this exact point) against `pyproject.toml`/`CHANGELOG.md` before trusting install docs.
- **No cross-signing in matrix-nio**: rotating a bot's device (e.g., a redeploy) has no trust-delegation fallback — every org-pair relationship needs re-verification, or the design must standardize on `ignore_unverified_devices=True` and accept a weaker "who am I talking to" guarantee.
- **True federation cannot run over plain HTTP**, even in a same-Docker-network dev setup — TLS (self-signed certs + a shared custom CA, or `federation_verify_certificates: false`) is required regardless of network trust, so a same-host federation dev/POC needs real cert plumbing (Matrix's own "Complement" test suite is a good template) rather than a quick HTTP shortcut.
- **Retention is a per-homeserver housekeeping feature, not a cross-org control**: it's Synapse-specific, explicitly experimental/pre-spec, doesn't cover state events, and the sending org has no way to compel the receiving org's homeserver to purge anything — "retention" as a privacy promise only ever applies to your own install.
