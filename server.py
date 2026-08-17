#!/usr/bin/env python3
"""
Buzzowl — live transcription web server
Usage: python server.py  →  open http://localhost:8000

This file is intentionally thin. All routes and business logic live in:
  context.py              — shared singletons (config, executor, DB, locks)
  routers/auth.py         — /api/auth/* + current_user dependency
  routers/pipeline.py     — session lifecycle, promotion, background tasks
  routers/knowledge.py    — clients, contacts, documents, search
  routers/agents.py       — agent runs, research queue, /ws/agents
  routers/transcription.py — live transcription, /ws, model loaders
"""

import asyncio
import logging
import os
import re
import secrets

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

import context
from context import BASE_DIR, DB_AVAILABLE, RATE_LIMIT_AVAILABLE, config, console, db_module, executor, limiter, pwd_context
from routers import auth, pipeline, knowledge, agents, transcription, chat, notifications, internal, products, match, users, feedback, benchmark, evaluation, today, tasks
from routers.pipeline import (
    ensure_dirs,
    _migrate_legacy_dirs,
    _pipeline_sweep_loop,
    _start_heartbeat_scheduler,
)
from routers.transcription import get_live_model

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s: %(message)s",
)

app = FastAPI(title="Buzzowl")


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https: http:; "
            "connect-src 'self' wss: ws: *; "
            "font-src 'self' data:"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)


# ---------------------------------------------------------------------------
# Rate limiting (slowapi)
# ---------------------------------------------------------------------------

if RATE_LIMIT_AVAILABLE:
    from slowapi import _rate_limit_exceeded_handler  # type: ignore
    from slowapi.errors import RateLimitExceeded      # type: ignore
    from slowapi.middleware import SlowAPIMiddleware   # type: ignore
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)


# ---------------------------------------------------------------------------
# First-run bootstrap
# ---------------------------------------------------------------------------

_boot_logger = logging.getLogger("wk.server")


async def first_run_bootstrap() -> None:
    """Make a fresh install reachable. Runs once at startup, after init_db.

    Only acts when the orgs table is empty:
      - ADMIN_USERNAME + ADMIN_PASSWORD env set → create the org (name from
        ADMIN_ORG, default "My Organization") and an admin user directly.
      - env not set → ensure ONE unused registration key exists (reusing a
        still-valid key from a previous boot instead of minting a new one on
        every restart) and print it loudly: register at /login.

    Idempotent and race-safe enough for the single-process server; best-effort —
    any failure is logged and never blocks startup.
    """
    if not DB_AVAILABLE or db_module is None or not db_module._pool:
        return
    try:
        if await db_module.get_first_org():
            return

        admin_username = (os.environ.get("ADMIN_USERNAME") or "").strip()
        admin_password = os.environ.get("ADMIN_PASSWORD") or ""

        if admin_username and admin_password:
            org_name = (os.environ.get("ADMIN_ORG") or "").strip() or "My Organization"
            org_slug = re.sub(r"[^a-z0-9]+", "-", org_name.lower()).strip("-") or "my-organization"
            org = await db_module.create_org(org_name, org_slug)
            await db_module.seed_default_heartbeats(org["id"])
            await db_module.create_user(
                org_id=org["id"],
                username=admin_username,
                display_name=admin_username,
                password_hash=pwd_context.hash(admin_password),
                role="admin",
            )
            console.print(
                f"  First run: created org [cyan]{org_name}[/cyan] (slug: [cyan]{org_slug}[/cyan]) "
                f"with admin user [cyan]{admin_username}[/cyan] — login at /login"
            )
            _boot_logger.info(
                "First run: created org '%s' (slug %s) with admin user '%s'",
                org_name, org_slug, admin_username,
            )
        else:
            # No admin credentials in the environment — issue a registration key
            # instead (inline insert; no dependency on scripts/manage_registration.py).
            async with db_module._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT reg_key FROM registration_keys
                    WHERE used_at IS NULL AND (expires_at IS NULL OR expires_at > NOW())
                    ORDER BY id LIMIT 1
                    """
                )
                if row:
                    key = row["reg_key"]
                else:
                    key = secrets.token_urlsafe(32)
                    await conn.execute(
                        "INSERT INTO registration_keys (reg_key, label) VALUES ($1, $2)",
                        key, "first-run bootstrap",
                    )
            banner = f"FIRST RUN: no organisation exists yet — register at /login with key {key}"
            console.print(f"\n[bold yellow]{'=' * 74}[/bold yellow]")
            console.print(f"[bold yellow]{banner}[/bold yellow]")
            console.print(f"[bold yellow]{'=' * 74}[/bold yellow]\n")
            _boot_logger.warning("First run: register at /login with key %s", key)
    except Exception as exc:
        _boot_logger.warning("First-run bootstrap skipped: %s", exc)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup() -> None:
    ensure_dirs()
    _migrate_legacy_dirs()
    console.print(f"\n[bold]Buzzowl — live server[/bold]")
    loop = asyncio.get_event_loop()
    if config.get("transcription_mode", "local") == "local":
        console.print(f"  Default live model: [yellow]{config['live_model']}[/yellow]")
        await loop.run_in_executor(executor, get_live_model, config["live_model"])
    else:
        console.print(f"  Transcription mode: [yellow]app[/yellow] — Whisper models not loaded")

    if DB_AVAILABLE:
        db_module.set_main_loop(loop)
        await db_module.init_db(
            config.get("db_url", ""),
            config.get("embed_model", "text-embedding-3-small"),
            int(config.get("embed_dim", 768)),
            embed_backend=config.get("embed_backend", ""),
            embed_url=config.get("embed_url", ""),
            embed_api_key=config.get("embed_api_key", ""),
            pool_min=int(config.get("db_pool_min", 2)),
            pool_max=int(config.get("db_pool_max", 20)),
        )
        await first_run_bootstrap()

    # Internal-API security posture: with no agent_service_token the internal
    # endpoints fail closed (401) unless the explicit dev backdoor is set.
    if not config.get("agent_service_token", ""):
        if os.environ.get("ALLOW_INSECURE_INTERNAL", "") == "1":
            console.print(
                "  [bold red]WARNING: ALLOW_INSECURE_INTERNAL=1 — internal agent APIs "
                "accept UNAUTHENTICATED requests (dev only)[/bold red]"
            )
            _boot_logger.warning(
                "ALLOW_INSECURE_INTERNAL=1 — internal agent APIs accept unauthenticated requests"
            )
        else:
            console.print(
                "  [yellow]agent_service_token not set — internal APIs disabled (401). "
                "Set AGENT_SERVICE_TOKEN, or ALLOW_INSECURE_INTERNAL=1 for local dev.[/yellow]"
            )
            _boot_logger.warning("agent_service_token not set — internal APIs disabled")

    asyncio.create_task(_pipeline_sweep_loop())
    console.print(
        f"  Pipeline sweep: every [yellow]{config.get('pipeline_sweep_interval_min', 10)}m[/yellow]"
    )

    asyncio.create_task(_start_heartbeat_scheduler())

    # Re-attach watchers to delegated runs left in-flight by the previous process
    # so a deploy/restart doesn't leave agent runs hanging or silently lost.
    if DB_AVAILABLE:
        from routers.agents import reattach_orphaned_watchers
        asyncio.create_task(reattach_orphaned_watchers())

    if DB_AVAILABLE:
        n_workers = int(config.get("research_workers", 4))
        org = await db_module.get_first_org()
        if org:
            from agents.research_runner import run_research_workers
            run_research_workers(org["id"], n_workers=n_workers)
            console.print(f"  Research workers: [green]{n_workers} workers started[/green]")

    console.print(f"  Ready. Open [cyan]http://localhost:8000[/cyan]\n")


@app.on_event("shutdown")
async def shutdown() -> None:
    if DB_AVAILABLE:
        await db_module.close_db()
    if context._scheduler and context._scheduler.running:
        context._scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Static files + page routes
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


_NO_CACHE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}


def _html(filename: str) -> HTMLResponse:
    return HTMLResponse((BASE_DIR / "static" / filename).read_text(), headers=_NO_CACHE)


@app.get("/")
async def get_index() -> HTMLResponse:
    return _html("home.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> HTMLResponse:
    return _html("login.html")


@app.get("/record", response_class=HTMLResponse)
async def get_recorder() -> HTMLResponse:
    return _html("index.html")


@app.get("/agents", response_class=HTMLResponse)
async def agents_dashboard() -> HTMLResponse:
    return _html("agents.html")


@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page() -> HTMLResponse:
    return _html("knowledge.html")


@app.get("/clients", response_class=HTMLResponse)
async def clients_list_page() -> HTMLResponse:
    return _html("clients.html")


@app.get("/ranking", response_class=HTMLResponse)
async def ranking_page() -> HTMLResponse:
    return _html("ranking.html")


@app.get("/research", response_class=HTMLResponse)
async def research_list_page() -> HTMLResponse:
    return _html("research.html")


@app.get("/insights", response_class=HTMLResponse)
async def insights_page() -> HTMLResponse:
    return _html("insights.html")


@app.get("/client/{name}", response_class=HTMLResponse)
async def client_detail_page(name: str) -> HTMLResponse:
    return _html("client.html")


@app.get("/contact/{name}", response_class=HTMLResponse)
async def contact_detail_page(name: str) -> HTMLResponse:
    return _html("contact.html")


@app.get("/products", response_class=HTMLResponse)
async def products_page() -> HTMLResponse:
    return _html("products.html")


@app.get("/products/{product_id}", response_class=HTMLResponse)
async def product_detail_page(product_id: int) -> HTMLResponse:
    return _html("product.html")


@app.get("/match", response_class=HTMLResponse)
async def match_page() -> HTMLResponse:
    return _html("match.html")


@app.get("/opportunities", response_class=HTMLResponse)
async def opportunities_page() -> HTMLResponse:
    return _html("opportunities.html")


@app.get("/news", response_class=HTMLResponse)
async def news_feed_page() -> HTMLResponse:
    return _html("news.html")


@app.get("/today", response_class=HTMLResponse)
async def today_page() -> HTMLResponse:
    return _html("today.html")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page() -> HTMLResponse:
    return _html("settings.html")


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------

@app.get("/api/config/models")
async def get_config_models():
    """Return cloud_models list from config for UI dropdowns."""
    import yaml as _yaml
    try:
        with open("config.yaml") as f:
            cfg = _yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    return {"cloud_models": cfg.get("cloud_models", [])}


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health_check():
    """Returns DB + agent service liveness. No auth required — used by uptime monitors."""
    db_ok = False
    if DB_AVAILABLE and db_module is not None:
        try:
            await db_module._pool.fetchval("SELECT 1")
            db_ok = True
        except Exception:
            pass

    pi_url = config.get("agent_service_url_pi", "")
    pi_ok = False

    async with httpx.AsyncClient() as client:
        if pi_url:
            try:
                r = await client.get(f"{pi_url}/health", timeout=2.0)
                pi_ok = r.status_code == 200
            except Exception:
                pass

    # Embeddings: a live probe — if this fails, vector search is degraded to
    # FTS-only and every new document is stored without an embedding.
    embed_ok = False
    if DB_AVAILABLE and db_module is not None:
        try:
            embed_ok = bool(await db_module.embed_text("health check"))
        except Exception:
            pass

    checks = {
        "db": db_ok,
        "pi": pi_ok,
        "embeddings": embed_ok,
        "embed_stats": db_module.embed_stats if DB_AVAILABLE and db_module else {},
    }
    return {"status": "healthy" if db_ok else "degraded", "checks": checks}


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(auth.router)
app.include_router(pipeline.router)
app.include_router(knowledge.router)
app.include_router(agents.router)
app.include_router(transcription.router)
app.include_router(chat.router)
app.include_router(notifications.router)
app.include_router(internal.router)
app.include_router(products.router)
app.include_router(match.router)
app.include_router(users.router)
app.include_router(feedback.router)
app.include_router(benchmark.router)
app.include_router(evaluation.router)
app.include_router(today.router)
app.include_router(tasks.router)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
