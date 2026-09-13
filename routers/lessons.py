"""
routers/lessons.py — cross-site navigation lessons (WP4).

Lessons are PROPOSED by a periodic LLM pass (playbook.lessons_propose, run by
the weekly `lessons_review` heartbeat or on demand here) and only reach a
future task after a human approves them — this router is the approve/reject
surface. Nothing here ever auto-approves.

  GET  /api/agents/lessons                (any member)  grouped by status
  POST /api/agents/lessons/{id}/decision  (admin)        {"decision": "approve"|"reject"}
  POST /api/agents/lessons/review         (admin)        run lessons_propose now
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

import playbook
from context import DB_AVAILABLE, cache_clear
from routers.auth import current_user

logger = logging.getLogger("wk.lessons")
router = APIRouter()


@router.get("/api/agents/lessons")
async def list_lessons(user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="Database unavailable")
    lessons = await playbook.lessons_load(user["org_id"])
    grouped: dict = {"proposed": [], "approved": [], "rejected": []}
    for lesson in lessons:
        grouped.setdefault(lesson.get("status", "proposed"), []).append(lesson)
    return grouped


@router.post("/api/agents/lessons/{lesson_id}/decision")
async def decide_lesson(lesson_id: str, body: dict, user: dict = Depends(current_user)):
    """Approve or reject a proposed lesson — admin only (pattern: org_settings.py)."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="Database unavailable")
    decision = str((body or {}).get("decision") or "").strip().lower()
    if decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")
    try:
        lesson = await playbook.lessons_decide(user["org_id"], lesson_id, decision, user["id"])
    except KeyError:
        raise HTTPException(status_code=404, detail="lesson not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    cache_clear(user["org_id"])
    return {"ok": True, "lesson": lesson}


@router.post("/api/agents/lessons/review")
async def review_now(user: dict = Depends(current_user)):
    """Run the weekly proposal pass immediately — admin only."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="Database unavailable")
    result = await playbook.lessons_propose(user["org_id"])
    cache_clear(user["org_id"])
    return {"ok": True, **result}
