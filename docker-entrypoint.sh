#!/bin/sh
set -e

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
