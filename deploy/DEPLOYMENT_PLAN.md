# Buzzowl — Deployment Plan (Phase 19)

## Goal

Deploy Buzzowl to a self-hosted ThinkCentre server so the app is accessible via a public HTTPS URL, and set up a CI/CD pipeline so that every merge to `main` automatically deploys the latest version after tests pass.

---

## Infrastructure Decision

**Use the existing ThinkCentre running Proxmox.**

- Specs: Intel i3/Celeron, 8 GB RAM total
- A dedicated Ubuntu 22.04 LTS VM gets: **6 GB RAM, 4 vCores, 80 GB disk**
- No Ollama on server — all LLM calls go to OpenRouter / Claude API
- Audio transcription disabled on server — text-input mode only (`TRANSCRIPTION_MODE=app`)
- Cloudflare Tunnel already running → HTTPS is already solved, extend it to port 8000

**Upgrade path:** Hetzner CX32 (4 vCPU / 8 GB / €6.79/mo) or CX42 (8 vCPU / 16 GB / €14.99/mo) if home bandwidth or RAM becomes a bottleneck. Same setup steps apply.

---

## Everything Runs in Docker

`docker compose --profile research up -d` starts all 8 containers:

| Container | Purpose | Port |
|---|---|---|
| `db` | PostgreSQL 16 + pgvector | 5432 |
| `searxng` | Self-hosted web search | 8080 |
| `pgweb` | DB browser UI | 5433 |
| `server` | FastAPI main app (Python) | 8000 |
| `camofox` | Firefox fingerprint spoof | 9377 |
| `browser-service` | Playwright | 3001 |
| `agent-pi` | TypeScript agent service | 8001 |
| `agent-hermes` | Python agent service | 8002 |

No systemd service. No Python venv on the host. Docker handles everything including restarts (`restart: unless-stopped`).

---

## Resource Footprint (without Ollama)

| Service | Est. RAM |
|---|---|
| PostgreSQL + pgvector | 300–600 MB |
| SearXNG | 150–300 MB |
| pgweb | 50 MB |
| server (Python FastAPI) | 150–300 MB |
| agent-pi (Node.js) | 150–250 MB |
| agent-hermes (Python) | 150–300 MB |
| Camofox (Firefox) | 400–800 MB |
| browser-service (Playwright + 1 GB shm) | 800 MB–1.5 GB |
| **Total** | **~2.2–4.1 GB** |

Fits in 6 GB.

---

## Server Docker Image

`Dockerfile` at the repo root builds the `server` container:
- Base: `python:3.11-slim`
- Uses `requirements.server.txt` — **no whisperx / PyTorch** (~300 MB image vs ~3 GB)
- `whisperx` / `faster_whisper` imports in `routers/transcription.py` wrapped in `try/except` so the server starts without them
- `TRANSCRIPTION_MODE=app` env var disables Whisper model loading at startup

---

## Camofox on x86_64

The existing Camofox image is ARM64 (built on Mac). On the ThinkCentre (x86_64) it must be built once:

```bash
cd /opt/buzzowl/camofox-browser
make build-x86
```

The `docker-compose.yml` reads `${CAMOFOX_ARCH:-aarch64}` so:
- Mac `.env`: leave `CAMOFOX_ARCH` unset (defaults to `aarch64`) — no change
- Server `.env`: add `CAMOFOX_ARCH=x86_64`

---

## CI/CD — Self-hosted GitHub Actions Runner

A GitHub Actions runner on the ThinkCentre VM connects **outbound** to GitHub. No inbound SSH, no secrets.

```
Push PR  →  GitHub-hosted runner  →  Run tests (pytest, no slow/ollama)
                                            │
                                      tests pass
                                            │
Merge to main  →  deploy job  →  self-hosted runner on ThinkCentre
                                  └── git pull
                                  └── docker compose --profile research up -d --build
```

### Workflow files already created

| File | Purpose |
|---|---|
| `.github/workflows/ci.yml` | Tests on every push + PR |
| `.github/workflows/deploy.yml` | Deploy on merge to main (after tests pass) |

---

## Setup Checklist

### On the ThinkCentre VM

- [ ] Create Ubuntu 22.04 LTS VM — 6 GB RAM, 4 vCores, 80 GB disk
- [ ] `apt install docker.io docker-compose-plugin git make curl`
- [ ] `sudo usermod -aG docker $USER && newgrp docker`
- [ ] `git clone <repo> /opt/buzzowl`
- [ ] Copy `.env` to server, add `CAMOFOX_ARCH=x86_64`, set real `AGENT_SERVICE_TOKEN`
- [ ] `cd camofox-browser && make build-x86` (one-time Camofox build)
- [ ] `docker compose --profile research up -d` (starts all 8 containers)
- [ ] Install GitHub Actions self-hosted runner (repo Settings → Actions → Runners)
- [ ] Extend Cloudflare Tunnel: add `buzzowl.yourdomain.com → localhost:8000`

### On GitHub

- [ ] Commit and push `.github/workflows/` to `main`
- [ ] Confirm CI run passes
- [ ] Merge a trivial commit → confirm deploy job runs on ThinkCentre → server updates

---

## Pre-launch Checklist (from todo.md)

- [ ] Register Telegram webhook: `curl "https://api.telegram.org/bot{TOKEN}/setWebhook?url=https://buzzowl.yourdomain.com/api/telegram/webhook"`
- [ ] Set `AGENT_SERVICE_TOKEN` to a real value
- [ ] Test with a second browser / user account before declaring live

---

## Verification

```bash
docker compose --profile research ps      # all 8 containers: Up
curl http://localhost:8000/api/health     # 200 OK (endpoint needs to be added — Phase 19 todo)
```

From browser: `https://buzzowl.yourdomain.com` → login, trigger a research run, confirm Telegram notification arrives.

CI/CD: `git commit --allow-empty -m "ci: smoke test" && git push origin main` → CI passes → deploy job runs → `docker compose up -d --build` → server on latest `main`.
