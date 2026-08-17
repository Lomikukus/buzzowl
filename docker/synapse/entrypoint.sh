#!/bin/sh
# Buzzowl bundled Synapse (docker compose --profile federation).
# First start: generate homeserver.yaml for SYNAPSE_SERVER_NAME and append the
# settings Buzzowl needs (shared-secret registration for the org bots, media
# size). Later starts: just run.
set -e
if [ ! -f /data/homeserver.yaml ]; then
  echo "[buzzowl-synapse] generating config for server_name=${SYNAPSE_SERVER_NAME}"
  /start.py generate
  cat >> /data/homeserver.yaml <<YAML

# --- added by Buzzowl ---
# Buzzowl creates its bot accounts with this secret (config.yaml federation.registration_shared_secret)
registration_shared_secret: "${SYNAPSE_REGISTRATION_SHARED_SECRET:-change-me}"
enable_registration: false
max_upload_size: 50M
# Rooms are tiny and private; keep the rate limits from throttling bot syncs.
rc_message: { per_second: 20, burst_count: 100 }
rc_joins:
  local: { per_second: 20, burst_count: 100 }
  remote: { per_second: 20, burst_count: 100 }
YAML
fi
exec /start.py
