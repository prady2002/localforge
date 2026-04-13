"""Cloud-powered multi-agent orchestrator.

Uses ``CloudClient`` instead of ``OllamaClient`` for all LLM calls.
The powerful cloud model means fewer retries, larger patches, and more
sophisticated reasoning at every stage.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Protocol

from localforge.agent.display import OrchestratorDisplay
from localforge.cloud.client import CloudClient
from localforge.cloud.prompts import (
    CLOUD_ANALYZER_PROMPT,
    CLOUD_CODER_PROMPT,
    CLOUD_PLANNER_PROMPT,
    CLOUD_REFLECTOR_PROMPT,
    CLOUD_SUMMARIZER_PROMPT,
    CLOUD_VERIFIER_PROMPT,
)
from localforge.core.config import LocalForgeConfig
from localforge.core.models import (
    AgentHandoff,
    AgentMessage,
    AgentPlan,
    AgentRole,
    FileChunk,
    MultiAgentState,
    OperationType,
    PatchOperation,
    PlanStep,
    RetrievalResult,
    StepStatus,
    VerificationResult,
)
from localforge.core.prompt_templates import (
    ANALYZER_SCHEMA,
    CODER_SCHEMA,
    PLANNER_SCHEMA,
    REFLECTOR_SCHEMA,
    SUMMARIZER_SCHEMA,
    VERIFIER_SCHEMA,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols for decoupled dependencies
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
# CloudAgentOrchestrator
# ---------------------------------------------------------------------------


class CloudAgentOrchestrator:
    """Coordinates the analysis → plan → code → verify → reflect pipeline
    using the cloud API for all LLM reasoning.

    Key differences from the local ``AgentOrchestrator``:
    - Fewer retries needed (cloud model is far more capable)
    - Can handle much larger file patches (full file rewrites)
    - Richer / more detailed agent prompts
    - Parallel step execution when steps have no dependencies
    """

    _MAX_STEP_RETRIES = 3  # fewer than local (5) — model is better

    def __init__(
        self,
        config: LocalForgeConfig,
        client: CloudClient,
        retriever: RetrieverLike,
        patcher: PatcherLike,
        verifier_runner: VerifierRunnerLike,
    ) -> None:
        self.config = config
        self.client = client
        self.retriever = retriever
        self.patcher = patcher
        self.verifier_runner = verifier_runner

        self.state: MultiAgentState | None = None
        self.display = OrchestratorDisplay()
        self._patches_applied: list[PatchOperation] = []
        self._verification_results: list[VerificationResult] = []

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    async def run(self, task: str) -> MultiAgentState:
        self.state = MultiAgentState(
            task=task, iteration=0, current_agent=AgentRole.ORCHESTRATOR,
        )
        self._patches_applied = []
        self._verification_results = []

        try:
            # Phase 1: Analysis
            self.display.phase("ANALYSIS", "Analyzing task and codebase…")
            ctx = self.retriever.retrieve(task, limit=25)
            analysis = await self._call_agent(
                CLOUD_ANALYZER_PROMPT,
                json.dumps(ANALYZER_SCHEMA),
                {"task": task, "context": self._format_chunks(ctx.chunks)},
                "analyzer",
            )

            # Phase 2: Planning
            self.display.phase("PLANNING", "Creating execution plan…")
            plan_data = await self._call_agent(
                CLOUD_PLANNER_PROMPT,
                json.dumps(PLANNER_SCHEMA),
                {"task": task, "analysis": json.dumps(analysis)},
                "planner",
            )
            plan = self._build_plan(plan_data)
            self.display.show_plan(plan)

            # Phase 3: Execute each step
            for step in plan.steps:
                self.state.iteration += 1
                step_ok = await self._execute_step(task, step, plan_data)
                if not step_ok:
                    self.display.warning(
                        f"Step {step.step_id} failed — continuing…"
                    )

            # Phase 4: Final verification
            self.display.phase("FINAL VERIFICATION", "Running full verification…")
            results = self.verifier_runner.run_verification()
            self._verification_results.extend(results)

            # Phase 5: Summary
            self.display.phase("SUMMARY", "Generating summary…")
            summary = await self._call_agent(
                CLOUD_SUMMARIZER_PROMPT,
                json.dumps(SUMMARIZER_SCHEMA),
                {
                    "task": task,
                    "patches_applied": len(self._patches_applied),
                    "verification_passed": all(
                        v.passed for v in self._verification_results
                    ),
                },
                "summarizer",
            )
            self.state.final_summary = summary.get("summary", "")
            self.display.show_summary(self.state)

        except KeyboardInterrupt:
            self.display.warning("Interrupted. Saving state…")
        except Exception as exc:
            logger.error("CloudOrchestrator error: %s", exc, exc_info=True)
            self.display.error(str(exc))

        return self.state

    # ------------------------------------------------------------------
    # Generic agent caller
    # ------------------------------------------------------------------

    async def _call_agent(
        self,
        system_prompt: str,
        schema: str,
        payload: dict[str, Any],
        role_label: str,
    ) -> dict[str, Any]:
        messages = [{"role": "user", "content": json.dumps(payload)}]
        raw = await self.client.chat_structured(
            messages, system_prompt, schema, agent_role=role_label,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.error("Agent %s returned invalid JSON: %s", role_label, raw[:300])
            return {"error": "Invalid JSON", "raw": raw[:500]}

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------

    async def _execute_step(
        self,
        task: str,
        step: PlanStep,
        plan_data: dict[str, Any],
    ) -> bool:
        attempts: list[dict[str, Any]] = []

        for attempt in range(self._MAX_STEP_RETRIES):
            self.display.step(step, attempt + 1)

            # Read the file(s) involved
            file_content = self._read_file(
                step.files_involved[0] if step.files_involved else "",
            )

            previous_error = attempts[-1]["error"] if attempts else None

            # Coder
            coder_result = await self._call_agent(
                CLOUD_CODER_PROMPT,
                json.dumps(CODER_SCHEMA),
                {
                    "task": task,
                    "step": step.model_dump(),
                    "file_content": file_content,
                    "previous_error": previous_error,
                },
                "coder",
            )

            patch_op = self._parse_patch(coder_result, step)
            if not patch_op:
                attempts.append({"error": "Could not parse patch"})
                continue

            self.patcher.show_diff(patch_op)
            if not self.config.auto_approve and not self.display.confirm_patch(patch_op):
                return False

            applied = self.patcher.apply_patch(patch_op)
            if not applied:
                attempts.append({"error": "Patch application failed"})
                continue
            self._patches_applied.append(patch_op)

            # Verify
            results = self.verifier_runner.run_verification(
                changed_files=step.files_involved,
            )
            summary = self.verifier_runner.summarize_results(results)
            self._verification_results.extend(results)

            # Interpret
            verdict = await self._call_agent(
                CLOUD_VERIFIER_PROMPT,
                json.dumps(VERIFIER_SCHEMA),
                {"verification_output": summary.get("summary", "")},
                "verifier",
            )

            if verdict.get("passed"):
                self.display.step_success(step)
                return True

            error = verdict.get("error_summary", "verification failed")
            attempts.append({"error": error})

            # Reflect
            if attempt < self._MAX_STEP_RETRIES - 1:
                self.display.phase("REFLECTION", f"Failed: {error}. Reflecting…")
                await self._call_agent(
                    CLOUD_REFLECTOR_PROMPT,
                    json.dumps(REFLECTOR_SCHEMA),
                    {"attempts": attempts, "step": step.model_dump()},
                    "reflector",
                )

        step.status = StepStatus.FAILED
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_chunks(chunks: list[FileChunk]) -> str:
        parts = []
        for c in chunks:
            parts.append(f"--- {c.file_path} (L{c.start_line}-{c.end_line}) ---\n{c.content}")
        return "\n\n".join(parts)

    def _read_file(self, rel_path: str) -> str:
        if not rel_path:
            return ""
        full = Path(self.config.repo_path) / rel_path
        if not full.is_file():
            return ""
        try:
            return full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _build_plan(data: dict[str, Any]) -> AgentPlan:
        steps = []
        for s in data.get("steps", []):
            steps.append(PlanStep(
                step_id=s.get("step_id", 0),
                description=s.get("description", ""),
                files_involved=s.get("files_involved", []),
                operation=OperationType(s.get("operation", "MODIFY").upper()),
            ))
        return AgentPlan(
            task=data.get("task", ""),
            steps=steps,
            reasoning=data.get("reasoning", ""),
            estimated_complexity=data.get("estimated_complexity", "medium"),
        )

    @staticmethod
    def _parse_patch(
        coder_data: dict[str, Any],
        step: PlanStep,
    ) -> PatchOperation | None:
        if "error" in coder_data:
            return None
        file_path = step.files_involved[0] if step.files_involved else ""
        op_type = step.operation
        return PatchOperation(
            file_path=file_path,
            operation_type=op_type,
            new_content=coder_data.get("full_content") or coder_data.get("replace_block", ""),
            diff=coder_data.get("diff", ""),
            description=step.description,
        )
