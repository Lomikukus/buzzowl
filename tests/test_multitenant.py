"""Multi-tenant hardening (Phase 6a): org-scoped event bus, cache invalidation, signup flag."""

import asyncio

import pytest

import context
from agents import events


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, msg):
        self.sent.append(msg)


class _DeadWS:
    async def send_text(self, msg):
        raise RuntimeError("closed")


@pytest.fixture(autouse=True)
def _clean_bus():
    events._subscribers.clear()
    yield
    events._subscribers.clear()


def test_emit_routes_by_org():
    a, b, legacy = _FakeWS(), _FakeWS(), _FakeWS()
    events.subscribe(a, 8)
    events.subscribe(b, 9)
    events.subscribe(legacy, None)          # unknown org (dev/no-DB) still gets everything
    asyncio.run(events.emit({"org_id": 8, "type": "task_claimed", "subject": "Acme"}))
    assert len(a.sent) == 1 and len(b.sent) == 0 and len(legacy.sent) == 1
    asyncio.run(events.emit({"type": "system"}))   # no org → broadcast
    assert len(a.sent) == 2 and len(b.sent) == 1 and len(legacy.sent) == 2


def test_emit_drops_dead_sockets():
    dead, live = _DeadWS(), _FakeWS()
    events.subscribe(dead, 8)
    events.subscribe(live, 8)
    asyncio.run(events.emit({"org_id": 8, "type": "x"}))
    assert dead not in events._subscribers and live.sent


def test_cache_clear_is_org_scoped():
    context._ttl_cache.clear()
    context.cache_set(("clients", 8), ["a"], ttl=60)
    context.cache_set(("clients", 9), ["b"], ttl=60)
    context.cache_set(("products", 8, "x"), ["c"], ttl=60)
    context.cache_clear(8)
    assert context.cache_get(("clients", 8)) is None
    assert context.cache_get(("products", 8, "x")) is None
    assert context.cache_get(("clients", 9)) == ["b"]
    context.cache_clear()
    assert context.cache_get(("clients", 9)) is None


def test_signup_status_follows_config(monkeypatch):
    from routers import auth
    monkeypatch.setattr(context, "config", {"hosted": {"signup_enabled": True}})
    assert auth._signup_open() is True
    monkeypatch.setattr(context, "config", {})
    assert auth._signup_open() is False


def test_research_worker_pool_accepts_all_orgs():
    """run_research_workers(None) is the multi-tenant default; the loop uses the task's org."""
    import inspect
    from agents import research_runner as rr
    sig = inspect.signature(rr.run_research_workers)
    assert sig.parameters["org_id"].default is None
    src = inspect.getsource(rr._worker_loop)
    assert 'org_id = task["org_id"]' in src
