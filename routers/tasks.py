"""
routers/tasks.py — user to-do / follow-up tasks (Deploy 2).

Reps create tasks manually, from a contact follow-up date, or via the chat agent.
The Home "My Tasks" card lists them, the NBA queue prioritises focus-client tasks,
and a daily heartbeat (task_reminder) emails each rep their due/overdue tasks.
"""

import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

import reminders as rm
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
    for k in ("due_date", "completed_at", "created_at", "updated_at", "snooze_until"):
        if t.get(k) is not None and hasattr(t[k], "isoformat"):
            t[k] = t[k].isoformat()
    t["snoozed"] = rm.is_snoozed(t.get("snooze_until") and datetime.fromisoformat(t["snooze_until"]))
    return t


def _recurrence(v) -> Optional[str]:
    try:
        return rm.validate_recurrence(v)
    except rm.ReminderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _snooze(v) -> Optional[datetime]:
    try:
        return rm.parse_snooze(v)
    except rm.ReminderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def _resolve_deal(org_id: int, deal_id) -> Optional[dict]:
    """deal_id → deal row in this org, 404 if unknown. None/'' → None (unlink)."""
    if deal_id in (None, "", 0, "0"):
        return None
    try:
        did = int(deal_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="deal_id must be an integer")
    d = await db_module.get_deal(org_id, did)
    if not d:
        raise HTTPException(status_code=404, detail="deal not found")
    return d


async def _spawn_next_occurrence(row: dict, org_id: int) -> Optional[dict]:
    """A recurring task was completed → create its next instance (same title/notes/
    client/deal/priority, due date advanced past today). Returns the new row."""
    rec = row.get("recurrence")
    if not rec:
        return None
    due = row.get("due_date")
    if isinstance(due, str):
        due = date.fromisoformat(due[:10])
    nxt = rm.next_due(due, rec, date.today())
    return await db_module.create_task(
        org_id, row.get("user_id"), row.get("title") or "Task",
        client_name=row.get("client_name"), notes=row.get("notes"), due_date=nxt,
        priority=int(row.get("priority") or 5), source="recurrence",
        recurrence=rec, deal_id=row.get("deal_id"),
    )


def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


@router.get("/api/tasks")
async def list_tasks(mine: bool = True, include_done: bool = False, include_snoozed: bool = False,
                     deal_id: Optional[int] = None, client_name: Optional[str] = None,
                     limit: int = 200, user: dict = Depends(current_user)):
    """Tasks for the Home 'My Tasks' card (this rep by default). Snoozed tasks are hidden
    unless include_snoozed=1; deal_id/client_name scope the list (pipeline / client page)."""
    _require_db()
    rows = await db_module.list_tasks(
        user["org_id"], user_id=user["id"] if mine else None,
        include_done=include_done, include_snoozed=include_snoozed,
        deal_id=deal_id, client_name=(client_name or None), limit=min(limit, 500),
    )
    return {"tasks": [_ser(r) for r in rows]}


@router.post("/api/tasks")
async def create_task(body: dict, user: dict = Depends(current_user)):
    """Create a to-do / follow-up."""
    _require_db()
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    deal = await _resolve_deal(user["org_id"], body.get("deal_id"))
    client_name = (body.get("client_name") or "").strip() or None
    if deal and not client_name:
        client_name = deal.get("client_name")
    row = await db_module.create_task(
        user["org_id"], user["id"], title,
        client_name=client_name,
        notes=(body.get("notes") or "").strip() or None,
        due_date=_parse_due(body.get("due_date")),
        priority=int(body.get("priority") or 5),
        source=(body.get("source") or "manual"),
        recurrence=_recurrence(body.get("recurrence")),
        snooze_until=_snooze(body.get("snooze") or body.get("snooze_until")),
        deal_id=deal["id"] if deal else None,
    )
    if not row:
        raise HTTPException(status_code=500, detail="could not create task")
    return {"ok": True, "task": _ser(row)}


@router.patch("/api/tasks/{task_id}")
async def patch_task(task_id: int, body: dict, user: dict = Depends(current_user)):
    """Update fields or toggle done/open. Completing a recurring task spawns the next
    occurrence (returned as `next_task`)."""
    _require_db()
    patch: dict = {}
    for k in ("title", "notes", "priority", "status", "client_name"):
        if k in body:
            patch[k] = body[k]
    if "due_date" in body:
        patch["due_date"] = _parse_due(body.get("due_date"))
    if "recurrence" in body:
        patch["recurrence"] = _recurrence(body.get("recurrence"))
    if "snooze" in body or "snooze_until" in body:
        patch["snooze_until"] = _snooze(body.get("snooze") if "snooze" in body else body.get("snooze_until"))
    if "deal_id" in body:
        deal = await _resolve_deal(user["org_id"], body.get("deal_id"))
        patch["deal_id"] = deal["id"] if deal else None
        if deal and deal.get("client_name") and "client_name" not in body:
            patch["client_name"] = deal["client_name"]
    if patch.get("status") and patch["status"] not in ("open", "done"):
        raise HTTPException(status_code=400, detail="status must be open|done")
    if patch.get("status") == "done":
        patch["snooze_until"] = None
    row = await db_module.update_task(task_id, user["org_id"], patch)
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    out = {"ok": True, "task": _ser(row)}
    if patch.get("status") == "done" and row.get("recurrence"):
        nxt = await _spawn_next_occurrence(row, user["org_id"])
        if nxt:
            out["next_task"] = _ser(nxt)
    return out


@router.post("/api/tasks/{task_id}/snooze")
async def snooze_task(task_id: int, body: dict, user: dict = Depends(current_user)):
    """Hide a task until later: {"until": "1d" | "3d" | "1w" | "tomorrow" | "next_week" | ISO}.
    The due date moves to at least the wake-up day so it does not resurface as overdue.
    {"until": null} clears the snooze."""
    _require_db()
    t = await db_module.get_task(task_id, user["org_id"])
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    until = _snooze(body.get("until"))
    patch: dict = {"snooze_until": until}
    if until is not None:
        wake_day = until.astimezone(timezone.utc).date()
        due = t.get("due_date")
        if due is None or due < wake_day:
            patch["due_date"] = wake_day
    row = await db_module.update_task(task_id, user["org_id"], patch)
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
            rec = f" &#8635; {t['recurrence']}" if t.get("recurrence") else ""
            items.append(
                f"<li><b>{_esc(t['title'])}</b>{_esc(client)} "
                f"<span style='color:#888'>due {due_str}{tag}{rec}</span></li>"
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
