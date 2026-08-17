"""
POST /api/feedback — accepts user feedback, saves to DB, fires Telegram.
No auth required; user/org included in metadata if a valid session token is present.
"""

import asyncio
import logging
from uuid import uuid4

from fastapi import APIRouter, Request

import context
from context import DB_AVAILABLE, db_module

logger = logging.getLogger("wk.feedback")
router = APIRouter()


async def _resolve_user(token: str) -> tuple[int | None, int | None, str | None]:
    """Return (user_id, org_id, username) from a bearer token, or (None, None, None)."""
    if not token or not DB_AVAILABLE or not db_module._pool:
        return None, None, None
    try:
        async with db_module._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT u.id, u.org_id, u.username
                FROM user_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token = $1 AND s.expires_at > NOW()
                LIMIT 1
                """,
                token,
            )
        if row:
            return row["id"], row["org_id"], row["username"]
    except Exception as exc:
        logger.debug("feedback: token lookup failed: %s", exc)
    return None, None, None


@router.post("/api/feedback")
async def submit_feedback(request: Request):
    body = await request.json()
    subject = (body.get("subject") or "").strip()
    message = (body.get("message") or "").strip()
    if not subject or not message:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="subject and message are required")

    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    user_id, org_id, username = await _resolve_user(token)
    referer = request.headers.get("Referer", "")

    if org_id and DB_AVAILABLE:
        try:
            doc_id = f"feedback-{uuid4().hex[:12]}"
            embedding = await db_module.embed_text(f"{subject}\n{message}")
            await db_module.index_document(
                org_id=org_id,
                doc_id=doc_id,
                doc_type="feedback",
                title=subject,
                content=message,
                metadata={"submitted_by": username or "anonymous", "page": referer},
                embedding=embedding or [],
                source="user",
                created_by=user_id,
            )
        except Exception as exc:
            logger.warning("feedback: DB write failed: %s", exc)

    try:
        import notifications as _notify
        who = username or "anonymous"
        org_label = f" (org {org_id})" if org_id else ""
        text = f"\U0001f4ac Feedback from {who}{org_label}\nSubject: {subject}\n\n{message}"
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _notify.notify, text)
    except Exception as exc:
        logger.warning("feedback: Telegram notify failed: %s", exc)

    return {"ok": True}
