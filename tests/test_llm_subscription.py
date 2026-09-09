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
SUB = {"provider": "openai-codex", "model": "gpt-5.4"}


def _seed_overlay(monkeypatch, *, providers=None, roles=None, subscription=None, enforce=False):
    """Seed the cached org overlay — llm.org_subscription reads it, no DB."""
    llm._org_overlays[ORG_ID] = (
        _time.monotonic() + 60,
        {"plan": "light", "providers": providers or {}, "roles": roles or {},
         "budget": None, "month_cost": 0.0, "enforce": enforce,
         "subscription": subscription or {}},
    )
    monkeypatch.setitem(context.config, "llm_oauth_gray_flows", True)
    monkeypatch.setitem(context.config, "hosted", {})


def _platform_openrouter(monkeypatch):
    """The shipped config.yaml shape: a platform openrouter provider whose key
    comes from an env var that is empty on a self-hosted install."""
    monkeypatch.setitem(context.config, "llm", {
        "providers": {"openrouter": {"kind": "openai-compat",
                                     "base_url": "https://openrouter.ai/api/v1",
                                     "api_key_env": "OPENROUTER_API_KEY"}},
        "roles": {"chat": {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"}},
    })


async def test_subscription_beats_the_keyless_platform_default(monkeypatch):
    """The bug this pins: resolve() answers with the platform's openrouter for
    an org that has no provider of its own, so the run died on "No API key for
    provider: openrouter" while a connected subscription sat unused."""
    from routers.chat import _resolve_pi_chat_target

    _seed_overlay(monkeypatch, subscription=SUB)
    _platform_openrouter(monkeypatch)
    try:
        provider, brain, model = await _resolve_pi_chat_target(ORG_ID)
    finally:
        llm.invalidate_org_overlay(ORG_ID)
    assert (provider, brain, model) == ("openai-codex", "openai-codex", "gpt-5.4")


async def test_platform_default_still_used_without_a_subscription(monkeypatch):
    from routers.chat import _resolve_pi_chat_target

    _seed_overlay(monkeypatch)
    _platform_openrouter(monkeypatch)
    try:
        provider, _brain, model = await _resolve_pi_chat_target(ORG_ID)
    finally:
        llm.invalidate_org_overlay(ORG_ID)
    assert provider == "openrouter"
    assert model == "deepseek/deepseek-v4-pro"


async def test_own_provider_still_wins_over_the_subscription(monkeypatch):
    """The subscription replaces the platform fallback, not the org's own choice."""
    from routers.chat import _resolve_pi_chat_target

    _seed_overlay(
        monkeypatch,
        providers={"ollama": {"kind": "openai-compat",
                              "base_url": "http://127.0.0.1:19999/v1", "api_key": "local"}},
        roles={"chat": {"provider": "ollama", "model": "llama3.2:latest"}},
        subscription=SUB,
    )
    try:
        with patch("context.db_module.llm_usage_month_cost", new_callable=AsyncMock, return_value=0.0):
            provider, _brain, model = await _resolve_pi_chat_target(ORG_ID)
    finally:
        llm.invalidate_org_overlay(ORG_ID)
    assert provider == "ollama"
    assert model == "llama3.2:latest"


async def test_hosted_install_ignores_a_stored_subscription(monkeypatch):
    """Same stored setting, hosted deployment — the platform config wins."""
    from routers.chat import _resolve_pi_chat_target

    _seed_overlay(monkeypatch, subscription=SUB)
    monkeypatch.setitem(context.config, "hosted", {"signup_enabled": True})
    _platform_openrouter(monkeypatch)
    try:
        provider, _brain, _model = await _resolve_pi_chat_target(ORG_ID)
    finally:
        llm.invalidate_org_overlay(ORG_ID)
    assert provider == "openrouter"


# ── Agent runs ──────────────────────────────────────────────────────────────

async def test_agent_run_uses_the_subscription(monkeypatch):
    """Research and the other agent runs go through the same choke point."""
    from routers import agents as ag

    _seed_overlay(monkeypatch, subscription=SUB)
    sent = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"run_id": 1}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            sent.update(json or {})
            return _Resp()

    monkeypatch.setattr(ag.httpx, "AsyncClient", lambda *a, **k: _Client())
    try:
        await ag._fire_agent_service("Acme", ORG_ID, brain="", model="", agent_type="research")
    finally:
        llm.invalidate_org_overlay(ORG_ID)
    assert sent["provider"] == "openai-codex"
    assert sent["model"] == "gpt-5.4"


async def test_agent_run_keeps_a_user_chosen_brain(monkeypatch):
    """A brain the user explicitly asked for is never replaced."""
    from routers import agents as ag

    _seed_overlay(monkeypatch, subscription=SUB)
    sent = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"run_id": 1}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            sent.update(json or {})
            return _Resp()

    monkeypatch.setattr(ag.httpx, "AsyncClient", lambda *a, **k: _Client())
    try:
        await ag._fire_agent_service("Acme", ORG_ID, brain="ollama", model="qwen3.5",
                                     agent_type="research")
    finally:
        llm.invalidate_org_overlay(ORG_ID)
    assert sent["provider"] == "ollama"
    assert sent["model"] == "qwen3.5"


async def test_no_call_site_hardcodes_the_deployment_brain():
    """Guard against the regression this file was extended for.

    _fire_agent_service is the one place that decides which brain a run uses,
    and it can only do that when callers leave the choice to it. Thirteen call
    sites across products/pipeline/chat used to pass
    config.get("agent_service_brain") themselves, which silently defeated the
    org-subscription override for every one of them — the symptom was a
    product-research run finishing in 45ms having done nothing at all.

    The two legitimate exceptions pass brain_from_config=True (role-specific
    research/contact_enrich brains) or a brain the user picked.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted((root / "routers").glob("*.py")):
        src = path.read_text()
        for call in re.finditer(r"_fire_agent_service\(", src):
            window = src[call.start():call.start() + 400]
            if re.search(r'brain=(?:context\.)?config\.get\("agent_service_brain"', window):
                offenders.append(f"{path.name}:{src[:call.start()].count(chr(10)) + 1}")
    assert not offenders, (
        "these call sites pin the deployment brain instead of letting "
        "_fire_agent_service decide: " + ", ".join(offenders))


# ── The Python LLM paths (llm.resolve) ──────────────────────────────────────

def _settings(sub=None, own=None):
    s = {}
    if sub:
        s["llm_subscription"] = sub
    if own:
        s["llm"] = {"providers": own, "roles": {"default": {"provider": next(iter(own)), "model": "m"}}}
    return s


async def _overlay(monkeypatch, settings):
    """Build a real overlay through ensure_org_overlay with a stubbed DB."""
    monkeypatch.setitem(context.config, "llm_oauth_gray_flows", True)
    monkeypatch.setitem(context.config, "hosted", {})
    llm.invalidate_org_overlay(ORG_ID)
    with (
        patch("context.db_module.get_org_settings", new_callable=AsyncMock, return_value=settings),
        patch("context.db_module.llm_usage_month_cost", new_callable=AsyncMock, return_value=0.0),
    ):
        return await llm.ensure_org_overlay(ORG_ID, force=True)


async def test_subscription_serves_resolve_for_every_role(monkeypatch):
    """The pipeline brain that turns a research report into products goes
    through resolve(); without this it hit the keyless platform provider and
    the run ended with zero products and a 401 in the log."""
    try:
        await _overlay(monkeypatch, _settings(sub=SUB))
        for role in ("default", "pipeline", "research", "summary"):
            provider, model = llm.resolve(role, None, ORG_ID)
            assert provider.kind == "pi"
            assert provider.headers.get("pi_provider") == "openai-codex"
            assert model == "gpt-5.4"
    finally:
        llm.invalidate_org_overlay(ORG_ID)


async def test_subscription_counts_as_configured(monkeypatch):
    """A 'pi' provider carries no API key of its own — status_cheap must not
    read that as "no model configured" and put the banner back."""
    try:
        await _overlay(monkeypatch, _settings(sub=SUB))
        assert llm.status_cheap(org_id=ORG_ID) is True
    finally:
        llm.invalidate_org_overlay(ORG_ID)


async def test_synthetic_provider_is_not_mistaken_for_the_orgs_own(monkeypatch):
    """org_subscription's second element drives the agent-run precedence — if
    the injected provider counted as "the org configured one", agent runs
    would stop using the subscription again."""
    try:
        await _overlay(monkeypatch, _settings(sub=SUB))
        sub, has_own = llm.org_subscription(ORG_ID)
        assert sub == ("openai-codex", "gpt-5.4")
        assert has_own is False
    finally:
        llm.invalidate_org_overlay(ORG_ID)


async def test_own_provider_is_never_replaced_by_the_subscription(monkeypatch):
    own = {"ollama": {"kind": "openai-compat", "base_url": "http://x/v1", "api_key": "local"}}
    try:
        ov = await _overlay(monkeypatch, _settings(sub=SUB, own=own))
        assert set(ov["providers"]) == {"ollama"}
        assert not ov.get("providers_from_subscription")
        assert llm.org_subscription(ORG_ID)[1] is True
    finally:
        llm.invalidate_org_overlay(ORG_ID)


async def test_hosted_install_gets_no_synthetic_provider(monkeypatch):
    try:
        monkeypatch.setitem(context.config, "hosted", {"signup_enabled": True})
        llm.invalidate_org_overlay(ORG_ID)
        with (
            patch("context.db_module.get_org_settings", new_callable=AsyncMock,
                  return_value=_settings(sub=SUB)),
            patch("context.db_module.llm_usage_month_cost", new_callable=AsyncMock, return_value=0.0),
        ):
            ov = await llm.ensure_org_overlay(ORG_ID, force=True)
        assert not ov.get("providers")
    finally:
        llm.invalidate_org_overlay(ORG_ID)
