"""Specialized agent implementations for the localforge multi-agent system."""

from __future__ import annotations

import os
from pathlib import Path

from localforge.agent.base import BaseAgent
from localforge.context_manager.assembler import ContextAssembler
from localforge.context_manager.budget import TokenBudgetManager
from localforge.core.config import LocalForgeConfig
from localforge.core.models import AgentHandoff, AgentMessage, AgentRole
from localforge.core.ollama_client import OllamaClient
from localforge.core.prompt_templates import (
    ANALYZER_SCHEMA,
    CODER_SCHEMA,
    PLANNER_SCHEMA,
    REFLECTOR_SCHEMA,
    SUMMARIZER_SCHEMA,
    SYSTEM_ANALYZER,
    SYSTEM_CODER,
    SYSTEM_PLANNER,
    SYSTEM_REFLECTOR,
    SYSTEM_SUMMARIZER,
    SYSTEM_VERIFIER,
    VERIFIER_SCHEMA,
    analyzer_prompt,
    coder_prompt,
    planner_prompt,
    reflector_prompt,
    summarizer_prompt,
    verifier_prompt,
)

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".tox", ".mypy_cache", ".localforge"}

_MAX_TREE_LINES = 50


# ---------------------------------------------------------------------------
# 1. AnalyzerAgent
# ---------------------------------------------------------------------------


class AnalyzerAgent(BaseAgent):
    """Reads the task and retrieved code, outputs a structured analysis."""

    def __init__(
        self,
        ollama: OllamaClient,
        assembler: ContextAssembler,
        budget_manager: TokenBudgetManager,
        config: LocalForgeConfig,
    ) -> None:
        super().__init__(AgentRole.ANALYZER, ollama, assembler, budget_manager, config)
        self.system_prompt = SYSTEM_ANALYZER

    async def execute(self, handoff: AgentHandoff) -> AgentMessage:
        context_str = self._format_context(handoff.context_chunks)
        repo_structure = self._get_repo_structure(
            handoff.payload.get("repo_path", "."),
        )
        prompt = analyzer_prompt(
            handoff.payload["task"], context_str, repo_structure,
        )
        result = await self._call_llm(prompt, ANALYZER_SCHEMA)
        return self._record_message(str(result), result, success=bool(result), tokens=self._last_tokens_used)

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _get_repo_structure(repo_path: str) -> str:
        """Return a directory-tree string (max ``_MAX_TREE_LINES`` lines)."""
        root = Path(repo_path)
        lines: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            # prune ignored directories in-place
            dirnames[:] = [
                d for d in sorted(dirnames) if d not in _SKIP_DIRS
            ]
            depth = Path(dirpath).relative_to(root).parts
            indent = "  " * len(depth)
            lines.append(f"{indent}{Path(dirpath).name}/")
            for fname in sorted(filenames):
                lines.append(f"{indent}  {fname}")
            if len(lines) >= _MAX_TREE_LINES:
                lines.append("  ... (truncated)")
                break
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. PlannerAgent
# ---------------------------------------------------------------------------


class PlannerAgent(BaseAgent):
    """Creates an ordered execution plan from an analysis."""

    def __init__(
        self,
        ollama: OllamaClient,
        assembler: ContextAssembler,
        budget_manager: TokenBudgetManager,
        config: LocalForgeConfig,
    ) -> None:
        super().__init__(AgentRole.PLANNER, ollama, assembler, budget_manager, config)
        self.system_prompt = SYSTEM_PLANNER

    async def execute(self, handoff: AgentHandoff) -> AgentMessage:
        analysis = handoff.payload["analysis"]
        task = handoff.payload["task"]
        context_str = self._format_context(handoff.context_chunks)
        prompt = planner_prompt(task, analysis, context_str)

        result = await self._call_llm(prompt, PLANNER_SCHEMA)

        # If the model produced an unreasonably long plan, ask it to consolidate.
        if len(result.get("steps", [])) > 20:
            consolidation_prompt = (
                f"Your plan has {len(result['steps'])} steps which is too many. "
                "Consolidate to a maximum of 15 steps by combining related changes. "
                "Return the same JSON schema."
            )
            result = await self._call_llm(consolidation_prompt, PLANNER_SCHEMA)

        return self._record_message(str(result), result, success=bool(result), tokens=self._last_tokens_used)


# ---------------------------------------------------------------------------
# 3. CoderAgent
# ---------------------------------------------------------------------------


class CoderAgent(BaseAgent):
    """Implements a single plan step by writing a code patch."""

    def __init__(
        self,
        ollama: OllamaClient,
        assembler: ContextAssembler,
        budget_manager: TokenBudgetManager,
        config: LocalForgeConfig,
    ) -> None:
        super().__init__(AgentRole.CODER, ollama, assembler, budget_manager, config)
        self.system_prompt = SYSTEM_CODER

    async def execute(self, handoff: AgentHandoff) -> AgentMessage:
        task = handoff.payload["task"]
        step = handoff.payload["step"]
        file_path = handoff.payload.get("file_path", "")
        file_content = handoff.payload.get("file_content", "")
        previous_error = handoff.payload.get("previous_error", None)
        context_str = self._format_context(handoff.context_chunks)

        prompt = coder_prompt(
            task, step, file_content, file_path, context_str, previous_error,
        )
        result = await self._call_llm(prompt, CODER_SCHEMA)

        if result.get("error") == "need_more_context":
            return self._record_message(str(result), result, success=False, tokens=self._last_tokens_used)

        return self._record_message(str(result), result, success=bool(result), tokens=self._last_tokens_used)


# ---------------------------------------------------------------------------
# 4. VerifierAgent
# ---------------------------------------------------------------------------


class VerifierAgent(BaseAgent):
    """Interprets verification output and decides what to do next."""

    def __init__(
        self,
        ollama: OllamaClient,
        assembler: ContextAssembler,
        budget_manager: TokenBudgetManager,
        config: LocalForgeConfig,
    ) -> None:
        super().__init__(AgentRole.VERIFIER, ollama, assembler, budget_manager, config)
        self.system_prompt = SYSTEM_VERIFIER

    async def execute(self, handoff: AgentHandoff) -> AgentMessage:
        task = handoff.payload["task"]
        step = handoff.payload["step"]
        verification_output = handoff.payload["verification_output"]
        errors_parsed = handoff.payload.get("errors_parsed", [])

        prompt = verifier_prompt(task, step, verification_output, errors_parsed)
        result = await self._call_llm(prompt, VERIFIER_SCHEMA)

        return self._record_message(
            str(result), result, success=result.get("passed", False), tokens=self._last_tokens_used,
        )


# ---------------------------------------------------------------------------
# 5. ReflectorAgent
# ---------------------------------------------------------------------------


class ReflectorAgent(BaseAgent):
    """Analyses patch failures and suggests a revised approach."""

    def __init__(
        self,
        ollama: OllamaClient,
        assembler: ContextAssembler,
        budget_manager: TokenBudgetManager,
        config: LocalForgeConfig,
    ) -> None:
        super().__init__(AgentRole.REFLECTOR, ollama, assembler, budget_manager, config)
        self.system_prompt = SYSTEM_REFLECTOR

    async def execute(self, handoff: AgentHandoff) -> AgentMessage:
        task = handoff.payload["task"]
        step = handoff.payload["step"]
        attempts = handoff.payload["attempts"]
        errors = handoff.payload["errors"]

        prompt = reflector_prompt(task, step, attempts, errors)
        result = await self._call_llm(prompt, REFLECTOR_SCHEMA)

        return self._record_message(str(result), result, success=bool(result), tokens=self._last_tokens_used)


# ---------------------------------------------------------------------------
# 6. SummarizerAgent
# ---------------------------------------------------------------------------


class SummarizerAgent(BaseAgent):
    """Produces a human-readable summary of all agent actions and patches."""

    def __init__(
        self,
        ollama: OllamaClient,
        assembler: ContextAssembler,
        budget_manager: TokenBudgetManager,
        config: LocalForgeConfig,
    ) -> None:
        super().__init__(AgentRole.SUMMARIZER, ollama, assembler, budget_manager, config)
        self.system_prompt = SYSTEM_SUMMARIZER

    async def execute(self, handoff: AgentHandoff) -> AgentMessage:
        task = handoff.payload["task"]
        patches = handoff.payload["patches"]
        verification_results = handoff.payload["verification_results"]
        iterations = handoff.payload["iterations"]

        prompt = summarizer_prompt(task, patches, verification_results, iterations)
        result = await self._call_llm(prompt, SUMMARIZER_SCHEMA)

        return self._record_message(str(result), result, success=bool(result), tokens=self._last_tokens_used)
