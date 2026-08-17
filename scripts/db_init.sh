#!/bin/bash
# db_init.sh — start PostgreSQL and apply schema from scratch
# Usage:
#   ./scripts/db_init.sh           — start DB and apply schema (safe, skips if tables exist)
#   ./scripts/db_init.sh --reset   — drop all tables first, then re-apply schema (destructive)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RESET=false

for arg in "$@"; do
  case $arg in
    --reset) RESET=true ;;
    *) echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

echo "Starting PostgreSQL..."
docker compose -f "$REPO_ROOT/docker-compose.yml" up -d

echo "Waiting for PostgreSQL to be healthy..."
until docker compose -f "$REPO_ROOT/docker-compose.yml" exec -T db pg_isready -U whisper -d whisper &>/dev/null; do
  sleep 1
done

if [ "$RESET" = true ]; then
  echo "WARNING: --reset will drop all tables and delete all data."
  read -rp "Are you sure? (yes/N): " confirm
  if [ "$confirm" != "yes" ]; then
    echo "Aborted."
    exit 0
  fi
  echo "Dropping all tables..."
  docker compose -f "$REPO_ROOT/docker-compose.yml" exec -T db psql -U whisper -d whisper -c "
    DROP TABLE IF EXISTS heartbeats, agent_runs, document_links, documents,
                         contacts, clients, user_sessions, users, orgs CASCADE;
  "
  echo "Tables dropped."
fi

echo "Applying schema..."
docker cp "$REPO_ROOT/schema.sql" "$(docker compose -f "$REPO_ROOT/docker-compose.yml" ps -q db):/tmp/schema.sql"
docker compose -f "$REPO_ROOT/docker-compose.yml" exec -T db psql -U whisper -d whisper -f /tmp/schema.sql

echo "Done. Database is ready."
