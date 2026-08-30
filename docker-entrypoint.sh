#!/bin/sh
set -e

echo "Waiting for database..."
until pg_isready -d "${DATABASE_URL}" 2>/dev/null; do
    sleep 1
done
echo "Database ready."

# Fail loud instead of silently 401ing. Server and agent-pi share one secret
# (AGENT_SERVICE_TOKEN); without it both are fail-closed and every internal
# call answers 401 ("Internal APIs disabled: agent_service_token is not
# configured"). See docs/troubleshooting.md for the full chain.
if [ -z "${AGENT_SERVICE_TOKEN:-}" ] && [ "${ALLOW_INSECURE_INTERNAL:-}" != "1" ] && [ "${ALLOW_INSECURE_INTERNAL:-}" != "true" ]; then
    echo ""
    echo "============================================================================"
    echo "ERROR: AGENT_SERVICE_TOKEN is not set -- refusing to start."
    echo ""
    echo "The server and agent-pi share this one secret. Without it both are"
    echo "fail-closed: every internal API call answers 401 (\"Internal APIs"
    echo "disabled: agent_service_token is not configured\")."
    echo ""
    echo "Add to .env -- use the SAME value on both the server and agent-pi:"
    echo "  AGENT_SERVICE_TOKEN=\$(openssl rand -hex 32)"
    echo "  BUZZOWL_SECRET_KEY=\$(openssl rand -hex 32)"
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

if [ -n "${AGENT_SERVICE_TOKEN:-}" ] && [ -z "${BUZZOWL_SECRET_KEY:-}" ]; then
    echo "WARNING: BUZZOWL_SECRET_KEY is not set -- stored workspace LLM keys are encrypted using AGENT_SERVICE_TOKEN instead; rotating the token later will make them unreadable (set BUZZOWL_SECRET_KEY explicitly: openssl rand -hex 32)."
fi

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
