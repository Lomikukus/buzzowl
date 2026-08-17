"""
agents/runner.py — Async agent runner.

Loads config, builds brain + tools, runs the agent loop,
and persists results to the agent_runs table.

Usage:
    run_id = await db.create_agent_run(org_id, agent_type, task, "manual", user_id)
    await run_agent(run_id, org_id, agent_type, task, client_name=client_name)
"""

import logging
import os
from typing import Optional

import context
import db as _db
from agents.base import Agent, AGENT_INSTRUCTIONS, DEFAULT_INSTRUCTIONS
from agents.brain import OllamaBrain, ClaudeBrain, OpenRouterBrain, OpenAICompatibleBrain
from agents.tools import build_tools

logger = logging.getLogger("whisper.agents.runner")


def _load_brain(orchestrator: bool = False, brain_override: str = "", model_override: str = ""):
    """Build a Brain instance from config (context.config — same live source llm.py reads).

    brain_override / model_override: if set, take precedence over config
    (used by the benchmark script via orchestrator).

    Decision table:
        claude                        → ClaudeBrain (direct Anthropic SDK, needs api key)
        ollama AND no llm: block      → OllamaBrain (pure legacy back-compat)
        anything else (openrouter,
        ollama with llm: block, ...)  → OpenAICompatibleBrain (llm.py resolves
                                        provider/key from the llm: block or
                                        its legacy-key synthesis)
    """
    cfg = context.config

    brain_type = brain_override or cfg.get("agent_brain", "ollama")
    has_llm_block = isinstance(cfg.get("llm"), dict) and bool(cfg["llm"].get("providers"))

    if brain_type == "claude":
        model = model_override or cfg.get("agent_model", "llama3.2")
        api_key = (
            cfg.get("anthropic_api_key", "")
            or os.environ.get("ANTHROPIC_API_KEY", "")
        )
        if not api_key:
            raise ValueError("agent_brain is 'claude' but anthropic_api_key is not set in config.yaml or ANTHROPIC_API_KEY env var")
        return ClaudeBrain(model=model, api_key=api_key)

    if brain_type == "ollama" and not has_llm_block:
        model = model_override or cfg.get("agent_model", "llama3.2")
        ctx_key = "orchestrator_num_ctx" if orchestrator else "agent_num_ctx"
        num_ctx = cfg.get(ctx_key, 32768 if orchestrator else 16384)
        return OllamaBrain(model=model, num_ctx=num_ctx)

    # llm: block present → its default role owns the model choice; the legacy
    # agent_model key is only honored when synthesizing (no llm: block).
    model = model_override or (None if has_llm_block else cfg.get("agent_model"))
    return OpenAICompatibleBrain(role="default", model=model)


async def run_agent(
    run_id: int,
    org_id: int,
    agent_type: str,
    task: str,
    client_name: Optional[str] = None,
    sample_size: Optional[int] = None,
) -> dict:
    """Run an agent end-to-end. Updates agent_runs status throughout.

    Returns the result dict from Agent.run().
    Caller is responsible for creating the agent_runs row before calling this.
    """
    await _db.update_agent_run(run_id, "running")

    try:
        # Org agent is deterministic — no LLM loop needed
        if agent_type == "org":
            from agents.org import run_org_agent
            result = await run_org_agent(run_id, org_id, task, sample_size=sample_size)
            await _db.update_agent_run(
                run_id, "done",
                tool_calls=result["tool_calls"],
                output={"text": result["output"], "iterations": result["iterations"]},
            )
            logger.info("Org agent run %d done", run_id)
            return result

        brain = _load_brain()
        tools = build_tools(org_id, agent_run_id=run_id)
        instructions = AGENT_INSTRUCTIONS.get(agent_type, DEFAULT_INSTRUCTIONS)

        agent = Agent(
            name=agent_type,
            brain=brain,
            tools=tools,
            org_id=org_id,
            run_id=run_id,
            instructions=instructions,
        )

        context = {"client_name": client_name} if client_name else {}
        result = await agent.run(task, context)

        await _db.update_agent_run(
            run_id,
            "done",
            tool_calls=result["tool_calls"],
            output={"text": result["output"], "iterations": result["iterations"]},
        )
        logger.info(
            "Agent run %d (%s) done in %d iterations, %d tool calls",
            run_id, agent_type, result["iterations"], len(result["tool_calls"]),
        )
        return result

    except Exception as exc:
        logger.error("Agent run %d failed: %s", run_id, exc)
        await _db.update_agent_run(run_id, "failed", error=str(exc))
        raise
