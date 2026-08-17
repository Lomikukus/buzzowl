"""
agents/base.py — Agent base class.

Loop: observe → plan → act → reflect → write
- observe:  load relevant KB context before the first LLM call
- plan/act: brain.think() returns tool calls; agent executes them
- reflect:  if no tool calls are returned, the brain is done
- write:    caller (runner.py) persists output to agent_runs
"""

import json
import logging
from typing import Optional

import db as _db
from agents.brain import BrainResponse
from agents.tools import Tool

logger = logging.getLogger("whisper.agents.base")

MAX_ITERATIONS = 10

DEFAULT_INSTRUCTIONS = """\
You are a knowledgeable research assistant for a sales team.
You have access to a knowledge base of meeting transcripts, client profiles, and research notes.
Use the available tools to gather information and complete the given task.
When you have enough information, write a clear, structured response.
Always cite which clients or documents you referenced."""

AGENT_INSTRUCTIONS: dict[str, str] = {
    "research": """\
You are a research agent for a sales team knowledge base.
Your job is to gather and synthesise all available information about a client.
Use get_client to load their profile, search_kb to find related documents and meetings,
then write_document to produce a structured research summary (type='research').
Include: company overview, key contacts and roles, recent meeting outcomes,
open action items, and recommended next steps.""",

    "enrichment": """\
You are an enrichment agent. Given a new meeting session, your job is to:
1. Use search_kb to find the client and contacts mentioned.
2. Use update_client_metadata to add or update any new facts (industry, deal stage, etc.).
3. Use write_document to summarise any new knowledge extracted from the session.
Keep updates factual and concise. Only add information explicitly stated in the meeting.""",

    "osint": """\
You are an OSINT research agent for a sales team.
Your job is to research a company using publicly available information.
Use web_search to find recent news, company facts, and relevant developments.
Use fetch_page to extract content from useful URLs.
Write your findings as a structured OSINT report (type='osint') linked to the client.
Focus on: company overview, recent news, key people, financial signals, and competitive context.""",

    "org": """\
You are an org hygiene agent. Your job is to keep the knowledge base clean:
1. Use list_clients to review all clients.
2. Use search_kb to find documents that may be duplicated or unlinked.
3. Use update_client_metadata to fix or enrich sparse client profiles.
4. Write a brief org-health summary (type='note') noting what was updated.""",

    "meeting-prep": """\
You are a meeting preparation agent.
Given a client name, gather everything known about them and produce a pre-meeting brief.
Use get_client for their profile, search_kb to find recent meetings and action items.
Write the brief as type='summary' linked to the client.
Include: who you're meeting, last discussion topics, open action items, 3 suggested talking points.""",
}


class Agent:
    def __init__(
        self,
        name: str,
        brain,
        tools: list[Tool],
        org_id: int,
        run_id: int,
        instructions: str = "",
    ):
        self.name = name
        self.brain = brain
        self.tools = tools
        self.org_id = org_id
        self.run_id = run_id
        self.instructions = instructions or AGENT_INSTRUCTIONS.get(name, DEFAULT_INSTRUCTIONS)

    def _tool_by_name(self, name: str) -> Optional[Tool]:
        for t in self.tools:
            if t.name == name:
                return t
        return None

    async def _observe(self, context: dict) -> str:
        """Pre-load context before the first brain call. Returns a context string."""
        lines = []
        client_name = context.get("client_name", "")
        if client_name:
            c = await _db.get_client(self.org_id, client_name)
            if c:
                lines.append(f"Client: {c['name']}")
                meta = c.get("metadata") or {}
                if meta:
                    lines.append(f"Metadata: {json.dumps(meta)}")
                docs = await _db.list_documents(self.org_id, client_id=c["id"])
                if docs:
                    lines.append(f"Known documents ({len(docs)}):")
                    for d in docs[:5]:
                        lines.append(f"  - [{d['type']}] {d['title']}")
        return "\n".join(lines)

    async def run(self, task: str, context: dict = {}) -> dict:
        context_str = await self._observe(context)
        user_content = task
        if context_str:
            user_content += f"\n\nAvailable context:\n{context_str}"

        messages = [
            {"role": "system", "content": self.instructions},
            {"role": "user",   "content": user_content},
        ]

        tool_call_log: list[dict] = []
        final_output = ""
        iterations = 0

        for iteration in range(MAX_ITERATIONS):
            iterations = iteration + 1
            try:
                response: BrainResponse = await self.brain.think(messages, self.tools)
            except Exception as exc:
                logger.error("Brain error on iteration %d: %s", iterations, exc, exc_info=True)
                final_output = f"[Agent stopped: brain error — {exc}]"
                break

            logger.debug(
                "Agent %s iter %d: content_len=%d tool_calls=%d done=%s",
                self.name, iterations, len(response.content or ""),
                len(response.tool_calls), response.done,
            )

            if response.done:
                final_output = response.content
                logger.info(
                    "Agent %s done after %d iterations (no tool calls returned)",
                    self.name, iterations,
                )
                break

            # Append assistant turn with tool calls
            messages.append({
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": response.tool_calls,
            })

            # Execute each tool call
            for tc in response.tool_calls:
                tool = self._tool_by_name(tc["name"])
                if not tool:
                    result = {"error": f"unknown tool: {tc['name']}"}
                    logger.warning("Agent %s called unknown tool: %s", self.name, tc["name"])
                else:
                    try:
                        logger.debug("Agent %s calling tool %s with args: %s", self.name, tc["name"], tc["arguments"])
                        result = await tool.fn(**tc["arguments"])
                        logger.debug("Agent %s tool %s result: %s", self.name, tc["name"], str(result)[:200])
                    except Exception as exc:
                        result = {"error": str(exc)}
                        logger.warning("Tool %s failed: %s", tc["name"], exc, exc_info=True)

                tool_call_log.append({
                    "iteration": iterations,
                    "tool":      tc["name"],
                    "arguments": tc["arguments"],
                    "result":    result,
                })

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.get("id", ""),
                    "tool_name":    tc["name"],
                    "content":      json.dumps(result, default=str),
                })

        else:
            # Hit MAX_ITERATIONS without done signal
            logger.warning("Agent %s hit MAX_ITERATIONS (%d)", self.name, MAX_ITERATIONS)
            final_output = "[Agent stopped: maximum iterations reached]"

        return {
            "output":     final_output,
            "tool_calls": tool_call_log,
            "iterations": iterations,
        }
