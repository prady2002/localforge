"""End-to-end test: mock Ollama, run the full orchestrator pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from localforge.agent.orchestrator import AgentOrchestrator
from localforge.context_manager.assembler import ContextAssembler
from localforge.context_manager.budget import TokenBudgetManager
from localforge.core.config import LocalForgeConfig
from localforge.core.models import (
    AgentRole,
    FileChunk,
    MultiAgentState,
    RetrievalResult,
    VerificationResult,
)
from localforge.core.ollama_client import OllamaClient


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

_ANALYZER_RESP = json.dumps({
    "understanding": "The calculator add function subtracts instead of adding.",
    "key_files": ["calculator.py"],
    "complexity": "simple",
    "approach": "Change '-' to '+' in the add function.",
    "risks": [],
    "needs_more_context": False,
    "additional_context_queries": [],
})

_PLANNER_RESP = json.dumps({
    "reasoning": "Single line change needed.",
    "estimated_complexity": "simple",
    "steps": [
        {
            "step_id": 1,
            "description": "Fix add function operator",
            "files_involved": ["calculator.py"],
            "operation": "MODIFY",
        }
    ],
})

_CODER_RESP = json.dumps({
    "file_path": "calculator.py",
    "operation": "MODIFY",
    "search_block": "return a - b",
    "replace_block": "return a + b",
    "description": "Fix subtraction to addition",
})

_VERIFIER_RESP = json.dumps({
    "passed": True,
    "error_summary": "",
    "details": "All checks passed.",
    "recommendation": "proceed",
})

_SUMMARIZER_RESP = json.dumps({
    "summary": "Fixed the add function in calculator.py.",
    "files_changed": ["calculator.py"],
    "tests_status": "all passing",
    "remaining_issues": [],
})


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_fix_calculator_bug(tmp_path: Path) -> None:
    """Full pipeline: temp repo → mock LLM → run orchestrator → assert outcomes."""

    # --- 1. Create temp repo with a deliberate bug -----------------------
    calc = tmp_path / "calculator.py"
    calc.write_text(
        "def add(a, b):\n    return a - b  # BUG: should be +\n",
        encoding="utf-8",
    )

    # --- 2. Mock OllamaClient --------------------------------------------
    cfg = LocalForgeConfig(
        repo_path=str(tmp_path),
        auto_approve=True,
        max_iterations=10,
    )
    ollama = OllamaClient(cfg)

    call_sequence = iter([
        _ANALYZER_RESP,
        _PLANNER_RESP,
        _CODER_RESP,
        _VERIFIER_RESP,
        _SUMMARIZER_RESP,
    ])

    async def fake_chat(messages, system=None, temperature=0.1, stream=True, agent_role="agent"):
        return next(call_sequence)

    ollama.chat = AsyncMock(side_effect=fake_chat)

    bm = TokenBudgetManager(cfg)
    asm = ContextAssembler(bm, cfg)

    # --- 3. Mock retriever, patcher, verifier_runner ----------------------
    fake_chunk = FileChunk(
        file_path="calculator.py",
        start_line=1, end_line=2,
        content="def add(a, b):\n    return a - b\n",
        score=1.0,
    )
    retriever = MagicMock()
    retriever.retrieve.return_value = RetrievalResult(
        query="fix the bug", chunks=[fake_chunk], total_found=1,
    )

    patcher = MagicMock()
    patcher.apply_patch.return_value = True

    verifier_runner = MagicMock()
    verifier_runner.run_verification.return_value = [
        VerificationResult(
            success=True, command="python -m py_compile calculator.py",
            stdout="", stderr="", exit_code=0,
        ),
    ]
    verifier_runner.summarize_results.return_value = {
        "all_passed": True,
        "failed_commands": [],
        "total_errors": 0,
        "total_warnings": 0,
        "summary": "All verification checks passed.",
        "errors": [],
    }

    # --- 4. Build and run orchestrator ------------------------------------
    orch = AgentOrchestrator(
        config=cfg,
        ollama=ollama,
        retriever=retriever,
        assembler=asm,
        budget_manager=bm,
        patcher=patcher,
        verifier_runner=verifier_runner,
    )

    # Suppress Rich display output during tests
    orch.display.phase = MagicMock()
    orch.display.step = MagicMock()
    orch.display.step_success = MagicMock()
    orch.display.step_failed = MagicMock()
    orch.display.warning = MagicMock()
    orch.display.error = MagicMock()
    orch.display.show_plan = MagicMock()
    orch.display.show_summary = MagicMock()
    orch.display.confirm_patch = MagicMock(return_value=True)

    state = await orch.run("fix the bug in calculator.py")

    # --- 5. Assertions ----------------------------------------------------
    assert isinstance(state, MultiAgentState)

    # Plan was generated (planner agent was called)
    assert state.messages, "No messages recorded"
    agent_roles_seen = {m.role for m in state.messages}
    assert AgentRole.ANALYZER in agent_roles_seen
    assert AgentRole.PLANNER in agent_roles_seen
    assert AgentRole.CODER in agent_roles_seen

    # Patch was applied
    patcher.apply_patch.assert_called()

    # Verification ran
    verifier_runner.run_verification.assert_called()
