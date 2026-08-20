# Security Policy

Buzzowl is maintained by a single person. Security reports get a best-effort
response — there is no bug bounty and no SLA, but genuine reports are taken
seriously and fixed as quickly as possible.

## Reporting a vulnerability

Please report privately through GitHub's built-in vulnerability reporting:
**Security tab → "Report a vulnerability"** on this repository.

If you would rather not use GitHub, mail **security@buzzowl.app** — plain
text is fine, and encrypted mail is welcome if you have a key you prefer.

> Note for the maintainer: private vulnerability reporting must be turned on
> in repo Settings → Security → "Private vulnerability reporting" before this
> works — do this before the repo goes public.

Please do not open a public issue for a suspected vulnerability. Expect a first
reply within a few days; if you hear nothing after a week, ping again — mail gets
lost, silence is not a policy.

## Scope

In scope:

- Authentication bypass (login, session/token handling, invite keys)
- Data crossing an org/tenant boundary — anything that lets one org read or
  write another org's clients, documents, contacts, deals, or settings
- The internal agent API (`routers/internal.py`, called by the agent
  service) — its fail-closed token check, and anything reachable through it
- The operator API (`routers/operator.py`, the control-plane hook) — its
  `X-Operator-Key` check and tenant lifecycle operations (create/suspend/
  delete/login-token)
- Per-org LLM key handling — storage, encryption at rest, and any path that
  could leak a key across orgs or into logs
- The Matrix federation transport (shared clients between installs) — replay,
  spoofing, or decryption issues in that sync path
- Server-side request forgery (SSRF) via page fetching (browser-service,
  camofox, or any agent tool that fetches an arbitrary URL)

Out of scope:

- Findings that require an already-compromised host or container
- Self-XSS (an attacker convincing themselves to run something in their own
  browser session)
- The default `docker-compose.yml` dev database credentials
  (`whisper`/`whisper`) — these are intentionally public and the DB port is
  not published to the host by default, so this is not treated as a finding
  on its own
- Denial of service caused by giving an agent an expensive/absurd task
  (resource exhaustion by design of the feature, not a flaw)

If you're not sure whether something is in scope, report it anyway and we'll
sort it out.

## Hardening checklist for operators

Self-hosting Buzzowl? Before exposing an instance beyond your own machine:

- **Set `AGENT_SERVICE_TOKEN`** in `.env` (`openssl rand -hex 32`) — the
  internal agent API is fail-closed and disabled without it.
- **Set `BUZZOWL_SECRET_KEY`** in `.env` if you use per-org LLM keys (hosted/
  multi-org setups) — without it, key encryption at rest falls back to
  `AGENT_SERVICE_TOKEN`.
- **Do not publish the database port.** It's commented out in
  `docker-compose.yml` by default (`db` has no `ports:` mapping) — leave it
  that way; only uncomment it for host-side debugging on a trusted machine.
- **Put a reverse proxy with TLS in front of `server` (port 8000).** Compose
  publishes it in plaintext HTTP by default; terminate TLS in front of it for
  anything beyond localhost.
- **Keep `camofox` (9377) and `browser-service` (3001) off the public
  internet.** Both publish to all interfaces by default in
  `docker-compose.yml`. If your host itself is internet-facing, bind these to
  `127.0.0.1` or drop the port mappings — they don't need to be reachable
  from outside the compose network.
- **Rotate LLM API keys periodically**, and immediately if you suspect a leak
  — especially before making a previously private `.env` or backup public.

## Also see

`docs/hosting.md` for hosted/multi-tenant deployment notes and
`docs/federation.md` for the Matrix federation transport threat model.
