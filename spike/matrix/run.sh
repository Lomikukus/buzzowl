#!/usr/bin/env bash
# Buzzowl Phase 5 spike — bring up dev Synapse + two bot "installs", exchange one
# E2EE client card, print the proof. Idempotent; `./run.sh clean` wipes state.
set -euo pipefail
cd "$(dirname "$0")"

if [[ "${1:-}" == "clean" ]]; then
  docker compose down -v --remove-orphans 2>/dev/null || true
  rm -rf data
  echo "cleaned"; exit 0
fi

mkdir -p data/synapse data/a data/b

# 1) Generate a Synapse config once, then patch it for a dev instance
if [[ ! -f data/synapse/homeserver.yaml ]]; then
  echo "== generating Synapse config"
  docker run --rm -v "$PWD/data/synapse:/data" \
    -e SYNAPSE_SERVER_NAME=synapse -e SYNAPSE_REPORT_STATS=no \
    matrixdotorg/synapse:latest generate >/dev/null
  docker run --rm -v "$PWD/data/synapse:/data" --entrypoint sh matrixdotorg/synapse:latest -c 'cat >> /data/homeserver.yaml <<EOF

# --- spike overrides (dev only) ---
enable_registration: true
enable_registration_without_verification: true
rc_message: { per_second: 200, burst_count: 1000 }
rc_registration: { per_second: 200, burst_count: 1000 }
rc_login:
  address: { per_second: 200, burst_count: 1000 }
  account: { per_second: 200, burst_count: 1000 }
  failed_attempts: { per_second: 200, burst_count: 1000 }
rc_joins:
  local: { per_second: 200, burst_count: 1000 }
  remote: { per_second: 200, burst_count: 1000 }
rc_invites:
  per_room: { per_second: 200, burst_count: 1000 }
  per_user: { per_second: 200, burst_count: 1000 }
max_upload_size: 50M
EOF'
fi

# 2) Fresh bot state each run (new devices) unless KEEP=1
if [[ "${KEEP:-0}" != "1" ]]; then
  rm -rf data/a/* data/b/*
fi

# 3) Optional hand-off into a running Buzzowl on the host
export BUZZOWL_URL="${BUZZOWL_URL:-}"
export AGENT_SERVICE_TOKEN="${AGENT_SERVICE_TOKEN:-}"
export BUZZOWL_ORG_ID="${BUZZOWL_ORG_ID:-0}"

echo "== starting Synapse + bots"
docker compose up -d --build synapse
docker compose up --build --abort-on-container-exit --exit-code-from instance-a instance-b instance-a || true

echo
echo "== PROOF (data/a/proof.json, data/b/proof.json)"
python3 - <<'PY'
import json, os
for side in ("a", "b"):
    p = f"data/{side}/proof.json"
    if not os.path.exists(p):
        print(f"[{side}] no proof written"); continue
    d = json.load(open(p))
    print(f"[{side}] role={d['role']} steps={len(d['steps'])} error={d.get('error')}")
    for s in d["steps"]:
        print("   -", s["msg"], {k: v for k, v in s.items() if k not in ("t", "msg")} or "")
PY
