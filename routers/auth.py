"""
Auth router — user/org registration, login, logout, and the current_user dependency.

All routes are under /api/auth/*. The current_user dependency is imported by other
routers that require authentication.
"""

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from context import DB_AVAILABLE, RATE_LIMIT_AVAILABLE, console, db_module, limiter, pwd_context

if RATE_LIMIT_AVAILABLE:
    _limit = limiter.limit
else:
    def _limit(rate: str):  # no-op decorator when slowapi not installed
        def decorator(fn):
            return fn
        return decorator

router = APIRouter(prefix="/api/auth")


# ---------------------------------------------------------------------------
# Dependency: resolve Bearer token → user row
# ---------------------------------------------------------------------------

async def current_user(request: Request, authorization: Optional[str] = Header(None)) -> dict:
    """FastAPI dependency. Validates Bearer token and returns the user row.

    Raises 401 if missing/invalid, 503 if DB is unavailable, 402 for writes when
    the org is suspended (hosted: subscription lapsed — data kept, read-only).
    """
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    user = await db_module.get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if request.method not in ("GET", "HEAD", "OPTIONS") and not request.url.path.startswith("/api/auth/"):
        from routers.operator import is_suspended
        if await is_suspended(user["org_id"]):
            raise HTTPException(status_code=402, detail="This workspace is suspended (subscription inactive) — read-only until it is resumed")
    return user


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def context_config() -> dict:
    from context import config as _cfg
    return _cfg or {}


def _signup_open() -> bool:
    return bool((context_config().get("hosted") or {}).get("signup_enabled"))


@router.get("/signup-status")
async def signup_status():
    """Public: is self-service org creation open on this deployment?"""
    return {"signup_enabled": _signup_open()}


@router.post("/register")
@_limit("5/minute")
async def register(request: Request, body: dict):
    """Create a new org + admin user. Requires a valid operator-issued registration_key."""
    org_name         = body.get("org_name", "").strip()
    username         = body.get("username", "").strip()
    password         = body.get("password", "")
    display_name     = body.get("display_name", username).strip()
    email            = body.get("email", "").strip() or None
    registration_key = body.get("registration_key", "").strip()
    org_slug         = body.get("org_slug", "").strip() or re.sub(
        r"[^a-z0-9]+", "-", org_name.lower()
    ).strip("-")

    if not all([org_name, org_slug, username, password]):
        raise HTTPException(status_code=400, detail="org_name, username, and password are required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    # Validate registration key — unless the operator opened self-service signup
    # (hosted.signup_enabled), in which case anyone may create their own org.
    open_signup = _signup_open()
    _invalid_rk = "Registration key is invalid, already used, or expired"
    if not registration_key and not open_signup:
        raise HTTPException(status_code=400, detail="A registration key is required to create an organisation")
    if registration_key:
        rk = await db_module.get_registration_key(registration_key)
        if not rk:
            raise HTTPException(status_code=400, detail=_invalid_rk)
        if rk["used_at"] is not None:
            raise HTTPException(status_code=400, detail=_invalid_rk)
        if rk["expires_at"] and rk["expires_at"].replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail=_invalid_rk)

    existing = await db_module.get_org_by_slug(org_slug)
    if existing:
        raise HTTPException(status_code=409, detail=f"Org slug '{org_slug}' already taken")

    org = await db_module.create_org(org_name, org_slug)
    await db_module.seed_default_heartbeats(org["id"])
    if registration_key:
        consumed = await db_module.consume_registration_key(registration_key, org["id"])
        if not consumed:
            raise HTTPException(status_code=400, detail=_invalid_rk)
    else:
        # self-service tenant: record the plan the operator configured for signups
        try:
            hosted = (context_config().get("hosted") or {})
            await db_module.update_org_settings(org["id"], {"plan": hosted.get("default_plan", "light"),
                                                            "signup": "self_service"})
        except Exception:
            pass
    user = await db_module.create_user(
        org_id=org["id"],
        username=username,
        display_name=display_name,
        password_hash=pwd_context.hash(password),
        email=email,
        role="admin",
    )
    token = secrets.token_urlsafe(32)
    await db_module.create_session_token(
        user["id"], token, datetime.now(timezone.utc) + timedelta(days=30)
    )
    return {
        "token": token,
        "user": {
            "id": user["id"], "username": user["username"],
            "display_name": user["display_name"], "role": user["role"],
        },
        "org": {"id": org["id"], "name": org["name"], "slug": org["slug"]},
    }


@router.post("/login")
@_limit("10/minute")
async def login(request: Request, body: dict):
    """Authenticate with org slug + username + password; return a 30-day token."""
    org_slug = body.get("org_slug", "").strip()
    username = body.get("username", "").strip()
    password = body.get("password", "")

    if not all([org_slug, username, password]):
        raise HTTPException(status_code=400, detail="org_slug, username, and password are required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    org = await db_module.get_org_by_slug(org_slug)
    if not org:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user = await db_module.get_user_by_username(org["id"], username)
    if not user or not pwd_context.verify(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = secrets.token_urlsafe(32)
    await db_module.create_session_token(
        user["id"], token, datetime.now(timezone.utc) + timedelta(days=30)
    )
    # Log the login so the admin Insights dashboard can show login frequency / DAU.
    try:
        db_module.log_prompt(org["id"], user["id"], "login", username, {})
    except Exception:
        pass
    return {
        "token": token,
        "user": {
            "id": user["id"], "username": user["username"],
            "display_name": user["display_name"], "role": user["role"],
        },
        "org": {"id": org["id"], "name": org["name"], "slug": org["slug"]},
    }


@router.post("/logout")
async def logout(authorization: Optional[str] = Header(None)):
    """Invalidate the current session token."""
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if DB_AVAILABLE:
            await db_module.delete_session_token(token)
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    """Return the authenticated user's profile and org."""
    return {
        "user": {
            "id": user["id"], "username": user["username"],
            "display_name": user["display_name"], "role": user["role"],
            "ui_variant": user.get("ui_variant", "classic"),
        },
        "org": {"id": user["org_id"], "name": user["org_name"], "slug": user["org_slug"],
                **(await _org_flags(user["org_id"]))},
    }


async def _org_flags(org_id: int) -> dict:
    """plan + suspended for the UI (banner / plan badge); tolerant when DB helpers are absent."""
    try:
        import plans as _plans
        s = await db_module.get_org_settings(org_id)
        return {"plan": _plans.plan_of(s), "suspended": bool(s.get("suspended")),
                "suspended_reason": s.get("suspended_reason") or None}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# External login (Phase 6b): exchange a JWT from an identity provider — Supabase
# Auth, Auth0, Keycloak, any OIDC issuer — for a Buzzowl session. Configured in
# config.yaml `auth.external`; the control plane calls this after its own login
# and redirects the browser to /login#token=<session token>.
# ---------------------------------------------------------------------------

def _ext_cfg() -> dict:
    from context import config as _cfg
    return ((_cfg or {}).get("auth") or {}).get("external") or {}


_jwks_cache: dict = {}


def _verify_external_jwt(token: str) -> dict:
    """Returns the claims or raises HTTPException(401)."""
    import time
    import jwt as _jwt
    cfg = _ext_cfg()
    if not cfg.get("enabled"):
        raise HTTPException(status_code=404, detail="external login is not enabled")
    audience = cfg.get("audience") or None
    issuer = cfg.get("issuer") or None
    opts = {"verify_aud": bool(audience)}
    try:
        header = _jwt.get_unverified_header(token)
    except Exception:
        raise HTTPException(status_code=401, detail="malformed token")
    alg = header.get("alg", "")
    try:
        if alg.startswith("HS"):
            secret = cfg.get("hs256_secret") or ""
            if not secret:
                raise HTTPException(status_code=401, detail="token uses HS256 but auth.external.hs256_secret is not set")
            return _jwt.decode(token, secret, algorithms=["HS256"], audience=audience, issuer=issuer, options=opts)
        jwks_url = cfg.get("jwks_url") or ""
        if not jwks_url:
            raise HTTPException(status_code=401, detail="token uses an asymmetric key but auth.external.jwks_url is not set")
        now = time.time()
        client = _jwks_cache.get(jwks_url)
        if not client or client[0] < now:
            client = (now + 3600, _jwt.PyJWKClient(jwks_url, cache_keys=True))
            _jwks_cache[jwks_url] = client
        key = client[1].get_signing_key_from_jwt(token).key
        return _jwt.decode(token, key, algorithms=[alg], audience=audience, issuer=issuer, options=opts)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"token rejected: {type(exc).__name__}")


def _claim(claims: dict, path: str):
    cur = claims
    for part in (path or "").split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


@router.get("/external/config")
async def external_config():
    """Public: whether external login is on (login page shows the button/hint)."""
    cfg = _ext_cfg()
    return {"enabled": bool(cfg.get("enabled")), "label": cfg.get("label") or "Sign in with your account",
            "login_url": cfg.get("login_url") or ""}


@router.post("/external")
@_limit("20/minute")
async def external_login(request: Request, body: dict):
    """{token} → {token (Buzzowl session), user, org}. Maps the identity to a user by
    email; picks the org from the claim `org_claim` (default app_metadata.org_slug),
    else the user's existing org, else — with auto_provision — a fresh personal
    workspace named after the person."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    raw = (body.get("token") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="token required")
    cfg = _ext_cfg()
    claims = _verify_external_jwt(raw)
    email = (_claim(claims, cfg.get("email_claim") or "email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="token has no email claim")
    subject = str(claims.get("sub") or "")
    display = (_claim(claims, cfg.get("name_claim") or "user_metadata.full_name") or email.split("@")[0]).strip()[:120]
    org_slug = _claim(claims, cfg.get("org_claim") or "app_metadata.org_slug")

    org = await db_module.get_org_by_slug(str(org_slug)) if org_slug else None
    user = None
    if org:
        user = next((u for u in await db_module.list_users(org["id"]) if (u.get("email") or "").lower() == email), None)
    else:
        hits = await db_module.find_users_global(email, exclude_org_id=None, limit=1)
        if hits and (hits[0].get("email") or "").lower() == email:
            user = await db_module.get_user_by_id(hits[0]["id"])
            org = await db_module.get_org(user["org_id"])
    if not user:
        if not cfg.get("auto_provision", True):
            raise HTTPException(status_code=403, detail="no workspace for this account — ask your operator")
        if not org:
            base = re.sub(r"[^a-z0-9]+", "-", (display or email.split("@")[0]).lower()).strip("-")[:40] or "workspace"
            slug, n = base, 2
            while await db_module.get_org_by_slug(slug):
                slug = f"{base}-{n}"; n += 1
            org = await db_module.create_org(f"{display}'s workspace", slug)
            try:
                await db_module.seed_default_heartbeats(org["id"])
            except Exception:
                pass
            from context import config as _cfg
            plan = ((_cfg or {}).get("hosted") or {}).get("default_plan", "light")
            await db_module.update_org_settings(org["id"], {"plan": plan, "signup": "external", "external_subject": subject})
        username = re.sub(r"[^a-z0-9]+", "-", email.split("@")[0].lower()).strip("-") or "user"
        role = "admin" if not await db_module.list_users(org["id"]) else "member"
        user = await db_module.create_user(org_id=org["id"], username=username, display_name=display,
                                           password_hash=pwd_context.hash(secrets.token_urlsafe(24)), email=email, role=role)
    token = secrets.token_urlsafe(32)
    await db_module.create_session_token(user["id"], token, datetime.now(timezone.utc) + timedelta(days=30))
    try:
        db_module.log_prompt(org["id"], user["id"], "login", f"external:{subject}", {})
    except Exception:
        pass
    return {"token": token,
            "user": {"id": user["id"], "username": user["username"], "display_name": user["display_name"], "role": user["role"]},
            "org": {"id": org["id"], "name": org["name"], "slug": org["slug"]}}


@router.post("/theme")
async def set_theme(body: dict, user: dict = Depends(current_user)):
    """Opt-in UI A/B: let a user switch their own front-end theme.
    'carbon' = the IBM-style redesign; 'classic' = the current look (default)."""
    variant = (body or {}).get("variant", "classic")
    if variant not in ("classic", "carbon"):
        raise HTTPException(status_code=400, detail="variant must be 'classic' or 'carbon'")
    saved = await db_module.set_user_ui_variant(user["id"], variant)
    # Log the choice so the evaluation can compare cohorts (best-effort, non-blocking).
    try:
        db_module.log_prompt(
            org_id=user["org_id"], user_id=user["id"],
            surface="ui_theme", prompt=variant,
        )
    except Exception:
        pass
    return {"ok": True, "ui_variant": saved or variant}


@router.get("/users")
async def list_users(user: dict = Depends(current_user)):
    """Return all users in the caller's org (admin only)."""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    users = await db_module.list_users(user["org_id"])
    return {"users": users}


@router.post("/invite")
async def create_invite(body: dict, user: dict = Depends(current_user)):
    """Generate a one-time invite key for a new org member (admin only)."""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    role  = body.get("role", "member").strip()
    email = body.get("email", "").strip() or None

    if role not in ("admin", "member"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'member'")

    key        = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    inv = await db_module.create_invitation(
        org_id=user["org_id"],
        invite_key=key,
        created_by=user["id"],
        role=role,
        email=email,
        expires_at=expires_at,
    )
    return {
        "id":         inv["id"],
        "invite_key": inv["invite_key"],
        "role":       inv["role"],
        "email":      inv["email"],
        "expires_at": inv["expires_at"].isoformat() if inv["expires_at"] else None,
    }


@router.get("/invites")
async def list_invites(user: dict = Depends(current_user)):
    """List all invitations for the org (admin only)."""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    now  = datetime.now(timezone.utc)
    rows = await db_module.list_invitations(user["org_id"])
    invites = []
    for r in rows:
        expires = r["expires_at"]
        used    = r["used_at"]
        is_pending = used is None and (expires is None or expires > now)
        is_expired = used is None and expires is not None and expires <= now
        invites.append({
            "id":                   r["id"],
            "invite_key":           r["invite_key"],
            "email":                r["email"],
            "role":                 r["role"],
            "created_by_username":  r["created_by_username"],
            "created_at":           r["created_at"].isoformat() if r["created_at"] else None,
            "expires_at":           expires.isoformat() if expires else None,
            "used_at":              used.isoformat() if used else None,
            "used_by_username":     r["used_by_username"],
            "is_pending":           is_pending,
            "is_expired":           is_expired,
        })
    return {"invites": invites}


@router.delete("/invites/{invite_id}")
async def revoke_invite(invite_id: int, user: dict = Depends(current_user)):
    """Revoke an unused invitation (admin only)."""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    deleted = await db_module.delete_invitation(invite_id, user["org_id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Invite not found or already used")
    return {"ok": True}


@router.post("/accept-invite")
@_limit("10/minute")
async def accept_invite(request: Request, body: dict):
    """Redeem an invite key to create a user account in the associated org."""
    invite_key   = body.get("invite_key", "").strip()
    username     = body.get("username", "").strip()
    password     = body.get("password", "")
    display_name = body.get("display_name", username).strip()

    if not all([invite_key, username, password]):
        raise HTTPException(status_code=400, detail="invite_key, username, and password are required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    _invalid = "Invite key is invalid, already used, or expired"

    inv = await db_module.get_invitation_by_key(invite_key)
    if not inv:
        raise HTTPException(status_code=400, detail=_invalid)
    now = datetime.now(timezone.utc)
    if inv["used_at"] is not None:
        raise HTTPException(status_code=400, detail=_invalid)
    if inv["expires_at"] and inv["expires_at"].replace(tzinfo=timezone.utc) <= now:
        raise HTTPException(status_code=400, detail=_invalid)

    existing = await db_module.get_user_by_username(inv["org_id"], username)
    if existing:
        raise HTTPException(status_code=409, detail=f"Username '{username}' already taken")

    new_user = await db_module.create_user(
        org_id=inv["org_id"],
        username=username,
        display_name=display_name,
        password_hash=pwd_context.hash(password),
        role=inv["role"],
    )
    consumed = await db_module.consume_invitation(invite_key, new_user["id"])
    if not consumed:
        raise HTTPException(status_code=400, detail=_invalid)

    token = secrets.token_urlsafe(32)
    await db_module.create_session_token(
        new_user["id"], token, datetime.now(timezone.utc) + timedelta(days=30)
    )
    return {
        "token": token,
        "user": {
            "id": new_user["id"], "username": new_user["username"],
            "display_name": new_user["display_name"], "role": new_user["role"],
        },
        "org": {"id": inv["org_id"], "name": inv["org_name"], "slug": inv["org_slug"]},
    }


@router.post("/users")
async def invite_user(body: dict, user: dict = Depends(current_user)):
    """Create a new member in the caller's org (admin only)."""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    username     = body.get("username", "").strip()
    password     = body.get("password", "")
    display_name = body.get("display_name", username).strip()
    email        = body.get("email", "").strip() or None
    role         = body.get("role", "member").strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password are required")
    if role not in ("admin", "member"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'member'")

    existing = await db_module.get_user_by_username(user["org_id"], username)
    if existing:
        raise HTTPException(status_code=409, detail=f"Username '{username}' already taken")

    new_user = await db_module.create_user(
        org_id=user["org_id"],
        username=username,
        display_name=display_name,
        password_hash=pwd_context.hash(password),
        email=email,
        role=role,
    )
    return {
        "user": {
            "id": new_user["id"], "username": new_user["username"],
            "display_name": new_user["display_name"], "role": new_user["role"],
        }
    }
