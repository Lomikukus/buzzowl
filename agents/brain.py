"""
agents/brain.py — Swappable LLM backends for the agent loop.

OpenAICompatibleBrain: default — routes through llm.py (provider layer), covers
    OpenRouter, OpenAI, Ollama (/v1), LM Studio and any OpenAI-compatible base_url.
OllamaBrain: legacy direct Ollama /api/chat path (kept for installs without an llm: block).
ClaudeBrain: opt-in, requires anthropic_api_key in config.yaml.
OpenRouterBrain: legacy direct OpenRouter path (kept for benchmark configs / imports).

All implement:
    async think(messages: list[dict], tools: list[Tool]) -> BrainResponse

Neutral message format (agent-internal):
    {"role": "system",    "content": "..."}
    {"role": "user",      "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [{"id": ..., "name": ..., "arguments": {...}}]}
    {"role": "tool",      "content": "...", "tool_call_id": "...", "tool_name": "..."}
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from agents.tools import Tool

logger = logging.getLogger("whisper.agents.brain")


@dataclass
class BrainResponse:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return len(self.tool_calls) == 0


# ---------------------------------------------------------------------------
# OpenAI-compatible brain (routes through llm.py)
# ---------------------------------------------------------------------------

class OpenAICompatibleBrain:
    """Routes through llm.py — covers OpenRouter, OpenAI, Ollama (/v1), LM Studio,
    and any OpenAI-compatible base_url. Provider/model resolved per llm config role.

    Needs no api_key wiring: llm.resolve() picks provider + key from the llm:
    block (or its legacy-key synthesis). Failures propagate as llm.LLMError,
    matching the raise-through behavior of the other brains.
    """

    def __init__(self, role: str = "default", model: str | None = None, org_id: int | None = None):
        self.org_id = org_id
        self.role = role
        self.model = model  # None → the role's configured model

    async def think(self, messages: list[dict], tools: list["Tool"]) -> BrainResponse:
        import llm  # lazy — keeps brain.py importable without pulling provider config at import time

        result = await llm.achat(messages, tools, role=self.role, model=self.model, org_id=self.org_id, surface="agent")
        return BrainResponse(
            content=result.get("content") or "",
            tool_calls=result.get("tool_calls") or [],
        )


# ---------------------------------------------------------------------------
# Ollama brain
# ---------------------------------------------------------------------------

class OllamaBrain:
    def __init__(self, model: str = "llama3.2", base_url: str = "http://localhost:11434", num_ctx: int = 16384):
        self.model = model
        self.base_url = base_url
        self.num_ctx = num_ctx

    def _to_ollama_messages(self, messages: list[dict]) -> list[dict]:
        """Convert neutral format → Ollama /api/chat format."""
        out = []
        for m in messages:
            role = m["role"]
            if role == "tool":
                out.append({"role": "tool", "content": m["content"]})
            elif role == "assistant" and m.get("tool_calls"):
                calls = [
                    {"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in m["tool_calls"]
                ]
                out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            else:
                out.append({"role": role, "content": m.get("content") or ""})
        return out

    def _call_ollama(self, messages: list[dict], tool_defs: list[dict]) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tool_defs,
            "stream": False,
            "options": {"num_ctx": self.num_ctx},
        }
        resp = requests.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()

    async def think(self, messages: list[dict], tools: list["Tool"]) -> BrainResponse:
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]
        ollama_msgs = self._to_ollama_messages(messages)
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, partial(self._call_ollama, ollama_msgs, tool_defs)
        )

        msg = data.get("message", {})
        content = msg.get("content") or ""
        raw_calls = msg.get("tool_calls") or []

        tool_calls = []
        for i, tc in enumerate(raw_calls):
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append({
                "id": str(i),
                "name": fn.get("name", ""),
                "arguments": args,
            })

        return BrainResponse(content=content, tool_calls=tool_calls)


# ---------------------------------------------------------------------------
# Claude brain
# ---------------------------------------------------------------------------

class ClaudeBrain:
    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str = ""):
        self.model = model
        self.api_key = api_key
        self._client = None

    def _get_client(self):
        if not self._client:
            try:
                import anthropic
                self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
            except ImportError as e:
                raise RuntimeError("anthropic package not installed — run: pip install anthropic") from e
        return self._client

    def _to_claude_messages(self, messages: list[dict]) -> tuple[str | None, list[dict]]:
        """Convert neutral format → Claude messages + system string."""
        system = None
        out = []
        pending_tool_calls = []

        for m in messages:
            role = m["role"]

            if role == "system":
                system = m["content"]

            elif role == "tool":
                # Claude expects tool results as user messages
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id", ""),
                        "content": m["content"],
                    }],
                })

            elif role == "assistant" and m.get("tool_calls"):
                content_blocks = []
                if m.get("content"):
                    content_blocks.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": tc["name"],
                        "input": tc["arguments"],
                    })
                out.append({"role": "assistant", "content": content_blocks})

            elif role == "user":
                out.append({"role": "user", "content": m["content"]})

            elif role == "assistant":
                out.append({"role": "assistant", "content": m.get("content") or ""})

        return system, out

    async def think(self, messages: list[dict], tools: list["Tool"]) -> BrainResponse:
        tool_defs = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]
        client = self._get_client()
        system, claude_msgs = self._to_claude_messages(messages)

        kwargs = dict(
            model=self.model,
            max_tokens=4096,
            tools=tool_defs,
            messages=claude_msgs,
        )
        if system:
            kwargs["system"] = system

        response = await client.messages.create(**kwargs)

        tool_calls = []
        content_text = ""
        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input,
                })
            elif block.type == "text":
                content_text = block.text

        return BrainResponse(content=content_text, tool_calls=tool_calls)


# ---------------------------------------------------------------------------
# OpenRouter brain (OpenAI-compatible, routes to any hosted model)
# ---------------------------------------------------------------------------

class OpenRouterBrain:
    """Routes to any model via openrouter.ai using the OpenAI-compatible API.

    Supports tool calling for models that declare it (e.g. anthropic/claude-*,
    openai/gpt-*, meta-llama/llama-3*, mistralai/*, google/gemini-*).
    """

    BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, model: str = "meta-llama/llama-3.3-70b-instruct", api_key: str = ""):
        self.model = model
        self.api_key = api_key

    def _call(self, messages: list[dict], tool_defs: list[dict]) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/buzzowl",
            "X-Title": "Buzzowl",
        }
        payload: dict = {"model": self.model, "messages": messages}
        if tool_defs:
            payload["tools"] = tool_defs
            payload["tool_choice"] = "auto"
        resp = requests.post(self.BASE_URL, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        return resp.json()

    def _to_openai_messages(self, messages: list[dict]) -> list[dict]:
        """Convert neutral format → OpenAI/OpenRouter chat format."""
        out = []
        for m in messages:
            role = m["role"]
            if role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id", ""),
                    "content": m["content"],
                })
            elif role == "assistant" and m.get("tool_calls"):
                calls = [
                    {
                        "id": tc.get("id", str(i)),
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                    }
                    for i, tc in enumerate(m["tool_calls"])
                ]
                out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            else:
                out.append({"role": role, "content": m.get("content") or ""})
        return out

    async def think(self, messages: list[dict], tools: list["Tool"]) -> BrainResponse:
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]
        openai_msgs = self._to_openai_messages(messages)
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, partial(self._call, openai_msgs, tool_defs)
        )

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        content = msg.get("content") or ""
        raw_calls = msg.get("tool_calls") or []

        tool_calls = []
        for i, tc in enumerate(raw_calls):
            fn = tc.get("function", {})
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append({
                "id": tc.get("id", str(i)),
                "name": fn.get("name", ""),
                "arguments": args,
            })

        return BrainResponse(content=content, tool_calls=tool_calls)
