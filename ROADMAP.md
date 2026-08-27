# Roadmap

Buzzowl is built by one person, so this is a set of directions rather than a
schedule — no dates, no version promises. What moves up the list is whatever
people actually hit when they run it, which is decided in
[Discussions](https://github.com/Lomikukus/buzzowl/discussions).

## Near term

**Prebuilt container images.** The first `docker compose up -d` still builds the
hardened browser image from its upstream source — around 2.5 GB and a few minutes
before anything starts. Multi-arch images (arm64 and x86_64) on GHCR would make
that first run a pull, and take the manual `CAMOFOX_ARCH` step off x86_64 hosts.

**Deeper supervised outreach.** Replies and bounces are detected over IMAP today,
and every send waits on a human clicking Approve — that part is not changing.
Missing is the sequence around it: `followup_due` exists in the state machine but
nothing drives it, so follow-up chains, timing and per-thread reply handling come
next ([docs/outreach.md](docs/outreach.md)).

**More bring-your-own-LLM providers.** OpenRouter can be connected with an OAuth
login instead of a pasted key. Same for every other provider that permits it, so
connecting a model never means copying secrets around.

## Mid term

**Cross-install sharing without the setup.** Two reps on two installs can already
share a client, synced end-to-end encrypted over Matrix
([docs/federation.md](docs/federation.md)) — but it needs a homeserver you run or
trust and a `--profile federation` start. The goal is that sharing is simply
there: invite-based, encrypted, no Matrix knowledge required.

**More of a CRM.** Deals with stage history, a pipeline board and CSV
import/export are in; the reporting around them is not — the Insights page covers
activity, feature usage and the outreach funnel, not pipeline or forecast. Import
mapping and bulk edits need the same attention.

**Smaller screens.** The UI assumes a desktop browser; a few pages have
breakpoints, most do not. Reading research and approving outreach from a phone
between meetings is the case worth fixing.

## Exploring

Not commitments — things I am looking at and would like opinions on.

- **A hosted Buzzowl.** The waitlist is open at
  [buzzowl.app](https://buzzowl.app/#hosted): Light = bring your own key,
  Premium = models included. The hooks a control plane needs already exist here
  ([docs/hosting.md](docs/hosting.md)).
- **A live in-meeting assistant.** Transcription is after-the-fact today;
  surfacing what is already known about a client *during* the call is a different
  problem, and a tempting one.
- **Official subscription sign-in.** The ChatGPT-Codex and Copilot logins work
  but sit in a ToS gray zone, which is why they ship with a warning. A sanctioned
  "sign in with your subscription" would replace them — that needs the vendors,
  not me.

## Not planned

- **Telemetry or phone-home.** Not now, not behind a flag
  ([docs/privacy.md](docs/privacy.md)).
- **Selling or brokering your data.** The point is that it stays on your server.
- **A crippled open core.** No per-seat licensing of the core, no features held
  back for a paid tier. The AGPL product is the complete product; a hosted plan
  would sell not having to run it yourself.

## Recently shipped

- **v0.1.0**, the first public release
  ([notes](https://github.com/Lomikukus/buzzowl/releases/tag/v0.1.0)).
- **A robustness pass** — healthchecks for server, agent service and search; the
  agent service fails closed without its token; an app-wide rate limit; a failed
  migration stops the boot; nightly pruning of run logs, knowledge untouched.
- **Cross-install client sharing** over Matrix, end-to-end encrypted, with a
  bundled Synapse profile.
- **Per-rep outreach identity** — display name, reply-to and signature per user,
  sent over the one org mailbox so SPF/DKIM keep matching.
- **A demo dataset** — `scripts/seed_demo.py` fills a fresh install with
  fictional clients, research, deals and an outreach draft.

Something you need that is not here, or an order that looks wrong to you?
[Open a Discussion](https://github.com/Lomikukus/buzzowl/discussions) — it is the
most direct way to change what gets built next.
