"""Tests for localforge.agent.orchestrator — AgentOrchestrator pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from localforge.agent.orchestrator import AgentOrchestrator
from localforge.agent.state_manager import StateManager
from localforge.context_manager.assembler import ContextAssembler
from localforge.context_manager.budget import TokenBudgetManager
from localforge.core.config import LocalForgeConfig
from localforge.core.models import (
    AgentHandoff,
    AgentMessage,
    AgentRole,
    FileChunk,
    MultiAgentState,
    RetrievalResult,
    VerificationResult,
)
from localforge.core.ollama_client import OllamaClient


# ---------------------------------------------------------------------------
# Reusable structured responses
# ---------------------------------------------------------------------------

_ANALYSIS = {
    "understanding": "The file has a bug.",
    "key_files": ["bug.py"],
    "complexity": "simple",
    "approach": "Fix the operator.",
    "risks": [],
    "needs_more_context": False,
    "additional_context_queries": [],
}

_PLAN = {
    "reasoning": "One step fix.",
    "estimated_complexity": "simple",
    "steps": [
        {
            "step_id": 1,
            "description": "Fix operator",
            "files_involved": ["bug.py"],
            "operation": "MODIFY",
        },
    ],
}

_CODER = {
    "file_path": "bug.py",
    "operation": "MODIFY",
    "search_block": "a - b",
    "replace_block": "a + b",
    "description": "Fix operator",
}

_VERIFIER_PASS = {
    "passed": True,
    "error_summary": "",
    "details": "OK",
    "recommendation": "proceed",
}

_VERIFIER_FAIL = {
    "passed": False,
    "error_summary": "test_add failed: expected 3 got -1",
    "details": "AssertionError",
    "recommendation": "fix",
}

_REFLECTOR = {
    "root_cause": "Wrong operator.",
    "should_skip": False,
    "skip_reason": "",
    "specific_instructions": "Use + instead of -.",
    "alternative_approach": "",
}

_SUMMARY = {
    "summary": "Fixed the operator bug.",
    "files_changed": ["bug.py"],
    "tests_status": "all passing",
    "remaining_issues": [],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orchestrator(
    tmp_path: Path,
    llm_responses: list[str],
    verifier_results: list[VerificationResult] | None = None,
) -> AgentOrchestrator:
    """Build an orchestrator with mocked dependencies.

    *llm_responses* is a flat list of JSON strings consumed in order by all
    agents (via the shared OllamaClient.chat mock).
    """
    cfg = LocalForgeConfig(
        repo_path=str(tmp_path),
        auto_approve=True,
        max_iterations=10,
    )

    ollama = OllamaClient(cfg)
    response_iter = iter(llm_responses)

    async def fake_chat(messages, system=None, temperature=0.1, stream=True, agent_role="agent"):
        return next(response_iter)

    ollama.chat = AsyncMock(side_effect=fake_chat)

    bm = TokenBudgetManager(cfg)
    asm = ContextAssembler(bm, cfg)

    retriever = MagicMock()
    retriever.retrieve.return_value = RetrievalResult(
        query="task", chunks=[
            FileChunk(file_path="bug.py", start_line=1, end_line=2,
                      content="def add(a,b): return a - b", score=1.0),
        ], total_found=1,
    )

    patcher = MagicMock()
    patcher.apply_patch.return_value = True

    if verifier_results is None:
        verifier_results = [
            VerificationResult(success=True, command="check", exit_code=0),
        ]

    verifier_runner = MagicMock()
    verifier_runner.run_verification.return_value = verifier_results
    verifier_runner.summarize_results.return_value = {
        "all_passed": all(r.success for r in verifier_results),
        "summary": "OK" if all(r.success for r in verifier_results) else "FAIL",
        "errors": [] if all(r.success for r in verifier_results) else ["error"],
        "failed_commands": [],
        "total_errors": 0 if all(r.success for r in verifier_results) else 1,
        "total_warnings": 0,
    }

    orch = AgentOrchestrator(
        config=cfg,
        ollama=ollama,
        retriever=retriever,
        assembler=asm,
        budget_manager=bm,
        patcher=patcher,
        verifier_runner=verifier_runner,
    )

    # Silence Rich display
    for attr in ("phase", "step", "step_success", "step_failed",
                 "warning", "error", "show_plan", "show_summary", "confirm_patch"):
        setattr(orch.display, attr, MagicMock())

    return orch


# ---------------------------------------------------------------------------
# test_full_pipeline_simple_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_pipeline_simple_task(tmp_path: Path) -> None:
    """Happy-path: analyze → plan → code → verify → summarize."""
    (tmp_path / "bug.py").write_text("def add(a,b): return a - b\n", encoding="utf-8")

    responses = [
        json.dumps(_ANALYSIS),   # analyzer
        json.dumps(_PLAN),       # planner
        json.dumps(_CODER),      # coder
        json.dumps(_VERIFIER_PASS),  # verifier
        json.dumps(_SUMMARY),    # summarizer (final verification verifier_agent is skipped if pass)
    ]

    orch = _make_orchestrator(tmp_path, responses)
    state = await orch.run("fix the bug")

    assert isinstance(state, MultiAgentState)
    roles_seen = {m.role for m in state.messages}
    assert AgentRole.ANALYZER in roles_seen
    assert AgentRole.PLANNER in roles_seen
    assert AgentRole.CODER in roles_seen
    assert AgentRole.VERIFIER in roles_seen
    assert AgentRole.SUMMARIZER in roles_seen

    # Summarizer was called → final_summary might be set via state.messages
    summarizer_msgs = [m for m in state.messages if m.role == AgentRole.SUMMARIZER]
    assert len(summarizer_msgs) >= 1


# ---------------------------------------------------------------------------
# test_reflection_triggered_on_failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_triggered_on_failure(tmp_path: Path) -> None:
    """When verification fails, the reflector should be invoked and coder retried."""
    (tmp_path / "bug.py").write_text("def add(a,b): return a - b\n", encoding="utf-8")

    # Sequence: analyzer, planner,
    #   coder(attempt1), verifier(fail), reflector,
    #   coder(attempt2), verifier(pass),
    #   summarizer
    responses = [
        json.dumps(_ANALYSIS),
        json.dumps(_PLAN),
        json.dumps(_CODER),         # attempt 1
        json.dumps(_VERIFIER_FAIL), # fail
        json.dumps(_REFLECTOR),     # reflection
        json.dumps(_CODER),         # attempt 2
        json.dumps(_VERIFIER_PASS), # pass
        json.dumps(_VERIFIER_PASS), # final verification (verifier_agent on final results)
        json.dumps(_SUMMARY),
    ]

    fail_result = VerificationResult(success=False, command="check", exit_code=1,
                                     error_count=1, stdout="FAILED test_add")
    pass_result = VerificationResult(success=True, command="check", exit_code=0)

    orch = _make_orchestrator(tmp_path, responses)

    # Make verifier_runner return fail first time, pass second time and onwards
    call_count = {"n": 0}
    original_run = orch.verifier_runner.run_verification

    def dynamic_verify(changed_files=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [fail_result]
        return [pass_result]

    orch.verifier_runner.run_verification = MagicMock(side_effect=dynamic_verify)

    def dynamic_summarize(results):
        all_pass = all(r.success for r in results)
        return {
            "all_passed": all_pass,
            "summary": "OK" if all_pass else "FAIL",
            "errors": [] if all_pass else ["error"],
            "failed_commands": [],
            "total_errors": 0 if all_pass else 1,
            "total_warnings": 0,
        }

    orch.verifier_runner.summarize_results = MagicMock(side_effect=dynamic_summarize)

    state = await orch.run("fix the bug")

    roles_seen = {m.role for m in state.messages}
    assert AgentRole.REFLECTOR in roles_seen

    # Coder was called at least twice
    coder_msgs = [m for m in state.messages if m.role == AgentRole.CODER]
    assert len(coder_msgs) >= 2

    # Patches applied > 1
    assert len(orch._patches_applied) >= 2


# ---------------------------------------------------------------------------
# test_orchestrator_handles_keyboard_interrupt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_handles_keyboard_interrupt(tmp_path: Path) -> None:
    """KeyboardInterrupt during analysis should save state and not propagate."""
    (tmp_path / "bug.py").write_text("x = 1\n", encoding="utf-8")

    cfg = LocalForgeConfig(repo_path=str(tmp_path), auto_approve=True)
    ollama = OllamaClient(cfg)

    # Analyzer raises KeyboardInterrupt
    ollama.chat = AsyncMock(side_effect=KeyboardInterrupt)

    bm = TokenBudgetManager(cfg)
    asm = ContextAssembler(bm, cfg)

    retriever = MagicMock()
    retriever.retrieve.return_value = RetrievalResult(
        query="task", chunks=[], total_found=0,
    )

    patcher = MagicMock()
    verifier_runner = MagicMock()
    verifier_runner.run_verification.return_value = []
    verifier_runner.summarize_results.return_value = {"summary": "", "errors": []}

    orch = AgentOrchestrator(
        config=cfg, ollama=ollama, retriever=retriever,
        assembler=asm, budget_manager=bm,
        patcher=patcher, verifier_runner=verifier_runner,
    )

    # Silence display
    for attr in ("phase", "step", "step_success", "step_failed",
                 "warning", "error", "show_plan", "show_summary", "confirm_patch"):
        setattr(orch.display, attr, MagicMock())

    # Mock _save_state to check it gets called
    orch._save_state = MagicMock()

    # Should NOT raise — KeyboardInterrupt is caught internally
    state = await orch.run("test task")

    assert isinstance(state, MultiAgentState)
    orch._save_state.assert_called_once()
