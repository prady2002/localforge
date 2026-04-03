"""Prompt assembly – packs retrieved context and task instructions into
token-budgeted prompts suitable for local LLMs via Ollama."""

from __future__ import annotations

from localforge.context_manager.budget import TokenBudgetManager
from localforge.core.config import LocalForgeConfig, ModelProfileSettings
from localforge.core.models import FileChunk

# ---------------------------------------------------------------------------
# Phase instruction templates
# ---------------------------------------------------------------------------

_PHASE_TEMPLATES: dict[str, str] = {
    "analyze": (
        "You are analyzing a codebase to understand a task.\n\n"
        "TASK:\n{task}\n\n"
        "INSTRUCTIONS:\n"
        "1. Read the retrieved code context carefully.\n"
        "2. Identify every file, function, and data structure relevant to the task.\n"
        "3. Note any dependencies, imports, or side-effects that could be affected.\n"
        "4. List any ambiguities or missing information.\n"
        "5. Produce a concise analysis summarising your findings.\n\n"
        "CONTEXT:\n{context}\n\n"
        "{extra}"
    ),
    "plan": (
        "You are creating an implementation plan for the following task.\n\n"
        "TASK:\n{task}\n\n"
        "INSTRUCTIONS:\n"
        "1. Break the task into small, ordered steps.\n"
        "2. For each step specify: description, files involved, and operation type "
        "(CREATE / MODIFY / DELETE).\n"
        "3. Identify risks or edge cases for each step.\n"
        "4. Output the plan as a JSON object with a 'steps' array. Each element must "
        "have keys: step_id (int), description (str), files_involved (list[str]), "
        "operation (str), and risk_notes (str).\n\n"
        "CONTEXT:\n{context}\n\n"
        "{extra}"
    ),
    "patch": (
        "You are generating a code patch for the following task.\n\n"
        "TASK:\n{task}\n\n"
        "INSTRUCTIONS:\n"
        "1. Review the existing code in the CONTEXT section.\n"
        "2. Write ONLY the changed file contents — do not reproduce unchanged files.\n"
        "3. For each file produce a unified diff (--- a/path, +++ b/path) or the "
        "complete new file content.\n"
        "4. Ensure the patch is syntactically valid and does not break imports.\n"
        "5. Add brief inline comments explaining non-obvious changes.\n\n"
        "CONTEXT:\n{context}\n\n"
        "{extra}"
    ),
    "verify": (
        "You are verifying whether a code change is correct.\n\n"
        "TASK:\n{task}\n\n"
        "INSTRUCTIONS:\n"
        "1. Examine the applied patch and the verification output below.\n"
        "2. Determine if any tests fail, lint errors appear, or type-check issues remain.\n"
        "3. If the change is correct, respond with a JSON object: "
        '{{"correct": true, "summary": "…"}}.\n'
        "4. If the change has problems, respond with: "
        '{{"correct": false, "issues": ["issue1", …], "suggested_fix": "…"}}.\n\n'
        "CONTEXT:\n{context}\n\n"
        "{extra}"
    ),
    "reflect": (
        "You are reflecting on the completed task to improve future performance.\n\n"
        "TASK:\n{task}\n\n"
        "INSTRUCTIONS:\n"
        "1. Summarise what was accomplished.\n"
        "2. Identify what worked well and what could be improved.\n"
        "3. Note any recurring patterns or lessons learned.\n"
        "4. Suggest any follow-up tasks if applicable.\n"
        "5. Output a JSON object with keys: summary (str), lessons (list[str]), "
        "follow_ups (list[str]).\n\n"
        "CONTEXT:\n{context}\n\n"
        "{extra}"
    ),
}


# ---------------------------------------------------------------------------
# System prompt helpers
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_SMALL = (
    "You are a precise coding assistant running on a resource-constrained local model.\n"
    "RULES:\n"
    "- Follow instructions EXACTLY and step-by-step.\n"
    "- Always output valid JSON when requested.\n"
    "- Keep responses concise — avoid unnecessary commentary.\n"
    "- Never fabricate file contents you have not been shown.\n"
    "- If uncertain, say so rather than guessing.\n"
    "- Use structured output formats (JSON, diffs) as instructed.\n"
)

_SYSTEM_PROMPT_MEDIUM = (
    "You are a skilled coding assistant running locally.\n"
    "You have access to relevant code context below.\n"
    "Follow the instructions carefully. Prefer structured output (JSON, diffs) "
    "when requested. Be thorough but concise.\n"
)

_SYSTEM_PROMPT_LARGE = (
    "You are an expert software engineer running as a local autonomous coding agent.\n"
    "You will be given a task and relevant code context. "
    "Use your best judgement to accomplish the task efficiently. "
    "Return structured output when requested, but you may also provide "
    "additional reasoning or suggestions when useful.\n"
)


class ContextAssembler:
    """Assembles prompt strings from retrieved chunks, task descriptions,
    and phase-specific templates, all within a token budget."""

    def __init__(
        self,
        budget_manager: TokenBudgetManager,
        config: LocalForgeConfig,
    ) -> None:
        """Initialise the assembler.

        Parameters
        ----------
        budget_manager:
            Provides token counting and budget allocation.
        config:
            Global localforge configuration.
        """
        self.budget_manager = budget_manager
        self.config = config

    # ------------------------------------------------------------------
    # Chunk formatting
    # ------------------------------------------------------------------

    def format_chunk(self, chunk: FileChunk) -> str:
        """Render a single chunk as a prompt-ready string.

        Format::

            // File: <path> (lines <start>-<end>)
            <content>

        Parameters
        ----------
        chunk:
            The ``FileChunk`` to format.

        Returns
        -------
        str
            Formatted chunk text.
        """
        header = f"// File: {chunk.file_path} (lines {chunk.start_line}-{chunk.end_line})"
        return f"{header}\n{chunk.content}"

    # ------------------------------------------------------------------
    # Context assembly
    # ------------------------------------------------------------------

    def assemble_retrieval_context(
        self,
        chunks: list[FileChunk],
        budget: int,
    ) -> str:
        """Format a list of chunks into a single context block within *budget*.

        Structure::

            === RETRIEVED CODE CONTEXT ===
            <formatted chunk 1>
            ---
            <formatted chunk 2>
            ...
            === END CONTEXT (<n> chunks, <t> tokens) ===

        Parameters
        ----------
        chunks:
            Candidate chunks (will be fitted to the budget).
        budget:
            Maximum token budget for the entire context block.

        Returns
        -------
        str
            Assembled context string.
        """
        header = "=== RETRIEVED CODE CONTEXT ===\n"
        # Reserve tokens for header, footer, separators, and chunk headers
        separator_estimate = max(len(chunks) - 1, 0) * 2  # "---\n" ≈ 2 tokens each
        header_per_chunk = len(chunks) * 10  # "// File: … (lines …)\n" ≈ 10 tokens
        overhead = (
            self.budget_manager.count_tokens(header)
            + 30  # footer
            + separator_estimate
            + header_per_chunk
        )
        fitted = self.budget_manager.fit_chunks_to_budget(
            chunks, max(budget - overhead, 0)
        )

        parts: list[str] = [header]
        for i, chunk in enumerate(fitted):
            if i > 0:
                parts.append("---")
            parts.append(self.format_chunk(chunk))

        body = "\n".join(parts)
        total_tokens = self.budget_manager.count_tokens(body)
        footer = f"\n=== END CONTEXT ({len(fitted)} chunks, {total_tokens} tokens) ==="
        return body + footer

    # ------------------------------------------------------------------
    # Task prompt
    # ------------------------------------------------------------------

    def assemble_task_prompt(
        self,
        task: str,
        context: str,
        phase: str,
        extra_instructions: str = "",
    ) -> str:
        """Build the final user-facing prompt for a given agent phase.

        Parameters
        ----------
        task:
            The user's task description.
        context:
            Pre-assembled context string (from ``assemble_retrieval_context``).
        phase:
            One of ``"analyze"``, ``"plan"``, ``"patch"``, ``"verify"``,
            ``"reflect"``.
        extra_instructions:
            Additional instructions appended to the prompt.

        Returns
        -------
        str
            Fully rendered prompt string.

        Raises
        ------
        ValueError
            If *phase* is not a recognised phase name.
        """
        template = _PHASE_TEMPLATES.get(phase)
        if template is None:
            valid = ", ".join(sorted(_PHASE_TEMPLATES))
            raise ValueError(
                f"Unknown phase {phase!r}. Valid phases: {valid}"
            )
        return template.format(
            task=task,
            context=context,
            extra=extra_instructions,
        )

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def build_system_prompt(self, profile: ModelProfileSettings) -> str:
        """Generate a system prompt tuned to the model's profile.

        Parameters
        ----------
        profile:
            The ``ModelProfileSettings`` describing the target model.

        Returns
        -------
        str
            System prompt string.
        """
        if profile.context_window <= 4096:
            return _SYSTEM_PROMPT_SMALL
        if profile.context_window <= 8192:
            return _SYSTEM_PROMPT_MEDIUM
        return _SYSTEM_PROMPT_LARGE
