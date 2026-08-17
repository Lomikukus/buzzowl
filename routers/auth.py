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

async def current_user(authorization: Optional[str] = Header(None)) -> dict:
    """FastAPI dependency. Validates Bearer token and returns the user row.

    Raises 401 if missing/invalid, 503 if DB is unavailable.
    """
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    user = await db_module.get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

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

    # Validate registration key
    if not registration_key:
        raise HTTPException(status_code=400, detail="A registration key is required to create an organisation")
    rk = await db_module.get_registration_key(registration_key)
    _invalid_rk = "Registration key is invalid, already used, or expired"
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
    consumed = await db_module.consume_registration_key(registration_key, org["id"])
    if not consumed:
        raise HTTPException(status_code=400, detail=_invalid_rk)
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
        "org": {"id": user["org_id"], "name": user["org_name"], "slug": user["org_slug"]},
    }


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
