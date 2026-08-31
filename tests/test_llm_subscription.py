"""Subscription routing — POST /api/org/llm/subscription + the chat path.

Connecting a ChatGPT/Copilot subscription (agent-pi's /oauth/*) and *using* it
are two different things: plans.sanitize_org_llm rejects kind 'pi' outright, so
the choice lives in orgs.settings.llm_subscription and is honoured only on a
self-hosted install. These tests pin both halves.
"""

import time as _time

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, patch

import context
import llm


ADMIN = {"org_id": 8, "role": "admin"}


class _FakeDB:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    async def get_org_settings(self, org_id):
        return dict(self.settings)

    async def update_org_settings(self, org_id, patch):
        self.settings.update(patch)
        return dict(self.settings)


@pytest.fixture
def selfhosted(monkeypatch):
    """Self-hosted install with gray flows on — the only shape that offers this."""
    from routers import org_settings as os_router
    db = _FakeDB()
    monkeypatch.setattr(os_router, "DB_AVAILABLE", True)
    monkeypatch.setattr(os_router, "db_module", db)
    monkeypatch.setattr(os_router, "_config", {"hosted": {}, "llm_oauth_gray_flows": True})
    return os_router, db


def _fake_pi(monkeypatch, *, connected=True, text="OK"):
    """Stand in for the agent-pi forward (status probe + completion probe).

    org_settings imports _pi_oauth_forward inside the handler, so patching the
    module attribute is enough — no import-order dance needed."""
    from routers import llm_config

    async def fake(method, path, payload, user):
        if path == "/oauth/status":
            return {"openai-codex": {"connected": connected}}
        return {"text": text}

    monkeypatch.setattr(llm_config, "_pi_oauth_forward", fake)


# ── The endpoint ────────────────────────────────────────────────────────────

async def test_happy_path_stores_provider_and_model(selfhosted, monkeypatch):
    os_router, db = selfhosted
    _fake_pi(monkeypatch)
    out = await os_router.set_org_subscription(
        {"provider": "openai-codex", "model": "gpt-5.1-codex"}, user=ADMIN)
    assert out == {"ok": True, "provider": "openai-codex", "model": "gpt-5.1-codex"}
    assert db.settings["llm_subscription"] == {"provider": "openai-codex", "model": "gpt-5.1-codex"}


async def test_hosted_install_refuses(selfhosted, monkeypatch):
    """A hosted deployment's personal subscription is not an org's to spend."""
    os_router, db = selfhosted
    monkeypatch.setattr(os_router, "_config",
                        {"hosted": {"signup_enabled": True}, "llm_oauth_gray_flows": True})
    _fake_pi(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await os_router.set_org_subscription(
            {"provider": "openai-codex", "model": "gpt-5.1-codex"}, user=ADMIN)
    assert exc.value.status_code == 403
    assert "llm_subscription" not in db.settings


async def test_gray_flows_off_refuses(selfhosted, monkeypatch):
    os_router, db = selfhosted
    monkeypatch.setattr(os_router, "_config", {"hosted": {}, "llm_oauth_gray_flows": False})
    _fake_pi(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await os_router.set_org_subscription(
            {"provider": "openai-codex", "model": "gpt-5.1-codex"}, user=ADMIN)
    assert exc.value.status_code == 403
    assert "llm_subscription" not in db.settings


async def test_not_connected_is_refused_and_stores_nothing(selfhosted, monkeypatch):
    os_router, db = selfhosted
    _fake_pi(monkeypatch, connected=False)
    with pytest.raises(HTTPException) as exc:
        await os_router.set_org_subscription(
            {"provider": "openai-codex", "model": "gpt-5.1-codex"}, user=ADMIN)
    assert exc.value.status_code == 400
    assert "llm_subscription" not in db.settings


async def test_model_that_produces_no_text_is_refused(selfhosted, monkeypatch):
    """The live probe is the point: a model this plan cannot drive must fail
    here, not on the user's first chat message."""
    os_router, db = selfhosted
    _fake_pi(monkeypatch, text="")
    with pytest.raises(HTTPException) as exc:
        await os_router.set_org_subscription(
            {"provider": "openai-codex", "model": "gpt-9-imaginary"}, user=ADMIN)
    assert exc.value.status_code == 400
    assert "llm_subscription" not in db.settings


async def test_unknown_provider_is_refused(selfhosted, monkeypatch):
    os_router, db = selfhosted
    _fake_pi(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await os_router.set_org_subscription({"provider": "claude-max", "model": "x"}, user=ADMIN)
    assert exc.value.status_code == 400


async def test_non_admin_is_refused(selfhosted, monkeypatch):
    os_router, _ = selfhosted
    _fake_pi(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await os_router.set_org_subscription(
            {"provider": "openai-codex", "model": "gpt-5.1-codex"},
            user={"org_id": 8, "role": "member"})
    assert exc.value.status_code == 403


async def test_clearing_removes_the_choice(selfhosted, monkeypatch):
    os_router, db = selfhosted
    db.settings["llm_subscription"] = {"provider": "openai-codex", "model": "gpt-5.1-codex"}
    out = await os_router.set_org_subscription({"provider": None}, user=ADMIN)
    assert out["provider"] is None
    assert db.settings["llm_subscription"] == {}


async def test_get_reports_state_and_offerability(selfhosted, monkeypatch):
    os_router, db = selfhosted
    db.settings["llm_subscription"] = {"provider": "openai-codex", "model": "gpt-5.1-codex"}
    out = await os_router.get_org_subscription(user=ADMIN)
    assert out["provider"] == "openai-codex" and out["offerable"] is True
    monkeypatch.setattr(os_router, "_config",
                        {"hosted": {"signup_enabled": True}, "llm_oauth_gray_flows": True})
    assert (await os_router.get_org_subscription(user=ADMIN))["offerable"] is False


# ── The chat path ───────────────────────────────────────────────────────────

ORG_ID = 8


def _no_org_providers(monkeypatch):
    """No org overlay and no usable llm: block, so resolve() cannot answer and
    the subscription is what decides."""
    llm._org_overlays[ORG_ID] = (
        _time.monotonic() + 60,
        {"plan": "light", "providers": {}, "roles": {}, "budget": None,
         "month_cost": 0.0, "enforce": True},   # enforce → resolve() refuses outright
    )


async def test_chat_routes_through_the_subscription(monkeypatch):
    from routers.chat import _resolve_pi_chat_target

    _no_org_providers(monkeypatch)
    monkeypatch.setitem(context.config, "llm_oauth_gray_flows", True)
    monkeypatch.setitem(context.config, "hosted", {})
    try:
        with patch("context.db_module.get_org_settings", new_callable=AsyncMock,
                   return_value={"llm_subscription": {"provider": "openai-codex",
                                                      "model": "gpt-5.1-codex"}}):
            provider, brain, model = await _resolve_pi_chat_target(ORG_ID)
    finally:
        llm.invalidate_org_overlay(ORG_ID)
    assert provider == "openai-codex"
    assert brain == "openai-codex"
    assert model == "gpt-5.1-codex"


async def test_hosted_install_ignores_a_stored_subscription(monkeypatch):
    """Same stored setting, hosted deployment — the config.yaml brain wins."""
    from routers.chat import _resolve_pi_chat_target

    _no_org_providers(monkeypatch)
    monkeypatch.setitem(context.config, "llm_oauth_gray_flows", True)
    monkeypatch.setitem(context.config, "hosted", {"signup_enabled": True})
    monkeypatch.setitem(context.config, "pi_chat_brain", "openrouter")
    monkeypatch.setitem(context.config, "pi_chat_model", "deepseek/deepseek-v4-pro")
    try:
        with patch("context.db_module.get_org_settings", new_callable=AsyncMock,
                   return_value={"llm_subscription": {"provider": "openai-codex",
                                                      "model": "gpt-5.1-codex"}}):
            provider, brain, model = await _resolve_pi_chat_target(ORG_ID)
    finally:
        llm.invalidate_org_overlay(ORG_ID)
    assert provider == "openrouter"
    assert model == "deepseek/deepseek-v4-pro"


async def test_own_provider_still_wins_over_the_subscription(monkeypatch):
    """An org that configured a real provider keeps it — the subscription is
    the fallback for a workspace that has none, not an override."""
    from routers.chat import _resolve_pi_chat_target

    llm._org_overlays[ORG_ID] = (
        _time.monotonic() + 60,
        {"plan": "light",
         "providers": {"ollama": {"kind": "openai-compat",
                                  "base_url": "http://127.0.0.1:19999/v1",
                                  "api_key": "local"}},
         "roles": {"chat": {"provider": "ollama", "model": "llama3.2:latest"}},
         "budget": None, "month_cost": 0.0, "enforce": False},
    )
    monkeypatch.setitem(context.config, "llm_oauth_gray_flows", True)
    monkeypatch.setitem(context.config, "hosted", {})
    try:
        with (
            patch("context.db_module.get_org_settings", new_callable=AsyncMock,
                  return_value={"llm_subscription": {"provider": "openai-codex",
                                                     "model": "gpt-5.1-codex"}}),
            patch("context.db_module.llm_usage_month_cost", new_callable=AsyncMock, return_value=0.0),
        ):
            provider, brain, model = await _resolve_pi_chat_target(ORG_ID)
    finally:
        llm.invalidate_org_overlay(ORG_ID)
    assert provider == "ollama"
    assert model == "llama3.2:latest"
