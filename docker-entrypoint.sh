#!/bin/sh
set -e

# Fail loud instead of silently 401ing. Server and agent-pi share one secret
# (AGENT_SERVICE_TOKEN); without it both are fail-closed and every internal
# call answers 401 ("Internal APIs disabled: agent_service_token is not
# configured"). See docs/troubleshooting.md for the full chain. Checked before
# the DB wait below -- it needs no DB, and a down DB would otherwise hide this
# error behind an unbounded "Waiting for database..." loop.
#
# The token may also be set via config.yaml's top-level agent_service_token
# (optionally overridden by config.local.yaml -- same untracked overlay
# context.py reads: CONFIG_LOCAL env, default "config.local.yaml", resolved
# relative to /app; local wins) instead of, or as well as, the env var.
# context.py and agent_service_ts/src/config.ts both honor it, with
# config.yaml (then its local overlay) winning over the environment when both
# are set. Check it here too so an operator who only set it in config.yaml is
# not stopped at boot. "|| true" matters: under `set -e`, a failing command
# substitution (yaml missing, unreadable, or invalid) would otherwise abort
# this whole script silently -- it must degrade to "no yaml token" instead.
EFFECTIVE_TOKEN=$(python3 - <<'PYEOF' 2>/dev/null
import os
import yaml


def load(path):
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


merged = load('/app/config.yaml')
local_path = os.path.join('/app', os.environ.get('CONFIG_LOCAL', 'config.local.yaml'))
merged.update(load(local_path))
print(merged.get('agent_service_token') or '')
PYEOF
) || true

if [ -z "${AGENT_SERVICE_TOKEN:-}" ] && [ -z "$EFFECTIVE_TOKEN" ] && [ "${ALLOW_INSECURE_INTERNAL:-}" != "1" ]; then
    echo ""
    echo "============================================================================"
    echo "ERROR: AGENT_SERVICE_TOKEN is not set -- refusing to start."
    echo ""
    echo "The server and agent-pi share this one secret. Without it both are"
    echo "fail-closed: every internal API call answers 401 (\"Internal APIs"
    echo "disabled: agent_service_token is not configured\")."
    echo ""
    echo "Fix (recommended): from the repo root, run:"
    echo "  ./scripts/init-env.sh"
    echo "  docker compose up -d"
    echo ""
    echo "Manual alternative -- run in YOUR OWN shell from the repo root (.env is"
    echo "read literally by Docker Compose, so paste real commands, not a value"
    echo "containing a literal \"\$(...)\"):"
    echo "  echo \"AGENT_SERVICE_TOKEN=\$(openssl rand -hex 32)\" >> .env"
    echo "  echo \"BUZZOWL_SECRET_KEY=\$(openssl rand -hex 32)\" >> .env"
    echo "(setting it in config.yaml instead of .env also works, but .env is"
    echo " recommended so agent-pi and server stay in sync)"
    echo ""
    echo "Then apply it with: docker compose up -d"
    echo "('docker compose restart' does NOT re-read .env -- the old value stays.)"
    echo ""
    echo "Local dev only, never on an instance anyone else can reach:"
    echo "  ALLOW_INSECURE_INTERNAL=1   # skips this check, serves APIs unauthenticated"
    echo "============================================================================"
    echo ""
    exit 1
fi

if [ -n "${AGENT_SERVICE_TOKEN:-}${EFFECTIVE_TOKEN}" ] && [ -z "${BUZZOWL_SECRET_KEY:-}" ]; then
    echo "WARNING: BUZZOWL_SECRET_KEY is not set -- stored workspace LLM keys are encrypted using AGENT_SERVICE_TOKEN instead; rotating the token later will make them unreadable (set BUZZOWL_SECRET_KEY explicitly: openssl rand -hex 32)."
fi

echo "Waiting for database..."
until pg_isready -d "${DATABASE_URL}" 2>/dev/null; do
    sleep 1
done
echo "Database ready."

# Apply schema on fresh installs only (orgs table as sentinel)
TABLE_EXISTS=$(psql "${DATABASE_URL}" -t -c \
    "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_schema='public' AND table_name='orgs');" \
    2>/dev/null | tr -d '[:space:]')

if [ "$TABLE_EXISTS" = "f" ]; then
    echo "Fresh database — applying schema..."
    psql "${DATABASE_URL}" -f /app/schema.sql
    echo "Schema applied."
else
    echo "Schema already applied — skipping."
fi

exec python server.py
