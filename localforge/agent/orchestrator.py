"""Multi-agent orchestrator — coordinates the full analysis→patch→verify pipeline."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

from localforge.agent.agents import (
    AnalyzerAgent,
    CoderAgent,
    PlannerAgent,
    ReflectorAgent,
    SummarizerAgent,
    VerifierAgent,
)
from localforge.agent.display import OrchestratorDisplay
from localforge.agent.state_manager import StateManager
from localforge.context_manager.assembler import ContextAssembler
from localforge.context_manager.budget import TokenBudgetManager
from localforge.core.config import LocalForgeConfig
from localforge.core.models import (
    AgentHandoff,
    AgentPlan,
    AgentRole,
    FileChunk,
    MultiAgentState,
    PatchOperation,
    PlanStep,
    RetrievalResult,
    VerificationResult,
)
from localforge.core.ollama_client import OllamaClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lightweight protocols for patcher / verifier_runner so the orchestrator
# stays testable without concrete implementations that may not exist yet.
# ---------------------------------------------------------------------------


class PatcherLike(Protocol):
    def show_diff(self, patch: PatchOperation) -> None: ...
    def apply_patch(self, patch: PatchOperation) -> bool: ...


class VerifierRunnerLike(Protocol):
    def run_verification(
        self, changed_files: list[str] | None = None,
    ) -> list[VerificationResult]: ...
    def summarize_results(self, results: list[VerificationResult]) -> dict[str, Any]: ...


class RetrieverLike(Protocol):
    def retrieve(self, task: str, limit: int = 15) -> RetrievalResult: ...


# ---------------------------------------------------------------------------
# AgentOrchestrator
# ---------------------------------------------------------------------------


class AgentOrchestrator:
    """Coordinates all specialist agents through the full task pipeline.

    Pipeline phases:
        1. Analysis   – understand the task
        2. Planning   – create an execution plan
        3. Execution  – implement & verify each step (with retry/reflection)
        4. Final verification
        5. Summary
    """

    _MAX_STEP_RETRIES = 5

    def __init__(
        self,
        config: LocalForgeConfig,
        ollama: OllamaClient,
        retriever: RetrieverLike,
        assembler: ContextAssembler,
        budget_manager: TokenBudgetManager,
        patcher: PatcherLike,
        verifier_runner: VerifierRunnerLike,
    ) -> None:
        self.config = config
        self.retriever = retriever
        self.patcher = patcher
        self.verifier_runner = verifier_runner

        # Instantiate the six specialist agents
        self.analyzer = AnalyzerAgent(ollama, assembler, budget_manager, config)
        self.planner = PlannerAgent(ollama, assembler, budget_manager, config)
        self.coder = CoderAgent(ollama, assembler, budget_manager, config)
        self.verifier_agent = VerifierAgent(ollama, assembler, budget_manager, config)
        self.reflector = ReflectorAgent(ollama, assembler, budget_manager, config)
        self.summarizer = SummarizerAgent(ollama, assembler, budget_manager, config)

        self.state: MultiAgentState | None = None
        self.display = OrchestratorDisplay()
        self.logger = logger

        # Accumulated across the run — not on MultiAgentState itself
        self._patches_applied: list[PatchOperation] = []
        self._verification_results: list[VerificationResult] = []

    # ==================================================================
    # Public entry point
    # ==================================================================

    async def run(self, task: str) -> MultiAgentState:
        """Execute the full multi-agent pipeline for *task*."""
        self.state = MultiAgentState(
            task=task,
            iteration=0,
            current_agent=AgentRole.ORCHESTRATOR,
        )
        self._patches_applied = []
        self._verification_results = []

        try:
            # ---- Phase 1: Analysis ----------------------------------
            self.display.phase("ANALYSIS", "Analyzing task and codebase…")
            retrieval_result = self._retrieve_initial_context(task)
            analysis = await self._run_analyzer(task, retrieval_result.chunks)

            if analysis.get("needs_more_context"):
                extra = self._retrieve_additional(
                    analysis.get("additional_context_queries", []),
                )
                retrieval_result.chunks.extend(extra)
                analysis = await self._run_analyzer(task, retrieval_result.chunks)

            # ---- Phase 2: Planning ----------------------------------
            self.display.phase("PLANNING", "Creating execution plan…")
            plan_result = await self._run_planner(
                task, analysis, retrieval_result.chunks,
            )
            plan = self._build_plan(plan_result)
            self.state.agent_states["plan"] = plan_result
            self.display.show_plan(plan)

            # ---- Phase 3: Execute each step -------------------------
            for step in plan.steps:
                self.state.iteration += 1
                step_ok = await self._execute_step_with_retry(
                    task, step, plan_result,
                )
                if not step_ok:
                    self.display.warning(
                        f"Step {step.step_id} could not be completed — continuing…"
                    )

            # ---- Phase 4: Final verification ------------------------
            self.display.phase("FINAL VERIFICATION", "Running full verification suite…")
            final_results = self.verifier_runner.run_verification()
            self._verification_results.extend(final_results)

            # ---- Phase 5: Summary -----------------------------------
            self.display.phase("SUMMARY", "Generating summary…")
            await self._run_summarizer(task)
            self.display.show_summary(self.state)

        except KeyboardInterrupt:
            self.display.warning("Interrupted by user. Saving state…")
            self._save_state()
        except Exception as exc:
            self.logger.error("Orchestrator error: %s", exc, exc_info=True)
            self.display.error(str(exc))

        return self.state

    # ==================================================================
    # Retrieval helpers
    # ==================================================================

    def _retrieve_initial_context(self, task: str) -> RetrievalResult:
        return self.retriever.retrieve(task, limit=15)

    def _retrieve_additional(self, queries: list[str]) -> list[FileChunk]:
        chunks: list[FileChunk] = []
        for q in queries[:3]:
            result = self.retriever.retrieve(q, limit=5)
            chunks.extend(result.chunks)
        return chunks

    # ==================================================================
    # Agent runners (each builds a handoff, calls execute, records msg)
    # ==================================================================

    async def _run_analyzer(
        self, task: str, chunks: list[FileChunk],
    ) -> dict[str, Any]:
        assert self.state is not None
        handoff = AgentHandoff(
            from_role=AgentRole.ORCHESTRATOR,
            to_role=AgentRole.ANALYZER,
            payload={"task": task, "repo_path": self.config.repo_path},
            context_chunks=chunks,
            instruction="Analyze this task",
        )
        msg = await self.analyzer.execute(handoff)
        self.state.messages.append(msg)
        self.state.handoffs.append(handoff)
        return msg.structured_data or {}

    async def _run_planner(
        self, task: str, analysis: dict[str, Any], chunks: list[FileChunk],
    ) -> dict[str, Any]:
        assert self.state is not None
        handoff = AgentHandoff(
            from_role=AgentRole.ORCHESTRATOR,
            to_role=AgentRole.PLANNER,
            payload={"task": task, "analysis": analysis},
            context_chunks=chunks,
            instruction="Create execution plan",
        )
        msg = await self.planner.execute(handoff)
        self.state.messages.append(msg)
        self.state.handoffs.append(handoff)
        return msg.structured_data or {}

    async def _run_summarizer(self, task: str) -> dict[str, Any]:
        assert self.state is not None
        handoff = AgentHandoff(
            from_role=AgentRole.ORCHESTRATOR,
            to_role=AgentRole.SUMMARIZER,
            payload={
                "task": task,
                "patches": [p.model_dump() for p in self._patches_applied],
                "verification_results": [
                    v.model_dump() for v in self._verification_results
                ],
                "iterations": self.state.iteration,
            },
            context_chunks=[],
            instruction="Summarize all work done",
        )
        msg = await self.summarizer.execute(handoff)
        self.state.messages.append(msg)
        self.state.handoffs.append(handoff)
        return msg.structured_data or {}

    # ==================================================================
    # Step execution with retry + reflection
    # ==================================================================

    async def _execute_step_with_retry(
        self,
        task: str,
        step: PlanStep,
        plan_data: dict[str, Any],
    ) -> bool:
        """Run one plan step.  Retry with reflection up to ``_MAX_STEP_RETRIES`` times."""
        assert self.state is not None
        attempts: list[dict[str, Any]] = []

        for attempt_num in range(self._MAX_STEP_RETRIES):
            self.display.step(step, attempt_num + 1)

            # Gather file context
            file_chunks = self._retrieve_additional(step.files_involved)
            file_content = self._get_primary_file_content(
                step.files_involved, self.config.repo_path,
            )
            previous_error = attempts[-1]["error"] if attempts else None

            # ---- Coder -------------------------------------------
            coder_handoff = AgentHandoff(
                from_role=AgentRole.ORCHESTRATOR,
                to_role=AgentRole.CODER,
                payload={
                    "task": task,
                    "step": step.model_dump(),
                    "file_path": step.files_involved[0] if step.files_involved else "",
                    "file_content": file_content,
                    "previous_error": previous_error,
                },
                context_chunks=file_chunks,
                instruction="Implement this step",
            )
            coder_msg = await self.coder.execute(coder_handoff)
            self.state.messages.append(coder_msg)
            self.state.handoffs.append(coder_handoff)

            if not coder_msg.success:
                attempts.append({
                    "patch": None,
                    "error": coder_msg.structured_data.get("reason", "coder reported failure")
                    if coder_msg.structured_data else "coder reported failure",
                })
                continue

            patch_op = self._parse_patch_operation(coder_msg.structured_data or {})

            # ---- Show diff / approval ----------------------------
            self.patcher.show_diff(patch_op)
            if not self.config.auto_approve and not self.display.confirm_patch(patch_op):
                    return False

            # ---- Apply patch -------------------------------------
            applied = self.patcher.apply_patch(patch_op)
            if not applied:
                attempts.append({
                    "patch": patch_op.model_dump(),
                    "error": "patch application failed",
                })
                continue
            self._patches_applied.append(patch_op)

            # ---- Verify ------------------------------------------
            verify_results = self.verifier_runner.run_verification(
                changed_files=step.files_involved,
            )
            verify_summary = self.verifier_runner.summarize_results(verify_results)
            self._verification_results.extend(verify_results)

            verifier_handoff = AgentHandoff(
                from_role=AgentRole.ORCHESTRATOR,
                to_role=AgentRole.VERIFIER,
                payload={
                    "task": task,
                    "step": step.model_dump(),
                    "verification_output": verify_summary.get("summary", ""),
                    "errors_parsed": verify_summary.get("errors", []),
                },
                context_chunks=[],
                instruction="Interpret verification results",
            )
            verifier_msg = await self.verifier_agent.execute(verifier_handoff)
            self.state.messages.append(verifier_msg)
            self.state.handoffs.append(verifier_handoff)

            verdict = verifier_msg.structured_data or {}

            if verdict.get("passed"):
                self.display.step_success(step)
                return True

            # ---- Failed — reflect --------------------------------
            error_summary = verdict.get("error_summary", "unknown error")
            attempts.append({"patch": patch_op.model_dump(), "error": error_summary})

            if attempt_num < self._MAX_STEP_RETRIES - 1:
                self.display.phase(
                    "REFLECTION",
                    f"Step failed: {error_summary}. Reflecting…",
                )
                reflector_handoff = AgentHandoff(
                    from_role=AgentRole.ORCHESTRATOR,
                    to_role=AgentRole.REFLECTOR,
                    payload={
                        "task": task,
                        "step": step.model_dump(),
                        "attempts": attempts,
                        "errors": [a["error"] for a in attempts],
                    },
                    context_chunks=file_chunks,
                    instruction="Analyze failure and provide revised approach",
                )
                reflector_msg = await self.reflector.execute(reflector_handoff)
                self.state.messages.append(reflector_msg)
                self.state.handoffs.append(reflector_handoff)

                reflection = reflector_msg.structured_data or {}
                if reflection.get("should_skip"):
                    self.display.warning(
                        f"Reflector recommends skipping: {reflection.get('skip_reason', 'N/A')}"
                    )
                    return False

                # Feed refined instructions as previous_error for next coder attempt
                attempts[-1]["error"] = reflection.get(
                    "specific_instructions", error_summary,
                )

        self.display.step_failed(step)
        return False

    # ==================================================================
    # Helpers
    # ==================================================================

    @staticmethod
    def _get_primary_file_content(files: list[str], repo_path: str = ".") -> str:
        if not files:
            return ""
        try:
            path = Path(repo_path) / files[0]
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    @staticmethod
    def _parse_patch_operation(data: dict[str, Any]) -> PatchOperation:
        return PatchOperation(
            file_path=data.get("file_path", ""),
            operation_type=data.get("operation", "MODIFY"),
            original_content=data.get("search_block", ""),
            new_content=data.get("replace_block") or data.get("full_content", ""),
            diff="",
            description=data.get("description", ""),
        )

    @staticmethod
    def _build_plan(plan_data: dict[str, Any]) -> AgentPlan:
        """Build an :class:`AgentPlan` from the planner's JSON output."""
        steps = []
        for s in plan_data.get("steps", []):
            steps.append(
                PlanStep(
                    step_id=s.get("step_id", 0),
                    description=s.get("description", ""),
                    files_involved=s.get("files_involved", []),
                    operation=s.get("operation", "MODIFY"),
                )
            )
        return AgentPlan(
            task=plan_data.get("task", ""),
            steps=steps,
            reasoning=plan_data.get("reasoning", ""),
            estimated_complexity=plan_data.get("estimated_complexity", "medium"),
        )

    def _save_state(self) -> None:
        if self.state is None:
            return
        mgr = StateManager()
        mgr.save_state(self.state, mgr.get_state_path(self.state.task))
