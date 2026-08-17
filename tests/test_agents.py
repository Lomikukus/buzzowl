"""
tests/test_agents.py — Unit tests for the agent runtime.

No DB, no Ollama, no network — all external calls are mocked.
Tests cover:
  - OllamaBrain / ClaudeBrain message format conversion
  - Agent loop: tool dispatch, multi-iteration, error handling, MAX_ITERATIONS cap
  - build_tools factory
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.base import Agent, MAX_ITERATIONS
from agents.brain import BrainResponse, ClaudeBrain, OllamaBrain
from agents.tools import Tool, build_tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool(name: str = "mock_tool", fn=None) -> Tool:
    return Tool(
        name=name,
        description="A mock tool",
        parameters={"type": "object", "properties": {"arg": {"type": "string"}}},
        fn=fn or AsyncMock(return_value={"result": "ok"}),
    )


def _make_agent(brain, tools=None, name="research") -> Agent:
    return Agent(
        name=name,
        brain=brain,
        tools=tools or [],
        org_id=1,
        run_id=99,
        instructions="You are a test agent.",
    )


def _brain_sequence(*responses: BrainResponse):
    """Return an AsyncMock brain.think that yields responses in order, then repeats last."""
    seq = list(responses)
    calls = [0]

    async def _think(messages, tools):
        idx = min(calls[0], len(seq) - 1)
        calls[0] += 1
        return seq[idx]

    brain = MagicMock()
    brain.think = _think
    return brain


# ---------------------------------------------------------------------------
# BrainResponse
# ---------------------------------------------------------------------------

class TestBrainResponse:
    def test_done_when_no_tool_calls(self):
        r = BrainResponse(content="finished", tool_calls=[])
        assert r.done is True

    def test_not_done_when_tool_calls_present(self):
        r = BrainResponse(content="", tool_calls=[{"name": "x", "arguments": {}}])
        assert r.done is False


# ---------------------------------------------------------------------------
# OllamaBrain — message format conversion
# ---------------------------------------------------------------------------

class TestOllamaBrainMessageFormat:
    def test_plain_system_and_user_pass_through(self):
        brain = OllamaBrain()
        msgs = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
        ]
        result = brain._to_ollama_messages(msgs)
        assert result == [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
        ]

    def test_tool_result_message(self):
        brain = OllamaBrain()
        msgs = [{"role": "tool", "content": '{"x": 1}', "tool_call_id": "0", "tool_name": "search_kb"}]
        result = brain._to_ollama_messages(msgs)
        assert result == [{"role": "tool", "content": '{"x": 1}'}]

    def test_assistant_with_tool_calls(self):
        brain = OllamaBrain()
        msgs = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "0", "name": "get_client", "arguments": {"name": "ACME"}}],
        }]
        result = brain._to_ollama_messages(msgs)
        assert result[0]["role"] == "assistant"
        fn = result[0]["tool_calls"][0]["function"]
        assert fn["name"] == "get_client"
        assert fn["arguments"] == {"name": "ACME"}

    def test_assistant_with_no_content_defaults_to_empty_string(self):
        brain = OllamaBrain()
        msgs = [{"role": "assistant", "content": None, "tool_calls": []}]
        result = brain._to_ollama_messages(msgs)
        assert result[0]["content"] == ""


class TestOllamaBrainThink:
    @pytest.mark.asyncio
    async def test_returns_tool_calls_from_response(self):
        brain = OllamaBrain()
        mock_resp = {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_client", "arguments": {"name": "ACME"}}}],
            }
        }
        with patch.object(brain, "_call_ollama", return_value=mock_resp):
            tools = [_make_tool("get_client")]
            response = await brain.think([{"role": "user", "content": "test"}], tools)

        assert not response.done
        assert response.tool_calls[0]["name"] == "get_client"
        assert response.tool_calls[0]["arguments"] == {"name": "ACME"}

    @pytest.mark.asyncio
    async def test_returns_done_when_no_tool_calls(self):
        brain = OllamaBrain()
        mock_resp = {"message": {"role": "assistant", "content": "Here is my answer.", "tool_calls": []}}
        with patch.object(brain, "_call_ollama", return_value=mock_resp):
            response = await brain.think([{"role": "user", "content": "test"}], [])

        assert response.done
        assert response.content == "Here is my answer."

    @pytest.mark.asyncio
    async def test_handles_string_arguments(self):
        """Ollama sometimes returns arguments as a JSON string instead of a dict."""
        import json
        brain = OllamaBrain()
        mock_resp = {
            "message": {
                "content": "",
                "tool_calls": [{"function": {"name": "search_kb", "arguments": '{"query": "ACME"}'}}],
            }
        }
        with patch.object(brain, "_call_ollama", return_value=mock_resp):
            response = await brain.think([{"role": "user", "content": "test"}], [])

        assert response.tool_calls[0]["arguments"] == {"query": "ACME"}


# ---------------------------------------------------------------------------
# ClaudeBrain — message format conversion
# ---------------------------------------------------------------------------

class TestClaudeBrainMessageFormat:
    def test_system_extracted_from_messages(self):
        brain = ClaudeBrain()
        msgs = [
            {"role": "system", "content": "Be concise"},
            {"role": "user", "content": "Hello"},
        ]
        system, result = brain._to_claude_messages(msgs)
        assert system == "Be concise"
        assert len(result) == 1
        assert result[0] == {"role": "user", "content": "Hello"}

    def test_no_system_returns_none(self):
        brain = ClaudeBrain()
        msgs = [{"role": "user", "content": "Hi"}]
        system, result = brain._to_claude_messages(msgs)
        assert system is None

    def test_tool_result_becomes_user_content_block(self):
        brain = ClaudeBrain()
        msgs = [{"role": "tool", "content": '{"ok": 1}', "tool_call_id": "abc", "tool_name": "search_kb"}]
        _, result = brain._to_claude_messages(msgs)
        assert result[0]["role"] == "user"
        block = result[0]["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "abc"
        assert block["content"] == '{"ok": 1}'

    def test_assistant_tool_calls_become_tool_use_blocks(self):
        brain = ClaudeBrain()
        msgs = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "tc1", "name": "list_clients", "arguments": {}}],
        }]
        _, result = brain._to_claude_messages(msgs)
        block = result[0]["content"][0]
        assert block["type"] == "tool_use"
        assert block["id"] == "tc1"
        assert block["name"] == "list_clients"

    def test_assistant_with_text_and_tool_calls(self):
        brain = ClaudeBrain()
        msgs = [{
            "role": "assistant",
            "content": "Let me search for that.",
            "tool_calls": [{"id": "tc2", "name": "search_kb", "arguments": {"query": "ACME"}}],
        }]
        _, result = brain._to_claude_messages(msgs)
        blocks = result[0]["content"]
        assert blocks[0] == {"type": "text", "text": "Let me search for that."}
        assert blocks[1]["type"] == "tool_use"


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

class TestAgentLoop:
    @pytest.mark.asyncio
    async def test_stops_when_brain_returns_no_tool_calls(self):
        brain = _brain_sequence(BrainResponse(content="All done.", tool_calls=[]))
        agent = _make_agent(brain)
        result = await agent.run("test task")
        assert result["output"] == "All done."
        assert result["iterations"] == 1
        assert result["tool_calls"] == []

    @pytest.mark.asyncio
    async def test_single_tool_call_then_done(self):
        tool = _make_tool()
        brain = _brain_sequence(
            BrainResponse(content="", tool_calls=[{"id": "1", "name": "mock_tool", "arguments": {"arg": "hi"}}]),
            BrainResponse(content="Done after tool.", tool_calls=[]),
        )
        agent = _make_agent(brain, tools=[tool])
        result = await agent.run("test task")

        tool.fn.assert_awaited_once_with(arg="hi")
        assert result["output"] == "Done after tool."
        assert result["iterations"] == 2
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["tool"] == "mock_tool"
        assert result["tool_calls"][0]["result"] == {"result": "ok"}

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_in_one_iteration(self):
        tool_a = _make_tool("tool_a")
        tool_b = _make_tool("tool_b")
        brain = _brain_sequence(
            BrainResponse(content="", tool_calls=[
                {"id": "1", "name": "tool_a", "arguments": {"arg": "x"}},
                {"id": "2", "name": "tool_b", "arguments": {"arg": "y"}},
            ]),
            BrainResponse(content="Both called.", tool_calls=[]),
        )
        agent = _make_agent(brain, tools=[tool_a, tool_b])
        result = await agent.run("test")

        tool_a.fn.assert_awaited_once_with(arg="x")
        tool_b.fn.assert_awaited_once_with(arg="y")
        assert len(result["tool_calls"]) == 2

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_dict(self):
        brain = _brain_sequence(
            BrainResponse(content="", tool_calls=[{"id": "1", "name": "nonexistent", "arguments": {}}]),
            BrainResponse(content="ok", tool_calls=[]),
        )
        agent = _make_agent(brain)
        result = await agent.run("test task")
        assert "unknown tool" in result["tool_calls"][0]["result"]["error"]
        assert result["output"] == "ok"  # loop continued after error

    @pytest.mark.asyncio
    async def test_tool_exception_caught_not_propagated(self):
        tool = _make_tool()
        tool.fn.side_effect = RuntimeError("DB exploded")
        brain = _brain_sequence(
            BrainResponse(content="", tool_calls=[{"id": "1", "name": "mock_tool", "arguments": {"arg": "x"}}]),
            BrainResponse(content="recovered", tool_calls=[]),
        )
        agent = _make_agent(brain, tools=[tool])
        result = await agent.run("test task")
        assert "DB exploded" in result["tool_calls"][0]["result"]["error"]
        assert result["output"] == "recovered"

    @pytest.mark.asyncio
    async def test_max_iterations_cap_stops_loop(self):
        never_done = BrainResponse(
            content="",
            tool_calls=[{"id": "1", "name": "mock_tool", "arguments": {"arg": "x"}}],
        )
        brain = _brain_sequence(never_done)
        tool = _make_tool()
        agent = _make_agent(brain, tools=[tool])
        result = await agent.run("infinite loop task")

        assert result["iterations"] == MAX_ITERATIONS
        assert "maximum iterations" in result["output"]
        assert tool.fn.await_count == MAX_ITERATIONS

    @pytest.mark.asyncio
    async def test_brain_error_stops_loop_gracefully(self):
        brain = MagicMock()
        brain.think = AsyncMock(side_effect=RuntimeError("Ollama offline"))
        agent = _make_agent(brain)
        result = await agent.run("test task")
        assert "brain error" in result["output"]
        assert result["iterations"] == 1

    @pytest.mark.asyncio
    async def test_tool_call_log_records_iteration_number(self):
        tool = _make_tool()
        brain = _brain_sequence(
            BrainResponse(content="", tool_calls=[{"id": "1", "name": "mock_tool", "arguments": {"arg": "a"}}]),
            BrainResponse(content="", tool_calls=[{"id": "2", "name": "mock_tool", "arguments": {"arg": "b"}}]),
            BrainResponse(content="done", tool_calls=[]),
        )
        agent = _make_agent(brain, tools=[tool])
        result = await agent.run("test")
        assert result["tool_calls"][0]["iteration"] == 1
        assert result["tool_calls"][1]["iteration"] == 2


class TestAgentObserve:
    @pytest.mark.asyncio
    async def test_observe_injects_client_context_into_user_message(self):
        fake_client = {"name": "ACME", "id": 1, "metadata": {"industry": "SaaS"}, "session_count": 3}
        fake_docs = [{"type": "meeting", "title": "Q1 Review", "id": 10}]

        with patch("agents.base._db") as mock_db:
            mock_db.get_client = AsyncMock(return_value=fake_client)
            mock_db.list_documents = AsyncMock(return_value=fake_docs)

            brain = MagicMock()
            brain.think = AsyncMock(return_value=BrainResponse(content="done", tool_calls=[]))
            agent = Agent("research", brain, [], org_id=1, run_id=1, instructions="test")
            await agent.run("research ACME", context={"client_name": "ACME"})

        messages_passed = brain.think.call_args[0][0]
        user_msg = next(m for m in messages_passed if m["role"] == "user")
        assert "ACME" in user_msg["content"]
        assert "Q1 Review" in user_msg["content"]

    @pytest.mark.asyncio
    async def test_observe_skips_gracefully_when_client_not_found(self):
        with patch("agents.base._db") as mock_db:
            mock_db.get_client = AsyncMock(return_value=None)

            brain = MagicMock()
            brain.think = AsyncMock(return_value=BrainResponse(content="done", tool_calls=[]))
            agent = Agent("research", brain, [], org_id=1, run_id=1, instructions="test")
            result = await agent.run("research Unknown", context={"client_name": "Unknown"})

        assert result["output"] == "done"


# ---------------------------------------------------------------------------
# build_tools factory
# ---------------------------------------------------------------------------

class TestBuildTools:
    def test_returns_seven_tools(self):
        tools = build_tools(org_id=1, agent_run_id=42)
        assert len(tools) == 7

    def test_all_tools_have_required_fields(self):
        for t in build_tools(org_id=1):
            assert t.name
            assert t.description
            assert t.parameters.get("type") == "object"
            assert callable(t.fn)

    def test_tool_names(self):
        names = {t.name for t in build_tools(org_id=1)}
        assert names == {
            "search_kb", "get_client", "list_clients",
            "write_document", "update_client_metadata",
            "web_search", "fetch_page",
        }

    @pytest.mark.asyncio
    async def test_web_search_returns_results_shape(self):
        tools = {t.name: t for t in build_tools(org_id=1)}
        # Patch SearXNG (primary) — DDG fallback never reached
        with patch("agents.tools._searxng_search", return_value=[
            {"title": "Horizon Logistik GmbH", "url": "https://horizon.de", "snippet": "Logistics company"},
        ]):
            result = await tools["web_search"].fn(query="Horizon Logistik news")
        assert "results" in result
        assert "query" in result
        assert result["count"] == 1
        assert result["results"][0]["title"] == "Horizon Logistik GmbH"

    @pytest.mark.asyncio
    async def test_web_search_handles_exception(self):
        tools = {t.name: t for t in build_tools(org_id=1)}
        # Both SearXNG and DDG must fail for the error dict to be returned
        with patch("agents.tools._searxng_search", side_effect=RuntimeError("searxng down")), \
             patch("agents.tools._ddg_search", side_effect=RuntimeError("network error")):
            result = await tools["web_search"].fn(query="test query")
        assert "error" in result
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_fetch_page_returns_text_shape(self):
        tools = {t.name: t for t in build_tools(org_id=1)}
        with patch("agents.tools._http_fetch", return_value="Horizon Logistik is a logistics company."):
            result = await tools["fetch_page"].fn(url="https://example.com")
        assert "text" in result
        assert "url" in result
        assert "length" in result
        assert result["url"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_fetch_page_handles_exception(self):
        tools = {t.name: t for t in build_tools(org_id=1)}
        with patch("agents.tools._http_fetch", side_effect=RuntimeError("timeout")):
            result = await tools["fetch_page"].fn(url="https://example.com")
        assert "error" in result
