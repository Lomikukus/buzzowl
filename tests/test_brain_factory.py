"""
tests/test_brain_factory.py — runner._load_brain factory + OpenAICompatibleBrain.

Drives a real tool-call round-trip against a fake OpenAI-compatible HTTP server
(same pattern as tests/test_llm.py) — the brain built by the factory must route
through llm.py and return a normalized BrainResponse.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import AsyncMock

import pytest

import context
import llm
from agents.brain import (
    BrainResponse,
    ClaudeBrain,
    OllamaBrain,
    OpenAICompatibleBrain,
)
from agents.runner import _load_brain
from agents.tools import Tool


# ---------------------------------------------------------------------------
# Fake OpenAI-compatible server (pattern from tests/test_llm.py)
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
    """context.config with an llm: block whose default role hits the fake server."""
    base_url, state = fake_server
    cfg = {
        "agent_brain": "openrouter",
        "agent_model": "legacy/agent-model",   # must NOT win once llm: exists
        "llm": {
            "providers": {
                "fake": {"kind": "openai-compat", "base_url": base_url, "api_key": "test-key"},
            },
            "roles": {
                "default": {"provider": "fake", "model": "fake-model"},
            },
        },
    }
    monkeypatch.setattr(context, "config", cfg)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)  # fast retries
    return state


def _make_tool(name="search_kb") -> Tool:
    return Tool(
        name=name,
        description="Search the knowledge base",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        fn=AsyncMock(return_value={"results": []}),
    )


# ---------------------------------------------------------------------------
# Tool-call round-trip through the factory-built brain
# ---------------------------------------------------------------------------

class TestOpenAICompatibleBrainRoundTrip:
    async def test_tool_call_round_trip(self, llm_config):
        # Server answers with a tool call missing its id — llm must synthesize call_0
        llm_config.responses = [_content_response("", tool_calls=[
            {"function": {"name": "search_kb", "arguments": '{"query": "acme"}'}},
        ])]

        brain = _load_brain()
        assert isinstance(brain, OpenAICompatibleBrain)

        response = await brain.think(
            [{"role": "user", "content": "find acme"}],
            [_make_tool("search_kb")],
        )

        assert isinstance(response, BrainResponse)
        assert response.tool_calls == [
            {"id": "call_0", "name": "search_kb", "arguments": {"query": "acme"}}
        ]
        assert response.done is False

        # Tool definition made it onto the wire in OpenAI format
        body = llm_config.bodies[0]
        assert body["tools"][0]["function"]["name"] == "search_kb"
        # llm: block's role model wins over the legacy agent_model key
        assert body["model"] == "fake-model"

    async def test_plain_content_response(self, llm_config):
        llm_config.responses = [_content_response("all done")]
        brain = _load_brain()
        response = await brain.think([{"role": "user", "content": "hi"}], [])
        assert response.content == "all done"
        assert response.tool_calls == []
        assert response.done is True

    async def test_llm_error_propagates(self, llm_config):
        # Persistent 500 → llm exhausts retries → LLMError must propagate
        llm_config.responses = [(500, {"error": "boom"})]
        brain = _load_brain()
        with pytest.raises(llm.LLMError):
            await brain.think([{"role": "user", "content": "hi"}], [])

    async def test_model_override_reaches_wire(self, llm_config):
        llm_config.responses = [_content_response("ok")]
        brain = _load_brain(brain_override="openrouter", model_override="bench/model-x")
        assert isinstance(brain, OpenAICompatibleBrain)
        assert brain.model == "bench/model-x"
        await brain.think([{"role": "user", "content": "hi"}], [])
        assert llm_config.bodies[0]["model"] == "bench/model-x"


# ---------------------------------------------------------------------------
# Factory decision table
# ---------------------------------------------------------------------------

class TestLoadBrainFactory:
    def test_claude_still_uses_claude_brain(self, llm_config, monkeypatch):
        monkeypatch.setitem(context.config, "anthropic_api_key", "sk-test")
        brain = _load_brain(brain_override="claude", model_override="claude-x")
        assert isinstance(brain, ClaudeBrain)
        assert brain.model == "claude-x"
        assert brain.api_key == "sk-test"

    def test_claude_without_key_raises(self, llm_config, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValueError, match="anthropic_api_key"):
            _load_brain(brain_override="claude")

    def test_ollama_without_llm_block_is_legacy(self, monkeypatch):
        monkeypatch.setattr(context, "config", {
            "agent_brain": "ollama", "agent_model": "qwen3.5", "agent_num_ctx": 4096,
        })
        brain = _load_brain()
        assert isinstance(brain, OllamaBrain)
        assert brain.model == "qwen3.5"
        assert brain.num_ctx == 4096

    def test_ollama_with_llm_block_routes_through_llm(self, llm_config, monkeypatch):
        monkeypatch.setitem(context.config, "agent_brain", "ollama")
        brain = _load_brain()
        assert isinstance(brain, OpenAICompatibleBrain)

    def test_openrouter_without_llm_block_synthesizes(self, monkeypatch):
        # No llm: block — brain still routes through llm.py, whose legacy
        # synthesis maps agent_brain/agent_model into the default role.
        monkeypatch.setattr(context, "config", {
            "agent_brain": "openrouter", "agent_model": "some/model",
        })
        brain = _load_brain()
        assert isinstance(brain, OpenAICompatibleBrain)
        assert brain.model == "some/model"
        provider, model = llm.resolve("default", brain.model)
        assert provider.name == "openrouter"
        assert model == "some/model"

    def test_unknown_brain_routes_through_llm(self, llm_config):
        brain = _load_brain(brain_override="hermes", model_override="m")
        assert isinstance(brain, OpenAICompatibleBrain)
        assert brain.model == "m"
