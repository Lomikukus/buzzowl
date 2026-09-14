"""
intake.py — Intake orchestrator: a collection point for a new (or re-triggered)
client's four research parts (osint, research, jobs, news) so the account brief
is written once, from everything we have, instead of racing a brief per finished
part off the first HTTP callback that happens to land (see WP5 in
mutable-greeting-clock.md for the full design).

State lives in `clients.metadata.intake` (see `start()` for the shape). It is
written with `db.set_client_intake_path` (jsonb_set), never with the shallow
`db.update_client_metadata` merge — two parts finishing within the same second
would otherwise clobber each other's nested state.

Server is a single `uvicorn.run` process with no workers, so the DB
compare-and-set in `_finish` (via `db.cas_client_intake_brief`) plus this
module's straight-line async functions are enough to make "write the brief
exactly once" safe under real concurrency — no extra in-process locking needed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from context import config, db_module

logger = logging.getLogger("wk.intake")

PARTS = ("osint", "research", "jobs", "news")
_TERMINAL_PART_STATES = ("done", "failed")
_TERMINAL_BRIEF_STATES = ("written", "refreshed", "failed")

# Caps concurrent LLM-bound calls made by the intake pipeline (jobs scan, news
# scan, brief generation) so they don't pile on top of the two Pi agent slots
# and blow through the ChatGPT-subscription bridge's rate limit.
_INTAKE_LLM_SEM = asyncio.Semaphore(2)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _empty_part() -> dict:
    return {"status": "queued", "run_id": None, "started_at": None, "done_at": None, "error": None}


def _empty_brief() -> dict:
    return {"status": "waiting", "written_at": None, "missing": [], "refreshed_at": None, "error": None}


def _missing_parts(parts: dict) -> list[str]:
    """Parts not (successfully) done, for the 'partial — missing: …' marker.
    A failed part is named with its reason; anything still queued/running is
    just named (its status chip already shows it's in flight)."""
    out = []
    for p in PARTS:
        st = parts.get(p) or {}
        status = st.get("status")
        if status == "failed":
            out.append(f"{p} (failed: {st.get('error') or 'unknown error'})")
        elif status != "done":
            out.append(p)
    return out


def _part_done_after(part_state: dict, ref: Optional[datetime]) -> bool:
    """True when `part_state` finished successfully (status == 'done') strictly
    after `ref` (or `ref` is None, i.e. there's nothing to compare against
    yet). Deliberately excludes 'failed': a late failure doesn't add anything
    new to fold into the brief text — it only updates `missing` from the live
    part state — so it must not by itself trigger a full LLM regeneration
    (see _maybe_finish's 'partial' branch, and its `force` path for how an
    all-failed conclusion still gets closed out)."""
    if part_state.get("status") != "done":
        return False
    if ref is None:
        return True
    done_at = _parse_iso(part_state.get("done_at"))
    return bool(done_at and done_at > ref)


def is_active(meta: Optional[dict]) -> bool:
    """True while a client's intake is still collecting — i.e. its brief has
    not reached a terminal state yet (written/refreshed/failed). A 'partial'
    brief is always active, INCLUDING the case where every part happens to be
    terminal already: that's a legitimate, temporary state (see _finish()'s
    handling of a part landing while its own LLM call was still running) that
    is waiting for exactly one more refresh pass, not a state to short-circuit
    out of here — sweep()'s all-terminal check plus the one-time-refresh guard
    in _maybe_finish are what actually close it out. (An earlier version of
    this function special-cased 'partial + all parts terminal' as inactive,
    defensively, because _finish used to compute 'missing'/all-terminal from a
    stale pre-write snapshot and could leave exactly that combination stuck
    forever with no other mechanism to revisit it. Now that _finish recomputes
    from the current state right before writing, that combination is expected
    and self-resolving — this function no longer needs to lie about it.)
    Used to gate part_done()/note_run_started() (no-op once the collection
    point has closed) and to pick the legacy vs. intake-aware branch in
    agents._brief_then_match."""
    intake = (meta or {}).get("intake") or {}
    if not intake:
        return False
    brief = intake.get("brief") or {}
    status = brief.get("status")
    if not status:
        # A NULL/missing status can't match list_clients_with_open_intake()'s
        # `= ANY(['waiting','writing','partial'])` WHERE clause (NULL = ANY
        # is NULL, never TRUE) — treat it as inactive here too, or sweep()
        # could disagree with is_active() about whether this client's
        # collection point is still open.
        return False
    return status not in _TERMINAL_BRIEF_STATES


def summary(meta: Optional[dict]) -> dict:
    """UI-facing view of the intake state for GET /api/clients/{name}/intake
    and static/client.html's #intakeStrip.

    `percent` counts terminal parts (done OR failed — a failed part isn't
    still "in progress") plus the brief itself as one more stage, out of
    len(PARTS) + 1 total: a written/refreshed/failed brief with all four
    parts terminal reads 100%, not 80%."""
    intake = (meta or {}).get("intake") or {}
    parts_state = intake.get("parts") or {}
    parts = {p: (parts_state.get(p) or _empty_part()) for p in PARTS}
    brief = intake.get("brief") or _empty_brief()
    terminal_parts = sum(1 for p in PARTS if parts[p].get("status") in _TERMINAL_PART_STATES)
    brief_done = 1 if brief.get("status") in _TERMINAL_BRIEF_STATES else 0
    total_stages = len(PARTS) + 1
    return {
        "active": is_active(meta),
        "trigger": intake.get("trigger"),
        "started_at": intake.get("started_at"),
        "deadline_at": intake.get("deadline_at"),
        "attempt": intake.get("attempt", 1),
        "parts": parts,
        "brief": brief,
        "percent": round((terminal_parts + brief_done) / total_stages * 100) if total_stages else 0,
    }


async def _has_match_report(org_id: int, client_name: str) -> bool:
    """Any match_report at all for this client (not just a recent one — a
    refresh should not re-run matching just because the brief changed)."""
    if not db_module._pool:
        return False
    async with db_module._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT d.id FROM documents d
            JOIN document_links dl ON dl.document_id = d.id AND dl.entity_type = 'client'
            JOIN clients c ON c.id = dl.entity_id
            WHERE d.org_id = $1 AND c.name ILIKE $2 AND d.type = 'match_report'
            LIMIT 1
            """,
            org_id, client_name,
        )
    return row is not None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def start(org_id: int, client_name: str, *, trigger: str,
                 osint_run_id: Optional[int] = None, research_run_id: Optional[int] = None) -> dict:
    """Open a client's intake: write the full `intake` object once (so every
    nested path later writers touch via set_client_intake_path already exists),
    pre-create the two Python-part run rows, and fire all four parts in the
    background. Callers (knowledge.create_client, trigger_client_research,
    internal_create_client) pre-create the osint/research run rows themselves
    (their IDs are returned to CSV-import callers for polling) and pass them in.
    """
    jobs_run_id = await db_module.create_agent_run(
        org_id=org_id, agent_type="jobs_scan",
        task=f"Jobs scan: {client_name}", trigger_type="event_hook",
    )
    news_run_id = await db_module.create_agent_run(
        org_id=org_id, agent_type="news_scan",
        task=f"News scan: {client_name}", trigger_type="event_hook",
    )
    run_ids = {
        "osint": osint_run_id or None, "research": research_run_id or None,
        "jobs": jobs_run_id or None, "news": news_run_id or None,
    }
    intake_state = {
        "version": 1, "trigger": trigger, "started_at": _iso(_now()),
        "deadline_at": None, "attempt": 1,
        "parts": {p: {**_empty_part(), "run_id": run_ids[p]} for p in PARTS},
        "brief": _empty_brief(),
    }
    await db_module.update_client_metadata(org_id, client_name, {"intake": intake_state})

    from routers.pipeline import _trigger_osint, _trigger_research
    asyncio.create_task(_trigger_osint(client_name, org_id, run_id=osint_run_id or None))
    asyncio.create_task(_trigger_research(client_name, org_id, run_id=research_run_id or None))
    asyncio.create_task(_run_python_part(org_id, client_name, "jobs"))
    asyncio.create_task(_run_python_part(org_id, client_name, "news"))
    return intake_state


async def note_run_started(org_id: int, client_name: str, db_run_id: int) -> None:
    """Watcher hook: called the first time agent-pi reports a Pi-backed part
    (osint/research) as actually `running` — i.e. past its FIFO slot, not just
    dispatched. This is deliberately what starts the 25-minute deadline clock:
    agent-pi has only 2 slots, so a newly created client's parts can sit queued
    behind other clients' runs for a while, and starting the clock at DB-row
    creation would hand queued clients bogus partial briefs.

    Note: sweep()'s lost-callback rescue (part_done() called from a stale
    agent_runs row instead of a live callback/watcher) does NOT go through
    this function, so it never sets deadline_at either. That's acceptable —
    a client whose Pi parts are only ever discovered via that rescue path
    still gets closed out eventually by the absolute cap, just not by the
    (tighter) 25-minute deadline."""
    client = await db_module.get_client(org_id, client_name)
    if not client or not is_active(client.get("metadata")):
        return
    intake = (client.get("metadata") or {}).get("intake") or {}
    parts = intake.get("parts") or {}
    part = next((p for p in PARTS if (parts.get(p) or {}).get("run_id") == db_run_id), None)
    if part is None:
        return
    part_state = parts.get(part) or {}
    if part_state.get("status") != "queued":
        return  # already running/terminal — nothing to do (idempotent)
    now = _now()
    await db_module.set_client_intake_path(
        org_id, client_name, ["intake", "parts", part],
        {**part_state, "status": "running", "started_at": _iso(now)},
    )
    if not intake.get("deadline_at"):
        deadline_min = config.get("intake_deadline_min", 25)
        await db_module.set_client_intake_path(
            org_id, client_name, ["intake", "deadline_at"], _iso(now + timedelta(minutes=deadline_min)),
        )


async def part_done(org_id: int, client_name: str, part: str, status: str, *,
                     run_id: Optional[int] = None, error: Optional[str] = None) -> None:
    """Record one part's outcome. A no-op when: there is no active intake for
    this client; `run_id` is given and doesn't match the part's own run (a
    stale callback from an earlier attempt/retry); or the part is already
    terminal. That last case is what makes this safe to call twice for the
    same event — the HTTP callback and the watcher's terminal poll both call
    this for the same research/osint run, and sweep()'s lost-callback rescue
    may call it again after either of those already landed."""
    client = await db_module.get_client(org_id, client_name)
    if not client or not is_active(client.get("metadata")):
        return
    intake = (client.get("metadata") or {}).get("intake") or {}
    parts = intake.get("parts") or {}
    part_state = parts.get(part)
    if part_state is None:
        return
    if run_id is not None and part_state.get("run_id") is not None and part_state.get("run_id") != run_id:
        return
    if part_state.get("status") in _TERMINAL_PART_STATES:
        return

    updated = await db_module.set_client_intake_path(
        org_id, client_name, ["intake", "parts", part],
        {**part_state, "status": status, "done_at": _iso(_now()), "error": error},
    )
    if updated is None:
        return
    await _maybe_finish(org_id, client_name, updated)


async def _run_python_part(org_id: int, client_name: str, part: str) -> None:
    """Run a Python-side intake part (jobs or news) and report the outcome via
    part_done(). `_scan_client_jobs` (WP2) and `_client_news_scan` (WP3) are
    both real now, imported lazily here (as routers.pipeline itself does
    elsewhere) to avoid an import cycle at module load time — an
    ImportError/AttributeError from either import is treated as that part
    having failed, never as a crash, in case either ever goes missing again
    (e.g. mid-refactor on some other branch)."""
    client = await db_module.get_client(org_id, client_name)
    if not client:
        await part_done(org_id, client_name, part, "failed", error="client not found")
        return
    intake = (client.get("metadata") or {}).get("intake") or {}
    part_state = (intake.get("parts") or {}).get(part) or _empty_part()
    run_id = part_state.get("run_id")

    await db_module.set_client_intake_path(
        org_id, client_name, ["intake", "parts", part],
        {**part_state, "status": "running", "started_at": _iso(_now())},
    )
    if run_id:
        await db_module.update_agent_run(run_id, "running")

    error: Optional[str] = None
    result: dict = {}
    async with _INTAKE_LLM_SEM:
        try:
            if part == "jobs":
                from routers.pipeline import _scan_client_jobs
                result = await _scan_client_jobs(org_id, client, run_id=run_id) or {}
            else:
                from routers.pipeline import _client_news_scan
                result = await _client_news_scan(org_id, client, run_id=run_id) or {}
        except (ImportError, AttributeError):
            # Defensive: the function should always be importable now that
            # WP2/WP3 are merged, but map a missing one to a short,
            # human-readable reason rather than a raw Python exception
            # message ending up verbatim in the brief's missing-parts line.
            error = f"{part} scan not available"
        except TypeError as exc:
            # Two very different causes share this exception type: a stale
            # call signature ("not available", same as above — defensive,
            # shouldn't happen once callers stay in sync with WP2/WP3's
            # signatures) vs. a genuine TypeError raised from inside an
            # otherwise-working scan (a real bug, not a missing feature).
            # Only the former should be swallowed as "not available"; the
            # latter must surface like any other scan failure below, not get
            # mislabelled.
            msg = str(exc)
            if "unexpected keyword argument" in msg or "positional argument" in msg:
                error = f"{part} scan not available"
            else:
                error = msg
        except Exception as exc:  # never crash the intake pipeline over a scan bug
            error = str(exc)
    if error is None:
        error = result.get("error")

    status = "failed" if error else "done"
    if run_id:
        await db_module.update_agent_run(run_id, status, output=result or None, error=error)
    await part_done(org_id, client_name, part, status, run_id=run_id, error=error)


# ---------------------------------------------------------------------------
# Finishing the collection point
# ---------------------------------------------------------------------------

async def _maybe_finish(org_id: int, client_name: str, meta: dict, *, force: bool = False) -> None:
    """Decide whether to (re)write the brief now. Several callers — concurrent
    part_done() calls, and the periodic sweep() — can all reach "yes" for the
    same client at nearly the same time; _finish()'s CAS on brief.status is
    what keeps that to exactly one actual write."""
    intake = (meta or {}).get("intake") or {}
    if not intake:
        return
    brief = intake.get("brief") or {}
    status = brief.get("status")
    if status not in ("waiting", "partial"):
        return  # writing/written/refreshed/failed — nothing left to decide

    parts = intake.get("parts") or {}
    all_terminal = all((parts.get(p) or {}).get("status") in _TERMINAL_PART_STATES for p in PARTS)
    deadline = _parse_iso(intake.get("deadline_at"))
    deadline_passed = bool(deadline and _now() >= deadline)

    if status == "partial":
        if brief.get("refreshed_at") is not None:
            return  # the one-time refresh already happened
        written_at = _parse_iso(brief.get("written_at"))
        became_done_since = any(_part_done_after(parts.get(p) or {}, written_at) for p in PARTS)
        if became_done_since:
            # A part genuinely finished (successfully) since the partial
            # brief was written — this is "the" one-time refresh, a nicer
            # regeneration triggered by real new information.
            await _finish(org_id, client_name, missing=_missing_parts(parts), refresh=True)
            return
        if all_terminal:
            # Nothing became done since written_at (that's the branch above)
            # but every part is now terminal anyway — so whatever got us here
            # was a late FAILURE on the last part still pending. There's
            # nothing new to regenerate the brief text over (see
            # _part_done_after's docstring), but the brief still needs
            # closing out: is_active() treats every 'partial' as active, so
            # leaving it 'partial' forever would keep this client swept every
            # 60s until the 90-minute absolute cap and the client page
            # polling /intake every 5s the whole time, with `missing` stuck
            # on stale "still running"/"queued" wording instead of the real
            # "(failed: ...)" reason. Patch the brief in place — no LLM call —
            # and close it out.
            updated = await db_module.cas_client_intake_brief(org_id, client_name, ["partial"], "written")
            if updated is not None:
                await db_module.set_client_intake_path(
                    org_id, client_name, ["intake", "brief"],
                    {**brief, "status": "written", "missing": _missing_parts(parts), "closed_at": _iso(_now())},
                )
                # Same post-finish step a normal (non-refresh) _finish call
                # does, so the match report still happens off this brief.
                try:
                    from routers.agents import _maybe_trigger_pain_point_research
                    await _maybe_trigger_pain_point_research(org_id, client_name)
                except Exception as exc:
                    logger.warning("intake._maybe_finish: match trigger failed for '%s': %s", client_name, exc)
            return
        if force:
            # Fallback for the absolute cap forcing this while all_terminal
            # is somehow still False (e.g. sweep()'s pre-mark-as-failed write
            # above didn't land) — not a natural completion, so treat it as a
            # plain finish rather than "the" refresh. In the ordinary case
            # (pre-marking succeeded) the all_terminal branch above already
            # closed this out without reaching here.
            await _finish(org_id, client_name, missing=_missing_parts(parts), refresh=False)
            return
        return  # nothing new since the partial brief was written

    if all_terminal or deadline_passed or force:
        # Whether this genuinely concludes (every part done/failed) or is a
        # deadline/cap cutting collection short with parts still
        # queued/running, _finish decides which by recomputing from the
        # current state right before it writes (see its docstring) — the
        # `all_terminal` computed here is only used for this gate, not passed
        # on.
        await _finish(org_id, client_name, missing=_missing_parts(parts), refresh=False)


async def _finish(org_id: int, client_name: str, *, missing: list[str], refresh: bool = False) -> None:
    """Write (or refresh) the brief exactly once. `db.cas_client_intake_brief`
    is the single-flight gate: only the caller that wins the waiting|partial →
    writing transition actually calls _auto_generate_brief.

    `missing` as passed in is a snapshot from BEFORE this call's own (up to
    180s) _auto_generate_brief — and a part can legitimately finish while
    that's running: part_done() keeps working the whole time (is_active() is
    still True while brief.status == 'writing'; it's only _maybe_finish that
    no-ops on a 'writing' brief, deliberately, so it doesn't race this very
    write). So right before writing the brief, the CURRENT parts are re-read
    from the DB and `missing`/`all_terminal` are both recomputed from that —
    the `missing` parameter is never trusted for the write itself, only
    passed to _auto_generate_brief as the prompt's "as of when we started"
    context, and `refresh` is the only caller-supplied signal that survives
    into the write (it says whether the caller intended this as the one-time
    refresh, not whether it succeeds at being final — see below).

    If a part reached 'done' strictly *after* our own CAS-win — i.e. during
    this call's own generation, not something the caller already knew about —
    the brief text we just generated doesn't reflect it yet. Even if that
    happens to make every part terminal now, this write stays 'partial' (with
    the recomputed `missing`, `refreshed_at` untouched) instead of finalizing
    on stale content: the ordinary one-time-refresh path (_maybe_finish's
    'partial' branch, driven by sweep or a stray part_done) picks it up next
    and produces a properly-refreshed brief once this generation is out of
    the way. The same applies if, after recomputing, something is *still* not
    terminal (only relevant for a forced/refresh call) — that can't be final
    either. Only when nothing new landed during this call AND everything is
    genuinely terminal now does the simple rule apply: 'refreshed' if this
    call was itself the one-time refresh, else 'written'."""
    updated = await db_module.cas_client_intake_brief(org_id, client_name, ["waiting", "partial"], "writing")
    if updated is None:
        return  # lost the race — another caller is already handling this

    now = _iso(_now())
    cas_won_at = _parse_iso(now)
    await db_module.set_client_intake_path(org_id, client_name, ["intake", "brief", "entered_writing_at"], now)

    from routers.knowledge import _auto_generate_brief
    try:
        async with _INTAKE_LLM_SEM:
            ok = await _auto_generate_brief(org_id, client_name, partial_missing=missing or None)
    except Exception as exc:
        ok = False
        logger.warning("intake._finish: brief generation raised for '%s': %s", client_name, exc)

    intake = updated.get("intake") or {}
    prior_brief = intake.get("brief") or {}
    attempt = intake.get("attempt", 1)

    if ok:
        # Recompute from the current state — never trust the pre-CAS snapshot
        # the caller decided to finish on (see docstring above).
        current_client = await db_module.get_client(org_id, client_name)
        current_meta = (current_client or {}).get("metadata") or {}
        current_parts = (current_meta.get("intake") or {}).get("parts") or intake.get("parts") or {}
        all_terminal_now = all((current_parts.get(p) or {}).get("status") in _TERMINAL_PART_STATES for p in PARTS)
        missing_now = _missing_parts(current_parts)
        landed_during_write = any(_part_done_after(current_parts.get(p) or {}, cas_won_at) for p in PARTS)

        if landed_during_write or not all_terminal_now:
            target = "partial"
            refreshed_at = prior_brief.get("refreshed_at")
        else:
            target = "refreshed" if refresh else "written"
            refreshed_at = now if refresh else prior_brief.get("refreshed_at")

        brief_patch = {
            "status": target,
            "written_at": now,
            "missing": missing_now,
            "refreshed_at": refreshed_at,
            "error": None,
        }
        await db_module.set_client_intake_path(org_id, client_name, ["intake", "brief"], brief_patch)
        if landed_during_write:
            # Not really final — the content is already known-stale, so don't
            # match against it. The follow-up refresh's own finalize (once it
            # lands with nothing new arriving mid-write) triggers the match.
            return
        if not (refresh and await _has_match_report(org_id, client_name)):
            try:
                from routers.agents import _maybe_trigger_pain_point_research
                await _maybe_trigger_pain_point_research(org_id, client_name)
            except Exception as exc:
                logger.warning("intake._finish: match trigger failed for '%s': %s", client_name, exc)
        return

    new_attempt = attempt + 1
    if new_attempt > 3:
        brief_patch = {
            "status": "failed",
            "written_at": prior_brief.get("written_at"),
            "missing": missing,
            "refreshed_at": prior_brief.get("refreshed_at"),
            "error": "brief generation failed after 3 attempts",
        }
    else:
        brief_patch = {**prior_brief, "status": "partial" if refresh else "waiting",
                        "error": "brief generation failed"}
    await db_module.set_client_intake_path(org_id, client_name, ["intake", "brief"], brief_patch)
    await db_module.set_client_intake_path(org_id, client_name, ["intake", "attempt"], new_attempt)


# ---------------------------------------------------------------------------
# Sweeper (APScheduler interval job — see routers/pipeline.py)
# ---------------------------------------------------------------------------

async def sweep() -> None:
    """Runs every 60s. Three independent jobs per client with an open intake:
    (1) a 'writing' brief stuck for >10 min (the process died mid-_finish) is
        reverted to 'waiting' so the next event retries it;
    (2) a part whose agent_runs row already finished (done/failed) while our
        copy of intake.parts still shows it queued/running is reconciled via
        part_done() — the fallback for a lost HTTP callback. Note this path
        never calls note_run_started(), so it never sets deadline_at either;
        see that function's docstring;
    (3) otherwise, re-check whether the deadline, the absolute cap, or plain
        all-terminal-ness (parts can all finish without a deadline ever being
        set — e.g. right after (1) resets a stuck 'writing' brief) means the
        brief should be (re)written now — or a partial brief is due its
        one-time refresh."""
    clients = await db_module.list_clients_with_open_intake()
    now = _now()
    absolute_cap_min = config.get("intake_absolute_cap_min", 90)

    for row in clients:
        org_id = row.get("org_id")
        name = row.get("name")
        meta = row.get("metadata") or {}
        intake = meta.get("intake") or {}
        if not intake:
            continue
        # list_clients_with_open_intake()'s WHERE already restricts to
        # waiting/writing/partial, all of which is_active() now always reads
        # as active — this is just a cheap belt-and-suspenders re-check.
        if not is_active(meta):
            continue
        brief = intake.get("brief") or {}

        if brief.get("status") == "writing":
            entered = _parse_iso(brief.get("entered_writing_at"))
            # A missing timestamp means _finish crashed between winning the CAS
            # and writing entered_writing_at — there's no way to tell how long
            # ago that was, so treat it as stale right away rather than waiting
            # on a timestamp that will never arrive.
            if entered is None or now - entered > timedelta(minutes=10):
                await db_module.set_client_intake_path(
                    org_id, name, ["intake", "brief"],
                    {**brief, "status": "waiting", "entered_writing_at": None},
                )
            continue

        parts = intake.get("parts") or {}
        rescued = False
        for part in PARTS:
            st = parts.get(part) or {}
            if st.get("status") in _TERMINAL_PART_STATES:
                continue
            run_id = st.get("run_id")
            if not run_id:
                continue
            run_row = await db_module.get_agent_run(run_id)
            if run_row and run_row.get("status") in ("done", "failed"):
                await part_done(org_id, name, part, run_row["status"], run_id=run_id,
                                 error=run_row.get("error"))
                rescued = True
        if rescued:
            continue  # part_done() already re-evaluated whether to finish

        started_at = _parse_iso(intake.get("started_at"))
        deadline = _parse_iso(intake.get("deadline_at"))
        cap_passed = bool(started_at and now - started_at > timedelta(minutes=absolute_cap_min))
        all_terminal = all((parts.get(p) or {}).get("status") in _TERMINAL_PART_STATES for p in PARTS)

        # The absolute cap is checked independently of the deadline branch —
        # once deadline_at is set (any part reached 'running'), the deadline
        # branch below stays true forever (now >= deadline never goes back to
        # false, and a still-partial brief keeps matching too), so nesting the
        # cap as an elif after it would make the cap unreachable exactly when
        # a client has been stuck the longest.
        if cap_passed:
            # Force-finish: anything still queued/running past the absolute
            # cap is timed out — mark it 'failed' up front so _finish's own
            # post-generation recompute (see its docstring) lands on a
            # genuine all_terminal=True and this ends in ONE write, instead
            # of repeating every sweep tick with the same stale 'missing'
            # (which used to get mislabelled 'refreshed' on the second pass).
            updated_meta = meta
            for part in PARTS:
                st = parts.get(part) or {}
                if st.get("status") in _TERMINAL_PART_STATES:
                    continue
                result = await db_module.set_client_intake_path(
                    org_id, name, ["intake", "parts", part],
                    {**st, "status": "failed", "done_at": _iso(now), "error": "timed out (absolute cap)"},
                )
                if result is not None:
                    updated_meta = result
            await _maybe_finish(org_id, name, updated_meta, force=True)
        elif all_terminal or (deadline is not None and (now >= deadline or brief.get("status") == "partial")):
            await _maybe_finish(org_id, name, meta)
