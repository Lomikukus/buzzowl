"""
User management router — role changes and user deletion.
All routes are under /api/users/* and are admin-only.
"""

from fastapi import APIRouter, Depends, HTTPException

from context import DB_AVAILABLE, db_module
from routers.auth import current_user

router = APIRouter(prefix="/api/users")


@router.get("")
async def list_users(user: dict = Depends(current_user)):
    """List all members in the caller's org (admin only)."""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    users = await db_module.list_users(user["org_id"])
    return {"users": [
        {
            "id":           u["id"],
            "username":     u["username"],
            "display_name": u["display_name"],
            "email":        u.get("email"),
            "role":         u["role"],
            "created_at":   u["created_at"].isoformat() if u["created_at"] else None,
        }
        for u in users
    ]}


@router.patch("/{user_id}")
async def update_user(user_id: int, body: dict, user: dict = Depends(current_user)):
    """Update a user's role and/or email in the caller's org (admin only)."""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    if "role" not in body and "email" not in body:
        raise HTTPException(status_code=400, detail="provide 'role' and/or 'email'")

    updated = None
    if "email" in body:
        email = (body.get("email") or "").strip()
        if email and ("@" not in email or "." not in email.rsplit("@", 1)[-1]):
            raise HTTPException(status_code=400, detail="invalid email address")
        updated = await db_module.update_user_email(user_id, user["org_id"], email)
    if "role" in body:
        role = (body.get("role") or "").strip()
        if role not in ("admin", "member"):
            raise HTTPException(status_code=400, detail="role must be 'admin' or 'member'")
        updated = await db_module.update_user_role(user_id, user["org_id"], role)

    if not updated:
        raise HTTPException(status_code=404, detail="User not found in org")
    return {"user": {
        "id":           updated["id"],
        "username":     updated["username"],
        "display_name": updated["display_name"],
        "email":        updated.get("email"),
        "role":         updated.get("role"),
    }}


@router.delete("/{user_id}")
async def delete_user(user_id: int, user: dict = Depends(current_user)):
    """Remove a user from the org (admin only). Cannot delete your own account."""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    deleted = await db_module.delete_user(user_id, user["org_id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found in org")
    return {"ok": True}
