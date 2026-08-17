"""Tests for GET/POST /api/llm/config — the admin provider/role editor.

Handlers are exercised directly (Depends bypassed by passing a user dict),
matching the pattern used elsewhere in this suite. config.yaml writes go to a
tmp file via the module's _CONFIG_YAML_PATH seam — the repo config is never
touched.
"""

import pytest
from fastapi import HTTPException

import context
from routers import transcription as tr

ADMIN = {"role": "admin", "id": 1}
MEMBER = {"role": "member", "id": 2}


@pytest.fixture()
def cfg(monkeypatch, tmp_path):
    """Shared live config dict patched into both context and the router module,
    plus a tmp config.yaml the POST handler persists to."""
    shared = {
        "llm": {
            "providers": {
                "openrouter": {"kind": "openai-compat",
                               "base_url": "https://openrouter.ai/api/v1",
                               "api_key": "sk-or-secret",
                               "api_key_env": "OPENROUTER_API_KEY"},
                "ollama": {"kind": "openai-compat",
                           "base_url": "http://localhost:11434/v1",
                           "api_key": "local"},
            },
            "roles": {
                "default": {"provider": "openrouter", "model": "m1"},
                "summary": {"provider": "ollama", "model": "m2"},
            },
        }
    }
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("llm: {}\nother_key: keep-me\n", encoding="utf-8")
    monkeypatch.setattr(context, "config", shared)
    monkeypatch.setattr(tr, "config", shared)
    monkeypatch.setattr(tr, "_CONFIG_YAML_PATH", yaml_path)
    return shared, yaml_path


def _valid_body():
    return {
        "providers": {
            "openrouter": {"kind": "openai-compat",
                           "base_url": "https://openrouter.ai/api/v1",
                           "api_key": tr._KEY_MASK,
                           "api_key_env": "OPENROUTER_API_KEY"},
        },
        "roles": {"default": {"provider": "openrouter", "model": "new/model"}},
    }


# ---------------------------------------------------------------------------
# Auth + masking
# ---------------------------------------------------------------------------

async def test_non_admin_forbidden(cfg):
    with pytest.raises(HTTPException) as exc:
        await tr.get_llm_config(user=MEMBER)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await tr.save_llm_config(_valid_body(), user=MEMBER)
    assert exc.value.status_code == 403


async def test_get_masks_inline_keys(cfg):
    result = await tr.get_llm_config(user=ADMIN)
    orp = result["providers"]["openrouter"]
    assert orp["api_key"] == tr._KEY_MASK          # never the raw key
    assert orp["has_key"] is True
    assert "sk-or-secret" not in str(result)
    assert result["explicit"] is True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

async def test_rejects_unknown_kind(cfg):
    body = _valid_body()
    body["providers"]["openrouter"]["kind"] = "grpc"
    with pytest.raises(HTTPException) as exc:
        await tr.save_llm_config(body, user=ADMIN)
    assert exc.value.status_code == 400
    assert "kind" in exc.value.detail


async def test_rejects_role_with_missing_provider(cfg):
    body = _valid_body()
    body["roles"]["default"]["provider"] = "ghost"
    with pytest.raises(HTTPException) as exc:
        await tr.save_llm_config(body, user=ADMIN)
    assert "unknown provider" in exc.value.detail


async def test_rejects_missing_base_url_for_openai_compat(cfg):
    body = _valid_body()
    body["providers"]["openrouter"].pop("base_url")
    with pytest.raises(HTTPException) as exc:
        await tr.save_llm_config(body, user=ADMIN)
    assert "base_url" in exc.value.detail


async def test_rejects_missing_default_role(cfg):
    body = _valid_body()
    body["roles"] = {"chat": {"provider": "openrouter", "model": "m"}}
    with pytest.raises(HTTPException) as exc:
        await tr.save_llm_config(body, user=ADMIN)
    assert "default" in exc.value.detail


# ---------------------------------------------------------------------------
# Key preservation + persistence
# ---------------------------------------------------------------------------

async def test_masked_key_preserves_stored_value(cfg):
    shared, yaml_path = cfg
    result = await tr.save_llm_config(_valid_body(), user=ADMIN)
    # live config kept the real key even though the client sent the mask
    assert shared["llm"]["providers"]["openrouter"]["api_key"] == "sk-or-secret"
    # response is masked
    assert result["providers"]["openrouter"]["api_key"] == tr._KEY_MASK
    # persisted yaml carries the real key but other keys survive
    text = yaml_path.read_text(encoding="utf-8")
    assert "sk-or-secret" in text
    assert "keep-me" in text


async def test_real_key_overwrites(cfg):
    shared, _ = cfg
    body = _valid_body()
    body["providers"]["openrouter"]["api_key"] = "sk-or-NEW"
    await tr.save_llm_config(body, user=ADMIN)
    assert shared["llm"]["providers"]["openrouter"]["api_key"] == "sk-or-NEW"


async def test_empty_key_keeps_existing(cfg):
    shared, _ = cfg
    body = _valid_body()
    body["providers"]["openrouter"]["api_key"] = ""
    await tr.save_llm_config(body, user=ADMIN)
    assert shared["llm"]["providers"]["openrouter"]["api_key"] == "sk-or-secret"


async def test_roles_replaced_and_live(cfg):
    shared, _ = cfg
    await tr.save_llm_config(_valid_body(), user=ADMIN)
    assert shared["llm"]["roles"] == {"default": {"provider": "openrouter", "model": "new/model"}}
    # llm.resolve sees the update through the live config
    import llm
    provider, model = llm.resolve("default")
    assert provider.name == "openrouter"
    assert model == "new/model"


# ---------------------------------------------------------------------------
# OpenRouter OAuth connect
# ---------------------------------------------------------------------------

async def test_openrouter_start_requires_admin_and_callback(cfg):
    with pytest.raises(HTTPException) as exc:
        await tr.openrouter_oauth_start({"callback_url": "http://x/settings"}, user=MEMBER)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await tr.openrouter_oauth_start({"callback_url": "javascript:alert(1)"}, user=ADMIN)
    assert exc.value.status_code == 400


async def test_openrouter_complete_without_pending_rejected(cfg):
    tr._or_pending.clear()
    with pytest.raises(HTTPException) as exc:
        await tr.openrouter_oauth_complete({"code": "abc"}, user=ADMIN)
    assert "start again" in exc.value.detail


async def test_openrouter_full_flow_stores_key(cfg, monkeypatch):
    shared, yaml_path = cfg
    d = await tr.openrouter_oauth_start(
        {"callback_url": "http://localhost:8000/settings"}, user=ADMIN)
    assert "openrouter.ai/auth" in d["auth_url"]
    assert "code_challenge=" in d["auth_url"]

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"key": "sk-or-v1-provisioned"}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = await tr.openrouter_oauth_complete({"code": "authcode"}, user=ADMIN)
    assert result["ok"] is True
    assert shared["llm"]["providers"]["openrouter"]["api_key"] == "sk-or-v1-provisioned"
    assert "sk-or-v1-provisioned" not in str(result)      # masked in response
    assert "sk-or-v1-provisioned" in yaml_path.read_text()  # persisted


async def test_gray_oauth_flag_gates_start(cfg):
    # flag absent/false → gray providers refused before any network call
    with pytest.raises(HTTPException) as exc:
        await tr.pi_oauth_start({"provider": "openai-codex"}, user=ADMIN)
    assert exc.value.status_code == 403
    assert "llm_oauth_gray_flows" in exc.value.detail
    # status endpoint reports disabled without proxying
    d = await tr.pi_oauth_status(user=ADMIN)
    assert d == {"enabled": False, "providers": {}}
    # non-admin blocked regardless
    with pytest.raises(HTTPException) as exc:
        await tr.pi_oauth_status(user=MEMBER)
    assert exc.value.status_code == 403
