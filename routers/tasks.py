"""
routers/tasks.py — user to-do / follow-up tasks (Deploy 2).

Reps create tasks manually, from a contact follow-up date, or via the chat agent.
The Home "My Tasks" card lists them, the NBA queue prioritises focus-client tasks,
and a daily heartbeat (task_reminder) emails each rep their due/overdue tasks.
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from context import DB_AVAILABLE, db_module
from routers.auth import current_user
import mailer

logger = logging.getLogger("wk.tasks")
router = APIRouter()


def _require_db() -> None:
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")


def _parse_due(v):
    """Accept 'YYYY-MM-DD' (or empty) -> date|None. 400 on a bad format."""
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise HTTPException(status_code=400, detail="due_date must be YYYY-MM-DD")


def _ser(t: dict) -> dict:
    for k in ("due_date", "completed_at", "created_at", "updated_at"):
        if t.get(k) is not None and hasattr(t[k], "isoformat"):
            t[k] = t[k].isoformat()
    return t


def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


@router.get("/api/tasks")
async def list_tasks(mine: bool = True, include_done: bool = False,
                     limit: int = 200, user: dict = Depends(current_user)):
    """Tasks for the Home 'My Tasks' card (this rep by default)."""
    _require_db()
    rows = await db_module.list_tasks(
        user["org_id"], user_id=user["id"] if mine else None,
        include_done=include_done, limit=min(limit, 500),
    )
    return {"tasks": [_ser(r) for r in rows]}


@router.post("/api/tasks")
async def create_task(body: dict, user: dict = Depends(current_user)):
    """Create a to-do / follow-up."""
    _require_db()
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    row = await db_module.create_task(
        user["org_id"], user["id"], title,
        client_name=(body.get("client_name") or "").strip() or None,
        notes=(body.get("notes") or "").strip() or None,
        due_date=_parse_due(body.get("due_date")),
        priority=int(body.get("priority") or 5),
        source=(body.get("source") or "manual"),
    )
    if not row:
        raise HTTPException(status_code=500, detail="could not create task")
    return {"ok": True, "task": _ser(row)}


@router.patch("/api/tasks/{task_id}")
async def patch_task(task_id: int, body: dict, user: dict = Depends(current_user)):
    """Update fields or toggle done/open."""
    _require_db()
    patch: dict = {}
    for k in ("title", "notes", "priority", "status", "client_name"):
        if k in body:
            patch[k] = body[k]
    if "due_date" in body:
        patch["due_date"] = _parse_due(body.get("due_date"))
    if patch.get("status") and patch["status"] not in ("open", "done"):
        raise HTTPException(status_code=400, detail="status must be open|done")
    row = await db_module.update_task(task_id, user["org_id"], patch)
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    return {"ok": True, "task": _ser(row)}


@router.delete("/api/tasks/{task_id}")
async def remove_task(task_id: int, user: dict = Depends(current_user)):
    _require_db()
    ok = await db_module.delete_task(task_id, user["org_id"])
    if not ok:
        raise HTTPException(status_code=404, detail="task not found")
    return {"ok": True}


async def send_task_reminders(org_id: int) -> str:
    """Heartbeat body: email each rep the open tasks they have due today/overdue."""
    if not DB_AVAILABLE:
        return "DB unavailable"
    rows = await db_module.list_reminder_tasks(org_id)
    if not rows:
        return "no due tasks"
    by_user: dict = {}
    for r in rows:
        by_user.setdefault(r["user_id"], []).append(r)
    sent, skipped = 0, 0
    today = date.today()
    for _uid, tasks in by_user.items():
        email = (tasks[0].get("user_email") or "").strip()
        name = tasks[0].get("user_name") or "there"
        if not email:
            skipped += 1
            continue
        items = []
        for t in tasks:
            due = t.get("due_date")
            overdue = bool(due and due < today)
            due_str = due.isoformat() if due else ""
            tag = " (overdue)" if overdue else " (today)"
            client = f" — {t['client_name']}" if t.get("client_name") else ""
            items.append(
                f"<li><b>{_esc(t['title'])}</b>{_esc(client)} "
                f"<span style='color:#888'>due {due_str}{tag}</span></li>"
            )
        html = (
            f"<p>Hi {_esc(name)},</p>"
            f"<p>You have {len(tasks)} task(s) due today or overdue:</p>"
            f"<ul>{''.join(items)}</ul>"
            f"<p style='color:#888;font-size:0.85em'>— Buzzowl</p>"
        )
        ok, _msg = mailer.send_email(
            email, f"⏰ {len(tasks)} task(s) due — Buzzowl", html
        )
        sent += 1 if ok else 0
        skipped += 0 if ok else 1
    return f"reminders emailed to {sent} rep(s), {skipped} skipped"
