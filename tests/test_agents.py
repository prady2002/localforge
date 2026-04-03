"""Tests for localforge.agent.agents — individual specialist agents."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from localforge.agent.agents import (
    AnalyzerAgent,
    CoderAgent,
    PlannerAgent,
    ReflectorAgent,
    SummarizerAgent,
    VerifierAgent,
)
from localforge.context_manager.assembler import ContextAssembler
from localforge.context_manager.budget import TokenBudgetManager
from localforge.core.config import LocalForgeConfig
from localforge.core.models import AgentHandoff, AgentRole, FileChunk
from localforge.core.ollama_client import OllamaClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _agent_deps(mock_config):
    """Return (ollama, assembler, budget_manager, config) with mocked LLM."""
    cfg = mock_config
    ollama = OllamaClient(cfg)
    bm = TokenBudgetManager(cfg)
    asm = ContextAssembler(bm, cfg)
    return ollama, asm, bm, cfg


# ---------------------------------------------------------------------------
# test_analyzer_agent_returns_structured_output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyzer_agent_returns_structured_output(_agent_deps) -> None:
    """AnalyzerAgent.execute() should return a message with 'understanding' key."""
    ollama, asm, bm, cfg = _agent_deps

    analysis_data = {
        "understanding": "The login endpoint has a null-check bug.",
        "key_files": ["app/routes.py"],
        "complexity": "simple",
        "approach": "Add guard clause.",
        "risks": [],
        "needs_more_context": False,
        "additional_context_queries": [],
    }

    ollama.chat_structured = AsyncMock(return_value=json.dumps(analysis_data))
    ollama.chat = AsyncMock(return_value=json.dumps(analysis_data))

    agent = AnalyzerAgent(ollama, asm, bm, cfg)
    handoff = AgentHandoff(
        from_role=AgentRole.ORCHESTRATOR,
        to_role=AgentRole.ANALYZER,
        payload={"task": "fix login bug", "repo_path": "."},
        context_chunks=[
            FileChunk(file_path="app/routes.py", start_line=1, end_line=10,
                      content="def login(): pass", score=0.9),
        ],
        instruction="Analyze this task",
    )

    msg = await agent.execute(handoff)

    assert msg.role == AgentRole.ANALYZER
    assert msg.structured_data is not None
    assert "understanding" in msg.structured_data


# ---------------------------------------------------------------------------
# test_planner_agent_consolidates_large_plans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_agent_consolidates_large_plans(_agent_deps) -> None:
    """PlannerAgent should call LLM twice when the initial plan has >20 steps."""
    ollama, asm, bm, cfg = _agent_deps

    big_plan = {
        "reasoning": "Many changes needed.",
        "estimated_complexity": "high",
        "steps": [
            {"step_id": i, "description": f"Step {i}", "files_involved": ["f.py"], "operation": "MODIFY"}
            for i in range(1, 26)  # 25 steps — triggers consolidation
        ],
    }
    consolidated_plan = {
        "reasoning": "Consolidated.",
        "estimated_complexity": "medium",
        "steps": [
            {"step_id": i, "description": f"Consolidated step {i}", "files_involved": ["f.py"], "operation": "MODIFY"}
            for i in range(1, 13)  # 12 steps
        ],
    }

    call_count = 0

    async def fake_chat(messages, system=None, temperature=0.1, stream=True, agent_role="agent"):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return json.dumps(big_plan)
        return json.dumps(consolidated_plan)

    ollama.chat = AsyncMock(side_effect=fake_chat)

    agent = PlannerAgent(ollama, asm, bm, cfg)
    handoff = AgentHandoff(
        from_role=AgentRole.ORCHESTRATOR,
        to_role=AgentRole.PLANNER,
        payload={"task": "big refactor", "analysis": {"understanding": "lots of work"}},
        context_chunks=[],
        instruction="Create execution plan",
    )

    msg = await agent.execute(handoff)

    assert call_count == 2, f"Expected 2 LLM calls (consolidation), got {call_count}"
    assert msg.structured_data is not None
    assert len(msg.structured_data.get("steps", [])) <= 15


# ---------------------------------------------------------------------------
# test_coder_agent_handles_need_more_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coder_agent_handles_need_more_context(_agent_deps) -> None:
    """CoderAgent should return success=False when LLM says need_more_context."""
    ollama, asm, bm, cfg = _agent_deps

    need_ctx = {"error": "need_more_context", "reason": "Cannot find the target function"}

    ollama.chat = AsyncMock(return_value=json.dumps(need_ctx))

    agent = CoderAgent(ollama, asm, bm, cfg)
    handoff = AgentHandoff(
        from_role=AgentRole.ORCHESTRATOR,
        to_role=AgentRole.CODER,
        payload={
            "task": "fix bug",
            "step": {"step_id": 1, "description": "modify file"},
            "file_path": "app.py",
            "file_content": "x = 1",
        },
        context_chunks=[],
        instruction="Implement this step",
    )

    msg = await agent.execute(handoff)

    assert msg.success is False
    assert msg.structured_data is not None
    assert msg.structured_data.get("error") == "need_more_context"


# ---------------------------------------------------------------------------
# test_verifier_agent_passes_on_clean_output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifier_agent_passes_on_clean_output(_agent_deps) -> None:
    """VerifierAgent should return success=True when verification passes."""
    ollama, asm, bm, cfg = _agent_deps

    verdict = {
        "passed": True,
        "error_summary": "",
        "details": "All tests pass.",
        "recommendation": "proceed",
    }
    ollama.chat = AsyncMock(return_value=json.dumps(verdict))

    agent = VerifierAgent(ollama, asm, bm, cfg)
    handoff = AgentHandoff(
        from_role=AgentRole.ORCHESTRATOR,
        to_role=AgentRole.VERIFIER,
        payload={
            "task": "fix bug",
            "step": {"step_id": 1, "description": "modify file"},
            "verification_output": "All tests passed.",
            "errors_parsed": [],
        },
        context_chunks=[],
        instruction="Interpret verification results",
    )

    msg = await agent.execute(handoff)

    assert msg.success is True
    assert msg.structured_data is not None
    assert msg.structured_data.get("passed") is True


# ---------------------------------------------------------------------------
# test_reflector_agent_recommends_skip_after_many_failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflector_agent_recommends_skip_after_many_failures(_agent_deps) -> None:
    """ReflectorAgent should set should_skip=True when recommending to skip."""
    ollama, asm, bm, cfg = _agent_deps

    reflection = {
        "root_cause": "Fundamental API mismatch.",
        "should_skip": True,
        "skip_reason": "Step requires external dependency changes.",
        "specific_instructions": "",
        "alternative_approach": "Skip this step and handle it manually.",
    }
    ollama.chat = AsyncMock(return_value=json.dumps(reflection))

    agent = ReflectorAgent(ollama, asm, bm, cfg)
    handoff = AgentHandoff(
        from_role=AgentRole.ORCHESTRATOR,
        to_role=AgentRole.REFLECTOR,
        payload={
            "task": "fix bug",
            "step": {"step_id": 1, "description": "modify file"},
            "attempts": [
                {"patch": "x", "error": "type error"},
                {"patch": "y", "error": "type error again"},
                {"patch": "z", "error": "still broken"},
            ],
            "errors": ["type error", "type error again", "still broken"],
        },
        context_chunks=[],
        instruction="Analyze failure and provide revised approach",
    )

    msg = await agent.execute(handoff)

    assert msg.structured_data is not None
    assert msg.structured_data["should_skip"] is True
