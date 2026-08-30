#!/usr/bin/env bash
# init-env.sh — create .env from .env.example and fill the secrets Buzzowl
# refuses to start without (AGENT_SERVICE_TOKEN), or silently misconfigures
# if left blank (BUZZOWL_SECRET_KEY, SEARXNG_SECRET).
#
# Idempotent — safe to run any time:
#   - .env missing entirely  -> created from .env.example, then filled.
#   - .env already exists    -> left in place; only the three vars below are
#                                filled, and only if they are missing/empty.
#                                Nothing else in .env is touched.
#
#   ./scripts/init-env.sh
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE=".env"
EXAMPLE_FILE=".env.example"
VARS="AGENT_SERVICE_TOKEN BUZZOWL_SECRET_KEY SEARXNG_SECRET"

if ! command -v openssl >/dev/null 2>&1; then
    echo "ERROR: openssl not found. Install it, or set these by hand in $ENV_FILE:" >&2
    echo "  $VARS" >&2
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    if [ ! -f "$EXAMPLE_FILE" ]; then
        echo "ERROR: $EXAMPLE_FILE not found — cannot create $ENV_FILE." >&2
        exit 1
    fi
    cp "$EXAMPLE_FILE" "$ENV_FILE"
    echo "Created $ENV_FILE from $EXAMPLE_FILE."
else
    echo "$ENV_FILE already exists — leaving it as-is, only filling missing secrets."
fi

# Current value of NAME in $ENV_FILE (empty if unset, blank, or commented out).
get_value() {
    grep -E "^${1}=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d'=' -f2-
}

# Set NAME=VALUE in $ENV_FILE: replaces an existing "NAME=..." line in place,
# or appends a new one if NAME is not present at all.
set_value() {
    local name="$1" value="$2" tmp
    if grep -qE "^${name}=" "$ENV_FILE"; then
        tmp=$(mktemp)
        awk -v name="$name" -v value="$value" \
            'BEGIN{FS=OFS="="} $1==name{$0=name"="value} {print}' \
            "$ENV_FILE" > "$tmp"
        mv "$tmp" "$ENV_FILE"
    else
        printf '%s=%s\n' "$name" "$value" >> "$ENV_FILE"
    fi
}

filled=""
for name in $VARS; do
    current="$(get_value "$name")"
    if [ -z "$current" ]; then
        set_value "$name" "$(openssl rand -hex 32)"
        filled="$filled $name"
    fi
done

if [ -n "$filled" ]; then
    echo "Generated fresh values (openssl rand -hex 32) for:$filled"
else
    echo "AGENT_SERVICE_TOKEN, BUZZOWL_SECRET_KEY and SEARXNG_SECRET were already set — nothing to fill."
fi

echo "Next: add at least one LLM credential to $ENV_FILE (e.g. OPENROUTER_API_KEY=...), then run: docker compose up -d"
