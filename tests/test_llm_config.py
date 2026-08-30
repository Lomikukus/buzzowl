"""Tests for GET/POST /api/llm/config — the admin provider/role editor.

Handlers are exercised directly (Depends bypassed by passing a user dict),
matching the pattern used elsewhere in this suite. config.yaml writes go to a
tmp file via the module's _CONFIG_YAML_PATH seam — the repo config is never
touched.
"""

import pytest
from fastapi import HTTPException

import context
from routers import llm_config as tr

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


async def test_rejects_bad_headers_type(cfg):
    body = _valid_body()
    body["providers"]["openrouter"]["headers"] = "not-a-dict"
    with pytest.raises(HTTPException) as exc:
        await tr.save_llm_config(body, user=ADMIN)
    assert "headers" in exc.value.detail


# ---------------------------------------------------------------------------
# Writability gate (config.yaml is mounted read-only in docker-compose)
# ---------------------------------------------------------------------------

async def test_post_403_when_config_not_writable(cfg, monkeypatch):
    # monkeypatch os.access rather than chmod(0o444): chmod-as-root (e.g. in a
    # container/CI running as root) doesn't actually deny writes, making that
    # style of test unreliable — os.access is what the handler calls anyway.
    shared, yaml_path = cfg
    monkeypatch.setattr(tr.os, "access", lambda path, mode: False)
    with pytest.raises(HTTPException) as exc:
        await tr.save_llm_config(_valid_body(), user=ADMIN)
    assert exc.value.status_code == 403
    assert "not writable" in exc.value.detail
    assert "Settings" in exc.value.detail   # points admins at the DB-backed alternative
    # nothing was mutated
    assert shared["llm"]["roles"]["default"]["model"] == "m1"


async def test_post_403_when_config_missing(cfg, monkeypatch):
    # os.access on a missing path is also False — same 403, worded to cover both
    shared, yaml_path = cfg
    yaml_path.unlink()
    with pytest.raises(HTTPException) as exc:
        await tr.save_llm_config(_valid_body(), user=ADMIN)
    assert exc.value.status_code == 403
    assert "missing or not writable" in exc.value.detail


# ---------------------------------------------------------------------------
# headers preservation (the chatgpt/pi bridge's {pi_provider: openai-codex})
# ---------------------------------------------------------------------------

async def test_headers_persisted_when_provided(cfg):
    shared, yaml_path = cfg
    shared["llm"]["providers"]["chatgpt"] = {"kind": "pi", "headers": {"pi_provider": "openai-codex"}}
    got = await tr.get_llm_config(user=ADMIN)
    assert got["providers"]["chatgpt"]["headers"] == {"pi_provider": "openai-codex"}
    # Round-trip the masked GET response straight back through POST, unchanged
    body = {"providers": got["providers"], "roles": got["roles"]}
    result = await tr.save_llm_config(body, user=ADMIN)
    assert result["providers"]["chatgpt"]["headers"] == {"pi_provider": "openai-codex"}
    assert shared["llm"]["providers"]["chatgpt"]["headers"] == {"pi_provider": "openai-codex"}
    assert "openai-codex" in yaml_path.read_text(encoding="utf-8")


async def test_headers_preserved_when_omitted_from_body(cfg):
    """The settings UI form has no headers field — a save must not silently wipe
    a provider's headers just because the client never mentioned them (this used
    to break the ChatGPT-Codex bridge)."""
    shared, _ = cfg
    shared["llm"]["providers"]["chatgpt"] = {"kind": "pi", "headers": {"pi_provider": "openai-codex"}}
    body = _valid_body()                         # mirrors what the UI actually posts
    body["providers"]["chatgpt"] = {"kind": "pi"}   # no "headers" key at all
    result = await tr.save_llm_config(body, user=ADMIN)
    assert result["providers"]["chatgpt"]["headers"] == {"pi_provider": "openai-codex"}
    assert shared["llm"]["providers"]["chatgpt"]["headers"] == {"pi_provider": "openai-codex"}


async def test_headers_cleared_with_explicit_empty_object(cfg):
    shared, _ = cfg
    shared["llm"]["providers"]["chatgpt"] = {"kind": "pi", "headers": {"pi_provider": "openai-codex"}}
    body = _valid_body()
    body["providers"]["chatgpt"] = {"kind": "pi", "headers": {}}
    await tr.save_llm_config(body, user=ADMIN)
    assert shared["llm"]["providers"]["chatgpt"].get("headers", {}) == {}


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


class _FakeOrgDB:
    """Minimal stand-in for db.py's org-settings surface."""

    def __init__(self, llm=None):
        self.settings = {"llm": llm or {}}

    async def get_org_settings(self, org_id):
        return dict(self.settings)

    async def update_org_settings(self, org_id, patch):
        self.settings.update(patch)
        return self.settings


async def test_openrouter_complete_persists_encrypted_org_key(cfg, monkeypatch):
    """Connect no longer touches config.yaml at all — the provisioned key is
    stored per-org in the DB (encrypted), so it survives a read-only mount."""
    fake_db = _FakeOrgDB()
    monkeypatch.setattr(tr, "DB_AVAILABLE", True)
    monkeypatch.setattr(tr, "db_module", fake_db)
    invalidated = []
    monkeypatch.setattr(tr.llm, "invalidate_org_overlay", lambda org_id=None: invalidated.append(org_id))
    admin = {"role": "admin", "id": 1, "org_id": 42}

    d = await tr.openrouter_oauth_start({"callback_url": "http://localhost:8000/settings"}, user=admin)
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
    result = await tr.openrouter_oauth_complete({"code": "authcode"}, user=admin)

    assert result["ok"] is True and result["connected"] == "openrouter"
    stored = fake_db.settings["llm"]["providers"]["openrouter"]["api_key"]
    assert stored.startswith("enc:v1:")                    # encrypted at rest
    assert "sk-or-v1-provisioned" not in stored
    assert "sk-or-v1-provisioned" not in str(result)        # never echoed back
    assert result["providers"]["openrouter"]["has_key"] is True
    # config.yaml (the platform-level shared config) is untouched by this flow —
    # its own pre-existing openrouter provider entry is unchanged
    assert tr.config["llm"]["providers"]["openrouter"]["api_key"] == "sk-or-secret"
    # first connect with no org roles yet -> usable default role, borrowing the
    # platform's default model (cfg fixture sets it to "m1")
    assert fake_db.settings["llm"]["roles"]["default"] == {"provider": "openrouter", "model": "m1"}
    assert invalidated == [42]


async def test_openrouter_complete_keeps_existing_org_roles(cfg, monkeypatch):
    # 'anthropic' must be a real stored provider (not just a role reference) —
    # sanitize_org_llm now drops any role whose provider isn't among the
    # providers actually being saved (WP7d dangling-role cheap belt).
    fake_db = _FakeOrgDB(llm={
        "providers": {"anthropic": {"kind": "anthropic", "api_key": tr.plans.encrypt_secret("sk-ant-x")}},
        "roles": {"default": {"provider": "anthropic", "model": "claude-x"}},
    })
    monkeypatch.setattr(tr, "DB_AVAILABLE", True)
    monkeypatch.setattr(tr, "db_module", fake_db)
    monkeypatch.setattr(tr.llm, "invalidate_org_overlay", lambda org_id=None: None)
    admin = {"role": "admin", "id": 1, "org_id": 42}
    await tr.openrouter_oauth_start({"callback_url": "http://localhost:8000/settings"}, user=admin)

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
    await tr.openrouter_oauth_complete({"code": "authcode"}, user=admin)
    # an org that already had roles keeps them — connect doesn't reassign 'default'
    assert fake_db.settings["llm"]["roles"]["default"] == {"provider": "anthropic", "model": "claude-x"}


async def test_openrouter_complete_preserves_other_providers(cfg, monkeypatch):
    """Regression for the blocker: connect used to build providers={"openrouter":
    ...} only, and merge_org_llm() doesn't union providers by name — it patches
    whatever incoming names and returns incoming as-is — so a bare connect used
    to silently delete every other stored provider (and its encrypted key) the
    org had, leaving roles dangling onto a provider that no longer exists."""
    existing_key_plain = "sk-ant-existing-secret"
    existing_llm = {
        "providers": {
            "anthropic": {"kind": "anthropic", "base_url": "",
                          "api_key": tr.plans.encrypt_secret(existing_key_plain), "headers": {}},
        },
        "roles": {"default": {"provider": "anthropic", "model": "claude-x"},
                  "chat": {"provider": "anthropic", "model": "claude-haiku"}},
    }
    fake_db = _FakeOrgDB(llm=existing_llm)
    monkeypatch.setattr(tr, "DB_AVAILABLE", True)
    monkeypatch.setattr(tr, "db_module", fake_db)
    monkeypatch.setattr(tr.llm, "invalidate_org_overlay", lambda org_id=None: None)
    admin = {"role": "admin", "id": 1, "org_id": 42}
    await tr.openrouter_oauth_start({"callback_url": "http://localhost:8000/settings"}, user=admin)

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
    result = await tr.openrouter_oauth_complete({"code": "authcode"}, user=admin)

    provs = fake_db.settings["llm"]["providers"]
    # the pre-existing provider survives the connect...
    assert set(provs.keys()) == {"anthropic", "openrouter"}
    # ...and its encrypted key still decrypts to the original plaintext
    assert tr.plans.decrypt_secret(provs["anthropic"]["api_key"]) == existing_key_plain
    # the new provider is there too, encrypted, never echoed back raw
    assert provs["openrouter"]["api_key"].startswith("enc:v1:")
    assert "sk-or-v1-provisioned" not in str(result)
    # roles that pointed at the surviving provider are untouched
    assert fake_db.settings["llm"]["roles"]["chat"] == {"provider": "anthropic", "model": "claude-haiku"}


async def test_openrouter_complete_503_without_db(cfg, monkeypatch):
    monkeypatch.setattr(tr, "DB_AVAILABLE", False)
    admin = {"role": "admin", "id": 1, "org_id": 42}
    await tr.openrouter_oauth_start({"callback_url": "http://localhost:8000/settings"}, user=admin)

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
    with pytest.raises(HTTPException) as exc:
        await tr.openrouter_oauth_complete({"code": "authcode"}, user=admin)
    assert exc.value.status_code == 503


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


# ---------------------------------------------------------------------------
# Pi OAuth proxy preflight — actionable errors instead of a raw upstream 401
# ---------------------------------------------------------------------------

async def test_pi_oauth_preflight_503_when_token_empty(cfg, monkeypatch):
    # cfg's shared config has no agent_service_token; a non-gray provider name
    # (or none at all) reaches the preflight check instead of the 403 gray gate
    monkeypatch.delenv("ALLOW_INSECURE_INTERNAL", raising=False)
    with pytest.raises(HTTPException) as exc:
        await tr.pi_oauth_start({}, user=ADMIN)
    assert exc.value.status_code == 503
    assert "AGENT_SERVICE_TOKEN" in exc.value.detail
    assert "docker compose up -d" in exc.value.detail


async def test_pi_oauth_preflight_allows_insecure_internal_override(cfg, monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_INTERNAL", "1")

    class _Resp:
        status_code = 200
        content = b"{}"
        def json(self): return {}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = await tr.pi_oauth_start({}, user=ADMIN)   # no exception
    assert result == {}


async def test_pi_oauth_forward_maps_upstream_401(cfg, monkeypatch):
    shared, _ = cfg
    shared["agent_service_token"] = "shared-secret"

    class _Resp:
        status_code = 401
        content = b'{"detail": "bad token"}'
        def json(self): return {"detail": "bad token"}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(HTTPException) as exc:
        await tr.pi_oauth_start({}, user=ADMIN)
    assert exc.value.status_code == 502
    assert "disagree on AGENT_SERVICE_TOKEN" in exc.value.detail


async def test_pi_oauth_forward_passthrough_other_statuses(cfg, monkeypatch):
    shared, _ = cfg
    shared["agent_service_token"] = "shared-secret"

    class _Resp:
        status_code = 418
        content = b'{"detail": "teapot"}'
        def json(self): return {"detail": "teapot"}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(HTTPException) as exc:
        await tr.pi_oauth_start({}, user=ADMIN)
    assert exc.value.status_code == 418
    assert exc.value.detail == "teapot"
