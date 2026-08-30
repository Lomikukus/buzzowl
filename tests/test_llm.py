"""Tests for llm.py — the unified provider layer.

Runs a real HTTP server speaking the OpenAI chat API on a random port, so the
adapter is exercised over the wire (headers, retry, streaming payloads) rather
than via mocks.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import context
import llm


# ---------------------------------------------------------------------------
# Fake OpenAI-compatible server
# ---------------------------------------------------------------------------

class _State:
    def __init__(self):
        self.hits = 0
        self.bodies = []          # parsed request payloads, in order
        self.responses = []       # queue of (status:int, payload:dict) — last repeats


def _make_handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            state.hits += 1
            state.bodies.append(body)
            status, payload = state.responses[min(state.hits - 1, len(state.responses) - 1)]
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # /models reachability probe
            data = b'{"data": []}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return Handler


def _content_response(text="hello", tool_calls=None):
    msg = {"content": text}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return (200, {"choices": [{"message": msg, "finish_reason": "stop"}]})


@pytest.fixture()
def fake_server():
    state = _State()
    state.responses = [_content_response()]
    server = HTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/v1"
    yield base_url, state
    server.shutdown()


@pytest.fixture()
def llm_config(fake_server, monkeypatch):
    base_url, state = fake_server
    cfg = {
        "llm": {
            "providers": {
                "fake": {"kind": "openai-compat", "base_url": base_url, "api_key": "test-key"},
                "anthropic": {"kind": "anthropic", "api_key": "sk-test"},
            },
            "roles": {
                "default": {"provider": "fake", "model": "fake-model"},
                "claude-role": {"provider": "anthropic", "model": "claude-x"},
            },
        }
    }
    monkeypatch.setattr(context, "config", cfg)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)  # fast retries
    return state


# ---------------------------------------------------------------------------
# Basic + retry
# ---------------------------------------------------------------------------

def test_complete_basic(llm_config):
    assert llm.complete("hi") == "hello"
    assert llm_config.hits == 1
    assert llm_config.bodies[0]["model"] == "fake-model"


def test_retry_on_429(llm_config):
    llm_config.responses = [
        (429, {"error": "rate limited"}),
        (429, {"error": "rate limited"}),
        _content_response("recovered"),
    ]
    assert llm.complete("hi") == "recovered"
    assert llm_config.hits == 3


def test_null_content_exhausts_retries(llm_config):
    llm_config.responses = [
        (200, {"choices": [{"message": {"content": None}, "finish_reason": "length"}]}),
    ]
    with pytest.raises(llm.LLMError, match="4 attempts"):
        llm.complete("hi")
    assert llm_config.hits == 4


def test_retry_on_500(llm_config):
    llm_config.responses = [(500, {"error": "boom"}), _content_response("ok")]
    assert llm.complete("hi") == "ok"
    assert llm_config.hits == 2


# ---------------------------------------------------------------------------
# Tool-call normalization
# ---------------------------------------------------------------------------

def test_tool_call_id_synthesized_on_response(llm_config):
    # Provider omits the id — we must synthesize one
    llm_config.responses = [_content_response("", tool_calls=[
        {"function": {"name": "search_kb", "arguments": '{"query": "acme"}'}},
    ])]
    result = llm.chat([{"role": "user", "content": "find acme"}],
                      tools=[{"name": "search_kb", "description": "d",
                              "parameters": {"type": "object", "properties": {}}}])
    assert result["tool_calls"] == [
        {"id": "call_0", "name": "search_kb", "arguments": {"query": "acme"}}
    ]


def test_tool_call_id_emitted_on_wire(llm_config):
    # Neutral tool message WITHOUT tool_call_id must still carry one on the wire
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"name": "search_kb", "arguments": {"query": "x"}},   # no id
        ]},
        {"role": "tool", "content": "result text"},               # no tool_call_id
    ]
    llm.chat(messages)
    wire = llm_config.bodies[0]["messages"]
    assistant = wire[1]
    tool = wire[2]
    assert assistant["tool_calls"][0]["id"] == "call_0"
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"query": "x"}'
    assert tool["tool_call_id"]           # present and non-empty
    assert tool["role"] == "tool"


def test_openai_tool_wrapper_accepted(llm_config):
    llm.chat([{"role": "user", "content": "q"}], tools=[
        {"type": "function", "function": {"name": "t1", "description": "d",
                                          "parameters": {"type": "object"}}},
    ])
    sent = llm_config.bodies[0]["tools"]
    assert sent[0]["function"]["name"] == "t1"


# ---------------------------------------------------------------------------
# Role resolution + legacy synthesis
# ---------------------------------------------------------------------------

def test_unknown_role_falls_back_to_default(llm_config):
    provider, model = llm.resolve("does-not-exist")
    assert provider.name == "fake"
    assert model == "fake-model"


def test_model_override(llm_config):
    provider, model = llm.resolve("default", model="override-model")
    assert model == "override-model"


def test_legacy_synthesis(monkeypatch):
    monkeypatch.setattr(context, "config", {
        "research_brain": "claude", "research_model": "claude-sonnet-4-6",
        "pipeline_brain": "openrouter", "pipeline_model": "some/model",
        "agent_brain": "openrouter", "agent_model": "agent/model",
        "ollama_model": "qwen3.5",
        "anthropic_api_key": "sk-legacy",
    })
    provider, model = llm.resolve("research")
    assert provider.kind == "anthropic"
    assert provider.api_key == "sk-legacy"
    assert model == "claude-sonnet-4-6"

    provider, model = llm.resolve("pipeline")
    assert provider.name == "openrouter"
    assert model == "some/model"

    provider, model = llm.resolve("summary")
    assert provider.name == "ollama"
    assert provider.base_url.endswith("/v1")
    assert provider.resolve_key() == "local"
    assert model == "qwen3.5"


def test_legacy_unknown_brain_falls_back_to_openrouter(monkeypatch):
    monkeypatch.setattr(context, "config", {
        "research_brain": "hermes", "research_model": "m",
    })
    provider, _ = llm.resolve("research")
    assert provider.name == "openrouter"


# ---------------------------------------------------------------------------
# Anthropic guard + status
# ---------------------------------------------------------------------------

def test_anthropic_missing_package(llm_config, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # forces ImportError
    with pytest.raises(llm.LLMError, match="anthropic package"):
        llm.chat([{"role": "user", "content": "hi"}], role="claude-role")


def test_status_reports_providers(llm_config):
    entries = {e["name"]: e for e in llm.status()}
    assert entries["fake"]["reachable"] is True
    assert entries["fake"]["has_key"] is True
    assert entries["anthropic"]["reachable"] is None   # not probed
    assert "default" in entries["fake"]["roles"]


def test_status_cheap_platform_only(llm_config):
    assert llm.status_cheap() is True                 # platform 'fake' provider has a key


def test_status_cheap_false_when_no_key_anywhere(monkeypatch):
    monkeypatch.setattr(context, "config", {
        "llm": {"providers": {"bare": {"kind": "openai-compat", "base_url": "http://x"}},
                "roles": {"default": {"provider": "bare", "model": "m"}}},
    })
    assert llm.status_cheap() is False


def test_status_cheap_org_overlay(llm_config):
    """status_cheap() mirrors resolve()'s branch structure exactly — it must
    answer for whatever provider resolve() would actually pick, not "does any
    provider anywhere have a key"."""
    import time

    def seed(ov):
        llm._org_overlays[7] = (time.monotonic() + 60, ov)

    try:
        # light org, own provider with a real key -> resolve() picks it, key present
        seed({"plan": "light", "enforce": True,
              "providers": {"own": {"kind": "openai-compat", "base_url": "http://own", "api_key": "ok"}},
              "roles": {"default": {"provider": "own", "model": "m"}}})
        assert llm.status_cheap(org_id=7) is True

        # DANGEROUS ROW: light org, own provider present but its key is EMPTY,
        # not enforced. resolve() still picks the org's own (keyless) provider
        # here — it never reaches the platform — so the call would fail; a
        # status that checked the platform instead would lie about that.
        seed({"plan": "light", "enforce": False,
              "providers": {"own": {"kind": "openai-compat", "base_url": "http://own", "api_key": ""}},
              "roles": {"default": {"provider": "own", "model": "m"}}})
        assert llm.status_cheap(org_id=7) is False

        # same shape, enforced -> also False (resolve() picks the same keyless provider)
        seed({"plan": "light", "enforce": True,
              "providers": {"own": {"kind": "openai-compat", "base_url": "http://own", "api_key": ""}},
              "roles": {"default": {"provider": "own", "model": "m"}}})
        assert llm.status_cheap(org_id=7) is False

        # light org with no providers of its own at all, enforced -> resolve()
        # refuses outright, never reaches the platform
        seed({"plan": "light", "enforce": True, "providers": {}, "roles": {}})
        assert llm.status_cheap(org_id=7) is False

        # same, but not enforced -> resolve() falls back to the platform
        seed({"plan": "light", "enforce": False, "providers": {}, "roles": {}})
        assert llm.status_cheap(org_id=7) is True

        # premium org -> platform check regardless of its own (empty) providers
        seed({"plan": "premium", "enforce": True, "providers": {}, "roles": {}})
        assert llm.status_cheap(org_id=7) is True

        # dangling role: 'chat' points at a provider name no longer present in
        # ov["providers"] (its row was removed while a role still pointed at
        # it). Enforced -> mirrors resolve()'s new refusal instead of quietly
        # checking the platform.
        seed({"plan": "light", "enforce": True,
              "providers": {"own": {"kind": "openai-compat", "base_url": "http://own", "api_key": "ok"}},
              "roles": {"chat": {"provider": "ghost", "model": "m"}}})
        assert llm.status_cheap(role="chat", org_id=7) is False

        # same dangling role, not enforced -> falls through to the platform
        # check, exactly like resolve() does in this same edge case
        seed({"plan": "light", "enforce": False,
              "providers": {"own": {"kind": "openai-compat", "base_url": "http://own", "api_key": "ok"}},
              "roles": {"chat": {"provider": "ghost", "model": "m"}}})
        assert llm.status_cheap(role="chat", org_id=7) is True   # platform 'fake' has a key (llm_config fixture)
    finally:
        llm.invalidate_org_overlay(7)


def test_status_cheap_role_param_org_without_default_role(llm_config):
    """A role param lets status_cheap answer for a role the org never gave a
    'default' entry — it must not fall back to treating the org as having no
    role config at all."""
    import time

    try:
        llm._org_overlays[7] = (time.monotonic() + 60, {
            "plan": "light", "enforce": True,
            "providers": {
                "keyless": {"kind": "openai-compat", "base_url": "http://a", "api_key": ""},
                "keyed": {"kind": "openai-compat", "base_url": "http://b", "api_key": "k"},
            },
            "roles": {"chat": {"provider": "keyed", "model": "m"}},   # no 'default' role at all
        })
        assert llm.status_cheap(role="chat", org_id=7) is True
    finally:
        llm.invalidate_org_overlay(7)


def test_status_cheap_role_param_platform(monkeypatch):
    """Platform (no org overlay): default role is keyless but 'summary' points
    at a different, keyed provider — status_cheap must answer per-role, not
    just for 'default'."""
    monkeypatch.setattr(context, "config", {
        "llm": {
            "providers": {
                "empty": {"kind": "openai-compat", "base_url": "http://x"},
                "full": {"kind": "openai-compat", "base_url": "http://y", "api_key": "key"},
            },
            "roles": {
                "default": {"provider": "empty", "model": "m1"},
                "summary": {"provider": "full", "model": "m2"},
            },
        },
    })
    assert llm.status_cheap() is False
    assert llm.status_cheap(role="summary") is True


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def test_stream_sse(fake_server, monkeypatch):
    base_url, state = fake_server

    # SSE needs a raw-bytes response — swap in a dedicated handler
    class SSEHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            chunks = [
                b'data: {"choices": [{"delta": {"content": "Hel"}}]}\n\n',
                b'data: {"choices": [{"delta": {"content": "lo"}}]}\n\n',
                b"data: [DONE]\n\n",
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for c in chunks:
                self.wfile.write(c)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), SSEHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(context, "config", {
        "llm": {
            "providers": {"sse": {"kind": "openai-compat",
                                  "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                                  "api_key": "k"}},
            "roles": {"default": {"provider": "sse", "model": "m"}},
        }
    })
    try:
        assert "".join(llm.stream("hi")) == "Hello"
    finally:
        server.shutdown()
