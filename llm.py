"""
llm.py — unified LLM provider layer.

Every chat/completion call in the server goes through this module. Three
wire adapters cover all providers:

  kind: openai-compat   OpenAI, OpenRouter, Ollama (/v1), LM Studio, vLLM,
                        LiteLLM — any endpoint speaking the OpenAI chat API.
  kind: anthropic       Anthropic Messages API via the official SDK
                        (optional dependency — guarded import).
  kind: pi              text-only bridge through the Pi agent service
                        (/complete) — for providers only Pi can auth, i.e. the
                        subscription-OAuth ones (ChatGPT-Codex, Copilot).
                        Config: {kind: pi, headers: {pi_provider: openai-codex}}

Configuration (config.yaml):

    llm:
      providers:
        openrouter:
          kind: openai-compat
          base_url: https://openrouter.ai/api/v1
          api_key_env: OPENROUTER_API_KEY
        anthropic:
          kind: anthropic
          api_key_env: ANTHROPIC_API_KEY
        ollama:
          kind: openai-compat
          base_url: http://localhost:11434/v1
          api_key: local           # local servers need a non-empty dummy key
      roles:
        default:  {provider: openrouter, model: deepseek/deepseek-v4-flash}
        research: {provider: openrouter, model: deepseek/deepseek-v4-flash}
        # chat / pipeline / match / summary / triage — all fall back to default

When no `llm:` block exists, the legacy keys (pipeline_brain, research_brain,
agent_brain, pi_chat_brain, match_brain, openrouter_api_key, OPENROUTE, …) are
synthesized into the same structure, so existing installs keep working
unchanged.

Neutral message format (shared with agents/brain.py):
    {"role": "system"|"user"|"assistant", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [{"id","name","arguments"}]}
    {"role": "tool", "content": "...", "tool_call_id": "...", "tool_name": "..."}

`chat()` always returns tool_calls carrying an `id` (synthesized when the
provider omits one) and always emits `tool_call_id` on the wire for tool-role
messages — strict OpenAI-compatible backends 400 without it.
"""

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Iterator, Optional

import requests

import context

logger = logging.getLogger("whisper.llm")

# Hard fallback when neither an llm: block nor legacy keys yield a usable role.
_FALLBACK_PROVIDER = "openrouter"
_FALLBACK_MODEL = "deepseek/deepseek-v4-flash"

_KNOWN_ROLES = ("default", "chat", "research", "pipeline", "match", "summary", "triage")


@dataclass
class ProviderConfig:
    name: str
    kind: str                      # "openai-compat" | "anthropic" | "pi"
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    headers: dict = field(default_factory=dict)

    def resolve_key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            val = os.environ.get(self.api_key_env, "")
            if val:
                return val
        # Legacy env fallbacks by provider name
        if self.name == "openrouter":
            return os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get("OPENROUTE", "")
        if self.kind == "anthropic":
            return os.environ.get("ANTHROPIC_API_KEY", "")
        return ""


class LLMError(RuntimeError):
    """Raised when a call fails after retries or a provider is misconfigured."""


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

_LEGACY_BRAIN_TO_PROVIDER = {
    "openrouter": "openrouter",
    "claude": "anthropic",
    "ollama": "ollama",
}


def _legacy_synthesis(cfg: dict) -> dict:
    """Build an llm: block from pre-provider-layer config keys."""
    providers = {
        "openrouter": {
            "kind": "openai-compat",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": cfg.get("openrouter_api_key", ""),
            "api_key_env": "OPENROUTER_API_KEY",
        },
        "anthropic": {
            "kind": "anthropic",
            "api_key": cfg.get("anthropic_api_key", ""),
            "api_key_env": "ANTHROPIC_API_KEY",
        },
        "ollama": {
            "kind": "openai-compat",
            "base_url": (os.environ.get("OLLAMA_URL", "") or "http://localhost:11434").rstrip("/") + "/v1",
            "api_key": "local",
        },
    }

    def role(brain_key: str, model_key: str, fb_brain: str, fb_model: str) -> dict:
        brain = cfg.get(brain_key) or cfg.get(fb_brain) or "openrouter"
        model = cfg.get(model_key) or cfg.get(fb_model) or _FALLBACK_MODEL
        provider = _LEGACY_BRAIN_TO_PROVIDER.get(brain, "openrouter")
        return {"provider": provider, "model": model}

    roles = {
        "default":  role("agent_brain", "agent_model", "agent_brain", "agent_model"),
        "research": role("research_brain", "research_model", "agent_brain", "agent_model"),
        "pipeline": role("pipeline_brain", "pipeline_model", "agent_brain", "agent_model"),
        "chat":     role("pi_chat_brain", "pi_chat_model", "agent_service_brain", "agent_service_model"),
        "match":    role("match_brain", "match_model", "agent_brain", "agent_model"),
        # Live-summary legacy path was Ollama-only (ollama_model)
        "summary":  {"provider": "ollama", "model": cfg.get("ollama_model", "llama3.2")},
    }
    return {"providers": providers, "roles": roles}


def _effective_config() -> dict:
    """Return the llm: block, synthesizing one from legacy keys if absent.

    Reads context.config live on every call — /api/settings mutates it in place.
    """
    cfg = context.config
    block = cfg.get("llm")
    if isinstance(block, dict) and block.get("providers"):
        return block
    return _legacy_synthesis(cfg)


def _get_provider(name: str) -> ProviderConfig:
    block = _effective_config()
    raw = (block.get("providers") or {}).get(name)
    if not raw:
        raise LLMError(f"LLM provider {name!r} is not configured")
    return ProviderConfig(
        name=name,
        kind=raw.get("kind", "openai-compat"),
        base_url=(raw.get("base_url") or "").rstrip("/"),
        api_key=raw.get("api_key", "") or "",
        api_key_env=raw.get("api_key_env", "") or "",
        headers=raw.get("headers") or {},
    )


# ---------------------------------------------------------------------------
# Per-org overlay (Phase 6a hosted plans). The overlay is loaded ASYNC by the
# a* wrappers / callers via ensure_org_overlay(org_id) and cached here so the
# synchronous chat/complete/stream path stays synchronous.
# ---------------------------------------------------------------------------
_ORG_OVERLAY_TTL = 60.0
_org_overlays: dict = {}   # org_id -> (expires_monotonic, {"plan","providers","roles","budget","month_cost","enforce"})


async def ensure_org_overlay(org_id: Optional[int], force: bool = False) -> Optional[dict]:
    """Load (or refresh) an org's plan/LLM overlay into the in-process cache."""
    if not org_id:
        return None
    import time as _t
    now = _t.monotonic()
    hit = _org_overlays.get(org_id)
    if hit and not force and hit[0] > now:
        return hit[1]
    try:
        import plans as _plans
        db = context.db_module
        settings = await db.get_org_settings(org_id) if (context.DB_AVAILABLE and db) else {}
        month_cost = await db.llm_usage_month_cost(org_id) if (context.DB_AVAILABLE and db) else 0.0
        ov = {
            "plan": _plans.plan_of(settings),
            **_plans.overlay_for_llm(settings.get("llm") or {}, org_id),
            "budget": _plans.budget_usd(settings, context.config),
            "month_cost": float(month_cost or 0.0),
            "enforce": _plans.enforce_plans(context.config),
        }
    except Exception as exc:  # DB hiccup → behave like a single-tenant install
        logger.debug("org overlay unavailable for %s: %s", org_id, exc)
        ov = None
    _org_overlays[org_id] = (now + _ORG_OVERLAY_TTL, ov)
    return ov


def invalidate_org_overlay(org_id: Optional[int] = None) -> None:
    if org_id is None:
        _org_overlays.clear()
    else:
        _org_overlays.pop(org_id, None)


def _org_overlay_sync(org_id: Optional[int]) -> Optional[dict]:
    hit = _org_overlays.get(org_id) if org_id else None
    return hit[1] if hit else None


def _get_provider_from(block: dict, name: str) -> ProviderConfig:
    raw = (block.get("providers") or {}).get(name)
    if not raw:
        raise LLMError(f"LLM provider {name!r} is not configured")
    return ProviderConfig(
        name=name,
        kind=raw.get("kind", "openai-compat"),
        base_url=(raw.get("base_url") or "").rstrip("/"),
        api_key=raw.get("api_key", "") or "",
        api_key_env=raw.get("api_key_env", "") or "",
        headers=raw.get("headers") or {},
    )


def resolve(role: str = "default", model: Optional[str] = None,
            org_id: Optional[int] = None) -> tuple[ProviderConfig, str]:
    """Resolve a role to (provider, model). Unknown roles fall back to default.

    With org_id (multi-tenant): a 'light' org that configured its own providers
    is served from those; a 'premium' org uses the platform providers but is
    stopped when its monthly budget is exhausted; with hosted.enforce_plans a
    light org WITHOUT own providers is refused (no platform spend for it)."""
    if not isinstance(org_id, int) or isinstance(org_id, bool):
        org_id = None            # never trust a stray value in the org slot
    ov = _org_overlay_sync(org_id)
    if ov:
        if ov.get("plan") == "premium":
            b = ov.get("budget")
            if b is not None and ov.get("month_cost", 0.0) >= b:
                raise LLMError(f"monthly LLM budget of ${b:.2f} exhausted for this org — "
                               f"raise the budget or wait for the next month")
        elif ov.get("providers"):
            roles = ov.get("roles") or {}
            entry = roles.get(role) or roles.get("default") or {}
            pname = entry.get("provider") or next(iter(ov["providers"]))
            if pname in ov["providers"]:
                return _get_provider_from(ov, pname), (model or entry.get("model") or _FALLBACK_MODEL)
        elif ov.get("enforce"):
            # An org whose stored key no longer decrypts arrives here with no
            # usable providers — say so instead of "none configured".
            if ov.get("undecryptable_providers"):
                raise LLMError("this workspace's stored LLM key can no longer be decrypted (the "
                               "encryption key changed) — enter it again under Settings › LLM")
            raise LLMError("this workspace has no LLM provider configured — add your own key under "
                           "Settings › LLM (light plan) or upgrade to premium")
    block = _effective_config()
    roles = block.get("roles") or {}
    entry = roles.get(role) or roles.get("default") or {}
    provider_name = entry.get("provider") or _FALLBACK_PROVIDER
    resolved_model = model or entry.get("model") or _FALLBACK_MODEL
    return _get_provider(provider_name), resolved_model


# ---------------------------------------------------------------------------
# Usage metering (Phase 6a): every adapter reports prompt/completion tokens;
# recorded fire-and-forget on the main loop (llm.chat runs in executor threads).
# ---------------------------------------------------------------------------

def _record_usage(org_id: Optional[int], role: str, provider: ProviderConfig, model: str,
                  usage: Optional[dict], surface: Optional[str] = None) -> None:
    if not isinstance(org_id, int) or isinstance(org_id, bool) or not org_id or not usage:
        return
    try:
        import plans as _plans
        db = context.db_module
        if not (context.DB_AVAILABLE and db):
            return
        pt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        ct = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        cost = _plans.estimate_cost(model, pt, ct, context.config)
        coro = db.record_llm_usage(org_id, provider=provider.name, model=model, prompt_tokens=pt,
                                   completion_tokens=ct, cost_usd=cost, role=role,
                                   surface=surface or role, source="python")
        loop = getattr(db, "_main_loop", None)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            running.create_task(coro)
        elif loop is not None:
            asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            coro.close()
        # keep the cached month cost roughly current so budgets bite without a refetch
        hit = _org_overlays.get(org_id)
        if hit and hit[1] and cost:
            hit[1]["month_cost"] = hit[1].get("month_cost", 0.0) + cost
    except Exception as exc:
        logger.debug("usage record skipped: %s", exc)


# ---------------------------------------------------------------------------
# Retry (ported from routers/knowledge.py — the >10-client bulk-mail lever)
# ---------------------------------------------------------------------------

_MAX_ATTEMPTS = 4


def _with_retry(fn, *, what: str):
    """Run fn() with exponential backoff on transient failures (429, 5xx,
    timeouts, connection drops) and on null content (ValueError)."""
    last_err: Optional[Exception] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return fn()
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError,
                ValueError, KeyError) as exc:
            last_err = exc
        if attempt < _MAX_ATTEMPTS - 1:
            delay = min(2 ** attempt, 8) + random.uniform(0, 0.75)
            logger.warning("%s failed (attempt %d/%d): %s — retry in %.1fs",
                           what, attempt + 1, _MAX_ATTEMPTS, last_err, delay)
            time.sleep(delay)
    raise LLMError(f"{what} failed after {_MAX_ATTEMPTS} attempts: {last_err}")


# ---------------------------------------------------------------------------
# Message / tool normalization
# ---------------------------------------------------------------------------

def _normalize_tools(tools: Optional[list]) -> list[dict]:
    """Accept {name, description, parameters} dicts, OpenAI wrappers, or
    agents.tools.Tool objects; return the plain neutral form."""
    out = []
    for t in tools or []:
        if isinstance(t, dict):
            if t.get("type") == "function" and "function" in t:
                fn = t["function"]
                out.append({"name": fn.get("name", ""),
                            "description": fn.get("description", ""),
                            "parameters": fn.get("parameters", {})})
            else:
                out.append({"name": t.get("name", ""),
                            "description": t.get("description", ""),
                            "parameters": t.get("parameters", t.get("input_schema", {}))})
        else:  # duck-typed Tool object
            out.append({"name": t.name, "description": t.description, "parameters": t.parameters})
    return out


def _ensure_messages(prompt: Optional[str], messages: Optional[list]) -> list[dict]:
    if messages is not None:
        return messages
    if prompt is None:
        raise LLMError("either prompt or messages is required")
    return [{"role": "user", "content": prompt}]


def _to_openai_messages(messages: list[dict]) -> list[dict]:
    """Neutral → OpenAI wire format. Always emits tool_call_id / call ids."""
    out = []
    synth = 0
    for m in messages:
        role = m["role"]
        if role == "tool":
            tcid = m.get("tool_call_id")
            if not tcid:
                tcid = f"call_{synth}"
                synth += 1
            out.append({"role": "tool", "tool_call_id": tcid, "content": m.get("content") or ""})
        elif role == "assistant" and m.get("tool_calls"):
            calls = []
            for i, tc in enumerate(m["tool_calls"]):
                args = tc.get("arguments", {})
                calls.append({
                    "id": tc.get("id") or f"call_{i}",
                    "type": "function",
                    "function": {"name": tc["name"],
                                 "arguments": args if isinstance(args, str) else json.dumps(args)},
                })
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


def _parse_openai_tool_calls(raw_calls: list) -> list[dict]:
    tool_calls = []
    for i, tc in enumerate(raw_calls or []):
        fn = tc.get("function", {})
        args = fn.get("arguments", "{}")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        tool_calls.append({
            "id": tc.get("id") or f"call_{i}",
            "name": fn.get("name", ""),
            "arguments": args,
        })
    return tool_calls


def _to_anthropic_messages(messages: list[dict]) -> tuple[Optional[str], list[dict]]:
    """Neutral → Anthropic messages + system string."""
    system = None
    out = []
    for m in messages:
        role = m["role"]
        if role == "system":
            system = m["content"]
        elif role == "tool":
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": m.get("content") or "",
                }],
            })
        elif role == "assistant" and m.get("tool_calls"):
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for i, tc in enumerate(m["tool_calls"]):
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or f"call_{i}",
                    "name": tc["name"],
                    "input": tc.get("arguments", {}),
                })
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return system, out


# ---------------------------------------------------------------------------
# openai-compat adapter
# ---------------------------------------------------------------------------

def _openai_headers(provider: ProviderConfig) -> dict:
    headers = {"Content-Type": "application/json"}
    key = provider.resolve_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if "openrouter.ai" in provider.base_url:
        headers.setdefault("HTTP-Referer", "https://github.com/buzzowl")
        headers.setdefault("X-Title", "Buzzowl")
    headers.update(provider.headers)
    return headers


def _openai_chat(provider: ProviderConfig, model: str, messages: list[dict],
                 tool_defs: list[dict], max_tokens: int, timeout: int,
                 stream: bool = False) -> requests.Response:
    if not provider.base_url:
        raise LLMError(f"provider {provider.name!r} has no base_url")
    payload: dict[str, Any] = {"model": model, "messages": messages}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if tool_defs:
        payload["tools"] = [
            {"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t["parameters"]}}
            for t in tool_defs
        ]
        payload["tool_choice"] = "auto"
    if stream:
        payload["stream"] = True
    resp = requests.post(
        f"{provider.base_url}/chat/completions",
        headers=_openai_headers(provider), json=payload,
        timeout=timeout, stream=stream,
    )
    if resp.status_code == 429 or resp.status_code >= 500:
        raise requests.HTTPError(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# anthropic adapter
# ---------------------------------------------------------------------------

def _anthropic_client(provider: ProviderConfig):
    try:
        import anthropic
    except ImportError as e:
        raise LLMError(
            "provider kind 'anthropic' requires the anthropic package — "
            "pip install anthropic"
        ) from e
    kwargs: dict = {"api_key": provider.resolve_key()}
    if provider.base_url:
        kwargs["base_url"] = provider.base_url
    return anthropic.Anthropic(**kwargs)


def _anthropic_chat(provider: ProviderConfig, model: str, messages: list[dict],
                    tool_defs: list[dict], max_tokens: int, timeout: int) -> dict:
    client = _anthropic_client(provider)
    system, a_msgs = _to_anthropic_messages(messages)
    kwargs: dict[str, Any] = dict(model=model, max_tokens=max_tokens or 4096,
                                  messages=a_msgs, timeout=float(timeout))
    if system:
        kwargs["system"] = system
    if tool_defs:
        kwargs["tools"] = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in tool_defs
        ]
    response = client.messages.create(**kwargs)
    content = ""
    tool_calls = []
    for block in response.content:
        if block.type == "text":
            content = block.text
        elif block.type == "tool_use":
            tool_calls.append({"id": block.id, "name": block.name, "arguments": block.input})
    u = getattr(response, "usage", None)
    usage = {"prompt_tokens": getattr(u, "input_tokens", 0), "completion_tokens": getattr(u, "output_tokens", 0)} if u else None
    return {"content": content, "tool_calls": tool_calls, "_usage": usage}


# ---------------------------------------------------------------------------
# pi adapter — text-only bridge through the Pi agent service's /complete.
# Lets Python-side roles (triage, summary, NBA reasons) use providers that
# only Pi can authenticate: the subscription-OAuth ones (ChatGPT-Codex,
# Copilot). Provider config: {kind: pi, pi_provider: openai-codex}. The Pi
# URL/token come from agent_service_url_pi / agent_service_token.
# ---------------------------------------------------------------------------

def _pi_complete(provider: ProviderConfig, model: str, messages: list[dict],
                 max_tokens: int, timeout: int, org_id: Optional[int] = None) -> dict:
    cfg = context.config
    base = (provider.base_url or cfg.get("agent_service_url_pi")
            or cfg.get("agent_service_url") or "http://localhost:8001").rstrip("/")
    token = cfg.get("agent_service_token", "") or os.environ.get("AGENT_SERVICE_TOKEN", "")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    pi_provider = provider.headers.get("pi_provider") if provider.headers else None
    payload = {
        "provider": pi_provider or provider.name,
        "model": model,
        "org_id": org_id,
        "messages": [{"role": m["role"], "content": m.get("content") or ""} for m in messages
                     if m.get("role") in ("system", "user", "assistant")],
        "max_tokens": max_tokens,
    }
    resp = requests.post(f"{base}/complete", headers=headers, json=payload, timeout=timeout)
    if resp.status_code == 429 or resp.status_code >= 500:
        raise requests.HTTPError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    resp.raise_for_status()
    data = resp.json() or {}
    text = data.get("text") or ""
    if not text:
        raise ValueError("null content from Pi /complete")
    return {"content": text, "tool_calls": [], "_usage": data.get("usage")}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def chat(messages: Optional[list] = None, tools: Optional[list] = None, *,
         prompt: Optional[str] = None, role: str = "default",
         model: Optional[str] = None, max_tokens: int = 4096,
         timeout: int = 180, org_id: Optional[int] = None,
         surface: Optional[str] = None) -> dict:
    """Chat completion with optional tool calling.

    Returns {"content": str, "tool_calls": [{"id","name","arguments"}]}.
    org_id (multi-tenant) selects the org's own providers / plan budget and
    meters the call; surface labels the usage row (defaults to role).
    """
    provider, resolved_model = resolve(role, model, org_id)
    msgs = _ensure_messages(prompt, messages)
    tool_defs = _normalize_tools(tools)

    def _done(res: dict) -> dict:
        _record_usage(org_id, role, provider, resolved_model, res.pop("_usage", None), surface)
        return res

    if provider.kind == "anthropic":
        return _done(_with_retry(
            partial(_anthropic_chat, provider, resolved_model, msgs, tool_defs,
                    max_tokens, timeout),
            what=f"LLM chat ({provider.name}/{resolved_model})",
        ))

    if provider.kind == "pi":
        if tool_defs:
            raise LLMError("provider kind 'pi' is text-only (no tool calling) — "
                           "use it for triage/summary/reason roles, not chat tool loops")
        return _done(_with_retry(
            partial(_pi_complete, provider, resolved_model, msgs, max_tokens, timeout, org_id),
            what=f"LLM complete via Pi ({provider.name}/{resolved_model})",
        ))

    def _call() -> dict:
        wire_msgs = _to_openai_messages(msgs)
        resp = _openai_chat(provider, resolved_model, wire_msgs, tool_defs,
                            max_tokens, timeout)
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        content = msg.get("content") or ""
        tool_calls = _parse_openai_tool_calls(msg.get("tool_calls"))
        if not content and not tool_calls:
            finish = choice.get("finish_reason", "unknown")
            raise ValueError(f"null content (finish_reason={finish})")
        return {"content": content, "tool_calls": tool_calls, "_usage": data.get("usage")}

    return _done(_with_retry(_call, what=f"LLM chat ({provider.name}/{resolved_model})"))


def complete(prompt: Optional[str] = None, *, messages: Optional[list] = None,
             role: str = "default", model: Optional[str] = None,
             max_tokens: int = 4096, timeout: int = 180,
             org_id: Optional[int] = None, surface: Optional[str] = None) -> str:
    """Plain text completion (no tools). Returns the stripped response text."""
    result = chat(messages, None, prompt=prompt, role=role, model=model,
                  max_tokens=max_tokens, timeout=timeout, org_id=org_id, surface=surface)
    return (result["content"] or "").strip()


async def achat(messages: Optional[list] = None, tools: Optional[list] = None,
                **kwargs) -> dict:
    await ensure_org_overlay(kwargs.get("org_id"))
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(chat, messages, tools, **kwargs))


async def acomplete(prompt: Optional[str] = None, **kwargs) -> str:
    await ensure_org_overlay(kwargs.get("org_id"))
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(complete, prompt, **kwargs))


def stream(prompt: Optional[str] = None, *, messages: Optional[list] = None,
           role: str = "default", model: Optional[str] = None,
           max_tokens: int = 2048, timeout: int = 300,
           org_id: Optional[int] = None) -> Iterator[str]:
    """Stream response text chunks. No retry — a stream either runs or raises
    fast so the caller can degrade gracefully (e.g. skip the live summary)."""
    provider, resolved_model = resolve(role, model, org_id)
    msgs = _ensure_messages(prompt, messages)

    if provider.kind == "pi":
        # No streaming over the Pi bridge — return the whole text as one chunk.
        yield _pi_complete(provider, resolved_model, msgs, max_tokens, timeout)["content"]
        return

    if provider.kind == "anthropic":
        client = _anthropic_client(provider)
        system, a_msgs = _to_anthropic_messages(msgs)
        kwargs: dict[str, Any] = dict(model=resolved_model,
                                      max_tokens=max_tokens or 2048,
                                      messages=a_msgs)
        if system:
            kwargs["system"] = system
        with client.messages.stream(**kwargs) as s:
            for text in s.text_stream:
                yield text
        return

    wire_msgs = _to_openai_messages(msgs)
    resp = _openai_chat(provider, resolved_model, wire_msgs, [], max_tokens,
                        timeout, stream=True)
    for raw_line in resp.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            delta = (json.loads(data).get("choices") or [{}])[0].get("delta", {})
        except json.JSONDecodeError:
            continue
        chunk = delta.get("content")
        if chunk:
            yield chunk


def provider_for_brain(brain: str) -> str:
    """Map a legacy brain value to a Pi-service provider name.

    "claude" → "anthropic"; "openrouter"/"ollama" pass through; any other
    value passes through too so a custom provider name (e.g. "lmstudio") set
    in agent_service_brain reaches Pi unchanged. Empty → "openrouter".
    """
    return {"claude": "anthropic"}.get(brain, brain or "openrouter")


def status() -> list[dict]:
    """Configured providers + role assignments + cheap reachability check.

    openai-compat providers are probed via GET {base_url}/models (5s timeout);
    anthropic is reported by key presence only (no billable probe).
    """
    block = _effective_config()
    roles = block.get("roles") or {}
    out = []
    for name in (block.get("providers") or {}):
        try:
            provider = _get_provider(name)
        except LLMError:
            continue
        info: dict[str, Any] = {
            "name": name,
            "kind": provider.kind,
            "base_url": provider.base_url,
            "has_key": bool(provider.resolve_key()),
            "roles": sorted(r for r, e in roles.items() if e.get("provider") == name),
        }
        if provider.kind == "openai-compat" and provider.base_url:
            try:
                r = requests.get(f"{provider.base_url}/models",
                                 headers=_openai_headers(provider), timeout=5)
                info["reachable"] = r.status_code < 500
            except requests.RequestException:
                info["reachable"] = False
        else:
            info["reachable"] = None   # not probed
        out.append(info)
    return out
