# Offering Buzzowl as a hosted product (control plane + tenants)

Buzzowl itself is a long-running stack (FastAPI, PostgreSQL/pgvector, research
workers, heartbeats, WebSockets, the Pi agent service, SearXNG, Camofox, the
Matrix federation loop). It runs wherever containers run for weeks: a VM,
Hetzner/DigitalOcean, Fly.io, Railway, Render, ECS — **not** on Netlify or
Vercel (serverless functions cannot host it).

What *does* belong on Vercel/Netlify is the **control plane**: the website,
pricing, checkout, the customer portal and login. That part is a separate,
private codebase (billing and provisioning logic are not part of the AGPL
product). Buzzowl exposes small, billing-agnostic hooks for it — described here.

```
                 ┌──────────────────────────────┐        ┌────────────────────────────────────┐
  visitor ─────▶ │  control plane (private repo)│        │  Buzzowl tenant host (this repo)   │
                 │  Next.js on Vercel            │        │  docker compose, multi-tenant       │
                 │  Supabase Auth (users)        │        │  hosted.enforce_plans: true          │
                 │  Stripe (checkout, portal,    │  ───▶  │  /api/operator/*  (X-Operator-Key)   │
                 │          webhooks)            │        │  /api/auth/external (JWT exchange)   │
                 │  customers/subscriptions      │        │  plans + usage metering (6a)         │
                 └──────────────────────────────┘        └────────────────────────────────────┘
```

## Hooks in Buzzowl (this repo)

### Operator API — `X-Operator-Key`

Set `hosted.operator_key` in `config.yaml` (or env `HOSTED_OPERATOR_KEY`). Without a
key the API is disabled (401 for everything).

| Call | Purpose |
|---|---|
| `POST /api/operator/orgs` `{name, admin_email, admin_name?, plan?, llm_budget_usd_per_month?, external_ref?}` | provision a tenant: org + admin user + plan; returns `login_token` and `login_path` (`/login#token=…`) |
| `GET /api/operator/orgs`, `GET /api/operator/orgs/{id}` | list / detail (plan, suspended, users, month cost, usage) |
| `POST /api/operator/orgs/{id}/plan` `{plan?, llm_budget_usd_per_month?, external_ref?}` | subscription changed |
| `POST /api/operator/orgs/{id}/suspend` `{reason?}` / `…/resume` | payment failed / recovered — suspended tenants are **read-only** (writes → 402), heartbeats and research tasks are skipped, data is kept |
| `POST /api/operator/orgs/{id}/login-token` `{email?, days?}` | SSO hand-off: 30-day session token for a user of the tenant |
| `GET /api/operator/orgs/{id}/usage?days=` | LLM usage (calls, tokens, est. cost, by model/day) — feed for metered billing |
| `DELETE /api/operator/orgs/{id}` `{"confirm": "<slug>"}` | hard delete |

`external_ref` is a free field for the control plane's own id (e.g. the Stripe
customer id).

### External login — `POST /api/auth/external {token}`

Configured under `auth.external` (Supabase Auth, Auth0, Keycloak, any OIDC issuer):
JWKS URL for RS256/ES256 or `hs256_secret` for legacy Supabase projects, issuer,
audience (`authenticated` for Supabase), claim paths for email / name / org.
Behaviour: verify → find the user by email (or by the `org_claim` workspace) → issue a
Buzzowl session token. Unknown people get a personal workspace on
`hosted.default_plan` when `auto_provision` is on. The login page accepts
`/login#token=<session token>` and drops the person into their workspace.

### Plans, budgets, metering (Phase 6a)

`orgs.settings.plan` (light | premium), `llm_budget_usd_per_month`, per-call rows
in `llm_usage_events`. With `hosted.enforce_plans: true` a light tenant without
its own LLM key is refused (no platform spend); a premium tenant over budget is
soft-blocked. See `plans.py`.

## Sequences the control plane implements

1. **Signup + purchase**: person signs up (Supabase) → picks a plan → Stripe
   Checkout → `checkout.session.completed` webhook → `POST /api/operator/orgs`
   (plan from the price, `external_ref = customer id`) → store `org_id` next to
   the customer → redirect the browser to `<tenant host>/login#token=<login_token>`.
2. **Return visits**: Supabase login on the portal → either
   `POST /api/auth/external {supabase jwt}` (Buzzowl verifies it) or
   `POST /api/operator/orgs/{id}/login-token` → redirect with the token.
3. **Subscription changes**: `customer.subscription.updated` → `POST …/plan`;
   `invoice.payment_failed` → `…/suspend`; `invoice.paid` → `…/resume`;
   `customer.subscription.deleted` → suspend (and delete after a grace period).
4. **Metered premium**: nightly job pulls `GET …/usage` and reports usage to a
   metered Stripe price, or simply sets `llm_budget_usd_per_month` from the tier.

## Environment for a hosted tenant host

```
llm_oauth_gray_flows: false  # never in a hosted deployment — see below
hosted:
  signup_enabled: false      # signups happen on the control plane
  enforce_plans: true
  operator_key: <long random>          # or env HOSTED_OPERATOR_KEY
  premium_monthly_budget_usd: 20
auth:
  external: { enabled: true, jwks_url: https://<project>.supabase.co/auth/v1/.well-known/jwks.json,
              issuer: https://<project>.supabase.co/auth/v1, audience: authenticated }
```
Env: `BUZZOWL_SECRET_KEY` (per-org LLM keys at rest), `HOSTED_OPERATOR_KEY`,
`AGENT_SERVICE_TOKEN`, platform LLM keys for premium.

## Security notes

- **Turn `llm_oauth_gray_flows` off.** Subscription logins (ChatGPT-Codex,
  Copilot) are not permitted for third-party apps without a vendor whitelist.
  On a personal install that risk is the operator's own; in a hosted product you
  would be running it on customers' accounts at scale. Hosted tenants use API
  keys: their own (light) or yours (premium).

- The operator key grants tenant-wide control — keep it in the control plane's
  server-side secrets only; rotate by changing the config and redeploying.
- **Rate limits.** `/api/operator/*` is capped at 30 requests/minute per client
  IP as a brute-force backstop on `X-Operator-Key` — comfortably above what a
  control plane needs, so batch reconciliation jobs should still stagger their
  calls. Every other endpoint falls back to an app-wide 300/minute per client IP
  and view function (`RATE_LIMIT_DEFAULT` to change it); `/api/health` and the
  agent service callbacks (`/api/internal/*`, `/api/agents/internal/*`,
  `/api/agents/callback`) are exempt.
- **Set `FORWARDED_ALLOW_IPS` when you terminate TLS elsewhere.** The limiter
  keys on the peer address, and uvicorn only trusts `X-Forwarded-For` from
  127.0.0.1 by default. Behind the cloudflared sidecar (or any proxy that is a
  separate container/host) every request looks like it comes from the proxy, so
  all tenants land in one bucket and throttle each other. Set the variable to
  the proxy's address — not `*`, since compose publishes port 8000 on the host.
- Login tokens are ordinary sessions (30 days); they travel in a URL fragment
  (`#token=`), which browsers do not send to servers.
- Suspension is enforced in `current_user` (writes) and in the schedulers; reads
  stay possible so customers can export their data.
