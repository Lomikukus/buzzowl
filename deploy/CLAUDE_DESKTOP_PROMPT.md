# Claude Desktop Prompt — Buzzowl Server Deployment

> Copy and paste everything below this line into Claude Desktop.

---

I am deploying a self-hosted web app called **Buzzowl** to my own server. I need step-by-step help setting up the server, deploying the app, and configuring CI/CD. Everything runs in Docker.

---

## What the app is

Buzzowl is a **research and knowledge sharing platform for sales teams**. It captures knowledge from meetings (audio → transcript → summary), enriches it with autonomous AI agent research, and makes it searchable through a web UI.

**Tech stack:**
- All services run in Docker via `docker compose --profile research up -d`
- LLM: OpenRouter API (cloud) — no local Ollama on the server
- Audio transcription is disabled on the server (runs on Mac locally)

**8 Docker containers when fully started:**

| Container | Purpose | Port |
|---|---|---|
| `db` | PostgreSQL 16 + pgvector | 5432 |
| `searxng` | Self-hosted web search | 8080 |
| `pgweb` | DB browser UI | 5433 |
| `server` | FastAPI main app (Python) | 8000 |
| `camofox` | Firefox with fingerprint spoofing | 9377 |
| `browser-service` | Playwright browser automation | 3001 |
| `agent-pi` | TypeScript agent service | 8001 |
| `agent-hermes` | Python async agent service | 8002 |

The first three (`db`, `searxng`, `pgweb`) start with just `docker compose up -d`. All eight start with `docker compose --profile research up -d`.

---

## My infrastructure

- **ThinkCentre mini PC** running Proxmox (hypervisor)
  - CPU: Intel i3/Celeron
  - Total RAM: 8 GB (shared across all VMs)
  - Already has: an Ubuntu VM running n8n in Docker
  - Already has: a Docker container with a Cloudflare Tunnel that exposes services via HTTPS — I will extend this to also expose Buzzowl on port 8000

- **Plan:** create a new dedicated Ubuntu 22.04 LTS VM for Buzzowl with **6 GB RAM, 4 vCores, 80 GB disk**

- **Public HTTPS:** handled by the existing Cloudflare Tunnel. I just need to add `buzzowl.yourdomain.com → localhost:8000` to the tunnel config.

---

## What has already been decided and implemented (do not re-decide these)

1. **Everything runs in Docker** — the FastAPI Python server (`server`) is containerised alongside all other services. The `Dockerfile` at the repo root builds it. It uses `requirements.server.txt` (no PyTorch/whisperx — the server runs in text-input mode only).

2. **Camofox (Firefox browser automation)** was built for ARM64 (Mac). On the ThinkCentre (x86_64) it must be rebuilt once with `make build-x86` in the `camofox-browser/` directory. The `docker-compose.yml` reads `CAMOFOX_ARCH` from `.env` (defaults to `aarch64`; server sets it to `x86_64`).

3. **CI/CD pipeline is already written** — two GitHub Actions workflow files exist:
   - `.github/workflows/ci.yml` — runs `pytest tests/ -m "not slow and not ollama" -x` on every push/PR using a GitHub-hosted runner
   - `.github/workflows/deploy.yml` — on merge to `main`, after tests pass, runs on a **self-hosted runner** on the ThinkCentre VM: `git pull` + `docker compose --profile research up -d --build`

4. **Self-hosted GitHub Actions runner** — installed on the ThinkCentre VM, connects outbound to GitHub. No inbound SSH required.

5. **No systemd service** — the Python server is now a Docker container, so no systemd unit is needed. `docker compose --profile research up -d` starts and manages everything, including `restart: unless-stopped` on all containers.

6. **No audio transcription on server** — `TRANSCRIPTION_MODE=app` is set in docker-compose, so the server skips loading Whisper models.

---

## What I need help with

Help me execute the server setup step by step.

### Proxmox VM creation
1. Create Ubuntu 22.04 LTS VM in Proxmox: 6 GB RAM, 4 vCores, 80 GB disk
2. Install packages: `docker.io docker-compose-plugin git make curl`
3. Add user to docker group: `sudo usermod -aG docker $USER`

### App deployment
4. Clone repo to `/opt/buzzowl`
5. Copy `.env` from Mac (`scp .env user@server:/opt/buzzowl/.env`), add `CAMOFOX_ARCH=x86_64`, set a real `AGENT_SERVICE_TOKEN` (`openssl rand -hex 32`)
6. Build Camofox for x86_64: `cd camofox-browser && make build-x86`
7. Start all containers: `docker compose --profile research up -d`

### CI/CD
8. Install GitHub Actions self-hosted runner on the VM (repo Settings → Actions → Runners → New self-hosted runner → Linux / x64)
9. Push the workflow files to GitHub, confirm CI passes, confirm deploy job fires on merge to `main`

### Cloudflare Tunnel
10. Extend existing tunnel config: add hostname `buzzowl.yourdomain.com → http://localhost:8000`

### Verification
11. `docker compose --profile research ps` → all 8 containers Up
12. `curl http://localhost:8000/api/health` → 200 OK
13. Open `https://buzzowl.yourdomain.com` → login screen
14. Merge a trivial commit to `main` → CI passes → deploy job runs → server auto-updates

---

## Key things to know

- `docker compose --profile research up -d --build` is the single command that starts and updates everything. The `--build` flag rebuilds changed images. The `--profile research` flag includes all 8 containers (without it, only db/searxng/pgweb start).
- All containers use `restart: unless-stopped` so they survive reboots automatically.
- The self-hosted runner means GitHub never needs to SSH into the server — the runner connects outbound. This avoids any firewall complexity.
- The Cloudflare Tunnel is already working for n8n on this machine — just add a new hostname entry for port 8000, no new infrastructure needed.
- `.env` is never committed to git. It must be copied to the server manually. The server reads it at container startup.
- The server container uses `requirements.server.txt` (no PyTorch). If transcription is ever needed on the server in future, the full `requirements.txt` would be used instead.
