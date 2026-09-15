"""Subscription routing — POST /api/org/llm/subscription + the chat path.

Connecting a ChatGPT/Copilot subscription (agent-pi's /oauth/*) and *using* it
are two different things: plans.sanitize_org_llm rejects kind 'pi' outright, so
the choice lives in orgs.settings.llm_subscription and is honoured only on a
self-hosted install. These tests pin both halves.
"""

import asyncio
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
    paths = sorted((root / "routers").glob("*.py"))
    for extra in ("intake.py", "playbook.py"):
        p = root / extra
        if p.exists():
            paths.append(p)
    offenders = []
    for path in paths:
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


async def test_no_llm_call_drops_the_org_context():
    """Same class as the brain-pinning guard: an LLM call without org_id
    resolves against the platform config, not the workspace — which is how the
    product-extraction step ended up on a keyless openrouter while the org had
    a working provider."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    paths = sorted((root / "routers").glob("*.py"))
    for extra in ("intake.py", "playbook.py"):
        p = root / extra
        if p.exists():
            paths.append(p)
    offenders = []
    for path in paths:
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"run_in_executor\(\s*None,\s*_call_pipeline_brain,\s*\w+\s*\)", line):
                offenders.append(f"{path.name}:{i}")
            if re.search(r"\bllm\.(complete|chat|acomplete|achat|stream)\(", line) and "org_id" not in line:
                # multi-line calls carry org_id on a later line; only flag one-liners
                if line.rstrip().endswith(")"):
                    offenders.append(f"{path.name}:{i}")
    assert not offenders, "LLM calls without org context: " + ", ".join(offenders)


async def test_no_bare_brain_call_outside_knowledge():
    """_call_brain_sync is a synchronous, cache-only read — it skips the
    ensure_org_overlay warm-up that llm.acomplete does itself, which is how
    routers/internal.py's create_client, the pipeline's careers/needs/
    market-signal/jobs-extract calls, and mail/broadcast/NBA-reason calls in
    evaluation.py/today.py/products.py used to fire on a cold overlay.
    routers/knowledge.py owns the helper (its definition plus its own
    in-file uses); everywhere else must go through
    llm.acomplete(..., org_id=...) instead."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    paths = sorted((root / "routers").glob("*.py"))
    for extra in ("intake.py", "playbook.py"):
        p = root / extra
        if p.exists():
            paths.append(p)
    offenders = []
    for path in paths:
        if path.name == "knowledge.py":
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if "_call_brain_sync(" in line:
                offenders.append(f"{path.name}:{i}")
    assert not offenders, (
        "these call _call_brain_sync directly instead of llm.acomplete, "
        "skipping the org overlay warm-up: " + ", ".join(offenders))


# ── Session enrichment: in-process tool loop vs agent-pi ────────────────────

@pytest.fixture
def enrichment(monkeypatch, tmp_path):
    """_trigger_enrichment with everything but the routing decision stubbed out.

    Yields (pipeline_module, record); record["fired"] is the agent-pi payload,
    record["legacy"] the in-process run_enrichment call — exactly one of them
    exists after a call, and which one is the thing under test.
    """
    import agents._legacy.enrichment as legacy
    import routers.agents as ag
    import routers.pipeline as pl

    record: dict = {}

    async def _fire(subject, org_id, brain, model, task=None, callback_url=None,
                    agent_type="research", brain_from_config=False):
        record["fired"] = {"subject": subject, "org_id": org_id, "brain": brain,
                           "model": model, "agent_type": agent_type}
        return "http://pi", 7

    async def _run_enrichment(session_id, entities, org_id, run_id):
        record["legacy"] = {"session_id": session_id, "org_id": org_id}
        return {"enriched": 0, "errors": []}

    monkeypatch.setattr(ag, "_fire_agent_service", _fire)
    monkeypatch.setattr(ag, "_watch_agent_service_run", AsyncMock())
    monkeypatch.setattr(legacy, "run_enrichment", _run_enrichment)
    monkeypatch.setattr(pl, "BASE_DIR", tmp_path)    # no session files → prep no-ops
    monkeypatch.setattr(pl, "DB_AVAILABLE", True)
    monkeypatch.setattr(pl, "_promote_session", lambda sid: {"ok": True})
    monkeypatch.setattr(pl.db_module, "create_agent_run", AsyncMock(return_value=42))
    monkeypatch.setattr(pl.db_module, "update_agent_run", AsyncMock())
    # The backend this is all about: static config says "run it in-process".
    monkeypatch.setitem(context.config, "agent_service_backend", "python")
    return pl, record


async def test_enrichment_on_a_subscription_is_handed_to_agent_pi(enrichment, monkeypatch):
    """The in-process enrichment loop calls tools, the Pi bridge is text-only —
    so on a subscription workspace the static "python" backend must not get the
    last word, or the run dies inside llm.chat with nothing written."""
    pl, record = enrichment
    await _overlay(monkeypatch, _settings(sub=SUB))
    try:
        await pl._trigger_enrichment("sess-sub", ORG_ID)
        await asyncio.sleep(0)
    finally:
        llm.invalidate_org_overlay(ORG_ID)
    assert "legacy" not in record, "the in-process loop ran on a text-only provider"
    assert record["fired"]["agent_type"] == "enrichment"
    # The resolution is passed on, not repeated — the two can never disagree.
    assert (record["fired"]["brain"], record["fired"]["model"]) == ("openai-codex", "gpt-5.4")


async def test_enrichment_without_a_subscription_keeps_the_python_loop(enrichment, monkeypatch):
    """Everything that is not the bridge keeps today's path, byte for byte."""
    pl, record = enrichment
    _platform_openrouter(monkeypatch)
    await _overlay(monkeypatch, _settings())
    try:
        await pl._trigger_enrichment("sess-own", ORG_ID)
        await asyncio.sleep(0)
    finally:
        llm.invalidate_org_overlay(ORG_ID)
    assert "fired" not in record, "an openai-compatible provider was sent to agent-pi"
    assert record["legacy"]["session_id"] == "sess-own"


# ── The early refusal for the in-process tool loop ──────────────────────────

async def test_tool_loop_on_the_bridge_is_refused_with_the_way_out(monkeypatch):
    """The refusal a tool loop used to get came from deep inside llm.chat and
    said only "text-only". At the seam where the loop is built it can say what
    to do instead."""
    from agents.runner import _load_brain

    _platform_openrouter(monkeypatch)
    monkeypatch.setitem(context.config, "agent_brain", "openrouter")
    await _overlay(monkeypatch, _settings(sub=SUB))
    try:
        with pytest.raises(llm.LLMError) as exc:
            _load_brain(org_id=ORG_ID)
    finally:
        llm.invalidate_org_overlay(ORG_ID)
    msg = str(exc.value)
    assert "agent-pi" in msg                     # where tool calling does work
    assert "enrichment" in msg                   # and which run types do it
    assert "Settings" in msg                     # and how to get the loop back


async def test_tool_loop_is_not_refused_on_an_openai_compatible_provider(monkeypatch):
    from agents.brain import OpenAICompatibleBrain
    from agents.runner import _load_brain

    own = {"ollama": {"kind": "openai-compat", "base_url": "http://x/v1", "api_key": "local"}}
    _platform_openrouter(monkeypatch)
    monkeypatch.setitem(context.config, "agent_brain", "openrouter")
    await _overlay(monkeypatch, _settings(sub=SUB, own=own))
    try:
        brain = _load_brain(org_id=ORG_ID)
    finally:
        llm.invalidate_org_overlay(ORG_ID)
    assert isinstance(brain, OpenAICompatibleBrain)


async def test_a_brain_built_on_a_cold_overlay_still_refuses_before_any_tool_runs(monkeypatch):
    """_load_brain reads the overlay cache only, so a brain built before the org
    was loaded gets no refusal there — think() is the backstop, and it fires
    before the first tool call rather than after six of them."""
    from agents.brain import OpenAICompatibleBrain
    from agents.tools import Tool

    monkeypatch.setitem(context.config, "llm_oauth_gray_flows", True)
    monkeypatch.setitem(context.config, "hosted", {})
    reached = {}

    async def _achat(*a, **k):
        reached["achat"] = True
        return {}

    monkeypatch.setattr(llm, "achat", _achat)
    tool = Tool(name="noop", description="", parameters={}, fn=lambda: None)
    try:
        with (
            patch("context.db_module.get_org_settings", new_callable=AsyncMock,
                  return_value=_settings(sub=SUB)),
            patch("context.db_module.llm_usage_month_cost", new_callable=AsyncMock,
                  return_value=0.0),
        ):
            llm.invalidate_org_overlay(ORG_ID)
            brain = OpenAICompatibleBrain(role="default", org_id=ORG_ID)
            with pytest.raises(llm.LLMError) as exc:
                await brain.think([{"role": "user", "content": "hi"}], [tool])
    finally:
        llm.invalidate_org_overlay(ORG_ID)
    assert "agent-pi" in str(exc.value)
    assert not reached, "the tool loop reached llm.achat on a text-only provider"


async def test_text_only_calls_still_run_on_the_bridge(monkeypatch):
    """The refusal is about tool loops only: summaries, triage and NBA reasons
    go through the same subscription provider and must keep working."""
    seen = {}

    def _fake_pi_complete(provider, model, messages, max_tokens, timeout, org_id=None):
        seen["pi_provider"] = (provider.headers or {}).get("pi_provider")
        return {"content": "summary text", "tool_calls": [], "_usage": None}

    monkeypatch.setattr(llm, "_pi_complete", _fake_pi_complete)
    await _overlay(monkeypatch, _settings(sub=SUB))
    try:
        out = await llm.acomplete("summarise this", role="summary", org_id=ORG_ID)
    finally:
        llm.invalidate_org_overlay(ORG_ID)
    assert out == "summary text"
    assert seen["pi_provider"] == "openai-codex"


async def test_no_run_is_fired_around_the_resolver():
    """match_synthesis and pain-point research POSTed to agent-pi themselves,
    building provider/brain/model from config — so they never saw the org's
    subscription and finished with zero tool calls while reporting "done".
    Every payload that names a provider must come from resolve_run_target."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    # The two resolvers themselves, keyed by ENCLOSING FUNCTION rather than by
    # line number: a line-keyed allowlist breaks on any edit above it (three
    # parallel branches had to bump it in one week) and, worse, can silently
    # bless a new bare call that lands on the old line.
    allowed = {"agents.py": {"resolve_run_target"}, "chat.py": {"_resolve_pi_chat_target"}}
    offenders = []
    for path in sorted((root / "routers").glob("*.py")):
        if path.name == "benchmark.py":
            continue          # explicit brain/model comparison tool, by design
        current_fn = ""
        for i, line in enumerate(path.read_text().splitlines(), 1):
            fn = re.match(r"\s*(?:async\s+)?def\s+(\w+)\(", line)
            if fn:
                current_fn = fn.group(1)
            if "provider_for_brain(" in line and current_fn not in allowed.get(path.name, set()):
                offenders.append(f"{path.name}:{i} ({current_fn or 'module level'})")
    assert not offenders, (
        "these build a run payload without resolve_run_target: " + ", ".join(offenders))
