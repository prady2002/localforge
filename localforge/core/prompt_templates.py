"""Prompt templates for localforge's multi-agent system.

Each agent role has a system prompt (constant) and a task-prompt builder
(function) that returns a complete user-turn message string.
"""

from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------------------
# System prompts – one per agent role
# ---------------------------------------------------------------------------

SYSTEM_ANALYZER = """\
You are the Analyzer Agent in a coding assistant system called LocalForge.
Your ONLY job: read a task description and retrieved code, then output a structured analysis.
You do NOT write code. You do NOT create plans.
You ONLY analyze and output JSON.

You work with ANY technology stack: Python, JavaScript, TypeScript, Go, Rust,
Java, C#, C/C++, Ruby, PHP, Kotlin, Swift, and more.

Be extremely precise. Only refer to code that is actually present in the provided context.
Never guess or hallucinate file contents or function names.
If you are uncertain about something, say so explicitly in your output.
When analyzing, pay close attention to:
- The full repository structure provided
- Import chains and module dependencies (language-specific)
- Function signatures, class hierarchies, and data flow
- Configuration files and entry points (pyproject.toml, package.json, Cargo.toml, etc.)
- Build system files (Makefile, CMakeLists.txt, build.gradle, pom.xml, etc.)

Output ONLY valid JSON. No markdown. No explanation outside the JSON."""

SYSTEM_PLANNER = """\
You are the Planner Agent in a coding assistant system called LocalForge.
Your ONLY job: given an analysis, create a concrete, ordered execution plan.
You do NOT write code. You do NOT analyze. You ONLY plan.

Each step must:
- Reference ONLY files that actually exist (as confirmed by analysis)
- Be small enough to implement in a single patch
- Have a clear success criterion
- Specify EXACTLY which files are touched

Output ONLY valid JSON. No markdown. No explanation outside the JSON."""

SYSTEM_CODER = """\
You are the Coder Agent in a coding assistant system called LocalForge.
Your ONLY job: implement ONE specific plan step by writing a precise code patch.
You do NOT plan. You do NOT analyze. You ONLY write code.

You work with ANY technology stack: Python, JavaScript, TypeScript, Go, Rust,
Java, C#, C/C++, Ruby, PHP, Kotlin, Swift, and more.

Rules:
- Output ONLY the patch JSON, nothing else
- The search_block MUST be exact text copied from the provided file content
- Include enough context in search_block to make the match unique (3+ lines)
- Make the MINIMAL change required — do not refactor unrelated code
- Never invent functions or imports that don't exist in the codebase
- Ensure your replacement code is syntactically valid for the target language
- Preserve existing indentation style and language conventions
- If you are unsure about existing code, output {"error": "need_more_context", "reason": "..."}

Output ONLY valid JSON. No markdown. No explanation outside the JSON."""

SYSTEM_VERIFIER = """\
You are the Verifier Agent in a coding assistant system called LocalForge.
Your ONLY job: interpret verification command output (test results,
lint, type check) and decide next action.
You do NOT write code. You do NOT plan. You ONLY interpret results and decide.

Output ONLY valid JSON. No markdown. No explanation outside the JSON."""

SYSTEM_REFLECTOR = """\
You are the Reflector Agent in a coding assistant system called LocalForge.
Your ONLY job: when a patch fails verification, analyze WHY it failed
and produce a corrected approach.
You do NOT write the fix yourself. You write INSTRUCTIONS for the Coder Agent.

Be precise. Identify the exact error. Provide specific corrective instructions.
Do not repeat what was already tried.

Output ONLY valid JSON. No markdown. No explanation outside the JSON."""

SYSTEM_SUMMARIZER = """\
You are the Summarizer Agent in a coding assistant system called LocalForge.
Your ONLY job: given the full history of agent actions and patches,
write a clear human-readable summary.
Output ONLY valid JSON. No markdown. No explanation outside the JSON."""

SYSTEM_ORCHESTRATOR = """\
You are the Orchestrator Agent in LocalForge.
You coordinate all other agents. You decide which agent runs next and what it receives.
You track overall progress and decide when the task is complete.
Output ONLY valid JSON."""


# ---------------------------------------------------------------------------
# Mapping from AgentRole enum values to system prompts for convenience
# ---------------------------------------------------------------------------

SYSTEM_PROMPTS: dict[str, str] = {
    "ANALYZER": SYSTEM_ANALYZER,
    "PLANNER": SYSTEM_PLANNER,
    "CODER": SYSTEM_CODER,
    "VERIFIER": SYSTEM_VERIFIER,
    "REFLECTOR": SYSTEM_REFLECTOR,
    "SUMMARIZER": SYSTEM_SUMMARIZER,
    "ORCHESTRATOR": SYSTEM_ORCHESTRATOR,
}


# ---------------------------------------------------------------------------
# JSON schema strings – used by chat_structured as response_schema
# ---------------------------------------------------------------------------

ANALYZER_SCHEMA = json.dumps(
    {
        "understanding": "string – what the task requires in detail",
        "affected_files": ["list of repo-relative file paths from context that are relevant"],
        "root_cause": "string – if bug fix, the likely root cause; else empty string",
        "complexity": "simple|moderate|complex",
        "approach": "string – specific strategy to implement the task",
        "risks": ["list of things that could go wrong"],
        "needs_more_context": "boolean",
        "additional_context_queries": ["search queries if more context is needed"],
    },
    indent=2,
)

PLANNER_SCHEMA = json.dumps(
    {
        "reasoning": "string – why this plan will work",
        "estimated_complexity": "simple|moderate|complex",
        "steps": [
            {
                "step_id": "integer",
                "description": "string – what this step does",
                "files_involved": ["file.py"],
                "operation": "MODIFY|CREATE|DELETE",
                "depends_on": ["list of step_id integers this step depends on"],
                "success_criterion": "string – how to know this step worked",
            }
        ],
    },
    indent=2,
)

CODER_SCHEMA = json.dumps(
    {
        "description": "string – what this patch does",
        "file_path": "relative/path/to/file.py",
        "operation": "MODIFY|CREATE|DELETE",
        "search_block": "exact existing code to replace (must exist verbatim in file)",
        "replace_block": "new code to insert in place of search_block",
        "full_content": "string – full file content, only for CREATE operations",
        "confidence": "float 0.0-1.0",
        "explanation": "string – why this change fixes the issue",
    },
    indent=2,
)

VERIFIER_SCHEMA = json.dumps(
    {
        "passed": "boolean",
        "confidence": "float 0.0-1.0",
        "failure_type": "syntax|test|lint|type|none",
        "error_summary": "string – one line summary",
        "affected_files": ["files with errors"],
        "next_action": "continue|retry|escalate|abort",
        "retry_instructions": "string – specific guidance for coder if retrying",
    },
    indent=2,
)

REFLECTOR_SCHEMA = json.dumps(
    {
        "failure_analysis": "string – why previous attempts failed",
        "root_cause": "string – the actual underlying issue",
        "revised_approach": "string – completely different strategy",
        "specific_instructions": "string – exact instructions for coder agent",
        "alternative_files": ["maybe these files need to be changed instead"],
        "should_skip": "boolean",
        "skip_reason": "string or null",
    },
    indent=2,
)

SUMMARIZER_SCHEMA = json.dumps(
    {
        "task_completed": "boolean",
        "summary": "string – human-readable paragraph summary",
        "files_modified": ["list"],
        "files_created": ["list"],
        "files_deleted": ["list"],
        "tests_passed": "boolean",
        "total_iterations": "integer",
        "key_changes": ["bullet point list of main changes"],
    },
    indent=2,
)

ORCHESTRATOR_SCHEMA = json.dumps(
    {
        "next_agent": "ANALYZER|PLANNER|CODER|VERIFIER|REFLECTOR|SUMMARIZER",
        "reason": "string – why this agent is next",
        "instruction": "string – specific instruction for that agent",
        "task_complete": "boolean",
        "completion_reason": "string or null",
    },
    indent=2,
)


# ---------------------------------------------------------------------------
# Task-prompt builder functions
# ---------------------------------------------------------------------------


def analyzer_prompt(task: str, context: str, repo_structure: str) -> str:
    """Build the user-turn message for the Analyzer agent."""
    return (
        f"TASK:\n{task}\n\n"
        f"REPOSITORY STRUCTURE:\n{repo_structure}\n\n"
        f"RETRIEVED CODE CONTEXT:\n{context}\n\n"
        "Analyze the task against the provided code and repository structure.\n"
        "Respond with a JSON object containing these exact keys:\n"
        "- understanding: what the task requires in detail\n"
        "- affected_files: list of file paths from the context that are relevant\n"
        "- root_cause: if this is a bug fix, what is the likely "
        "root cause (empty string otherwise)\n"
        "- complexity: one of simple, moderate, complex\n"
        "- approach: specific strategy to implement the task\n"
        "- risks: list of things that could go wrong\n"
        "- needs_more_context: boolean indicating if more code context is needed\n"
        "- additional_context_queries: list of search queries if more context is needed\n\n"
        "Output ONLY the JSON."
    )


def planner_prompt(task: str, analysis: dict[str, Any], context: str) -> str:
    """Build the user-turn message for the Planner agent."""
    analysis_json = json.dumps(analysis, indent=2)
    return (
        f"TASK:\n{task}\n\n"
        f"ANALYSIS FROM ANALYZER AGENT:\n{analysis_json}\n\n"
        f"CODE CONTEXT:\n{context}\n\n"
        "Create an ordered execution plan based on the analysis above.\n"
        "Respond with a JSON object containing these exact keys:\n"
        "- reasoning: why this plan will work\n"
        "- estimated_complexity: one of simple, moderate, complex\n"
        "- steps: ordered list of step objects, each with:\n"
        "    - step_id: sequential integer starting at 1\n"
        "    - description: what this step does\n"
        "    - files_involved: list of repo-relative file paths\n"
        "    - operation: one of MODIFY, CREATE, DELETE\n"
        "    - depends_on: list of step_id integers this step depends on\n"
        "    - success_criterion: how to verify this step worked\n\n"
        "Output ONLY the JSON."
    )


def coder_prompt(
    task: str,
    step: dict[str, Any],
    file_content: str,
    file_path: str,
    context: str,
    previous_error: str | None = None,
) -> str:
    """Build the user-turn message for the Coder agent."""
    step_json = json.dumps(step, indent=2)

    parts = [
        f"TASK:\n{task}\n",
        f"PLAN STEP TO IMPLEMENT:\n{step_json}\n",
        f"TARGET FILE ({file_path}):\n```\n{file_content}\n```\n",
        f"ADDITIONAL CONTEXT:\n{context}\n",
    ]

    if previous_error is not None:
        parts.append(
            f"PREVIOUS ATTEMPT FAILED WITH:\n{previous_error}\n"
            "Do NOT repeat the same mistake. Use a different approach.\n"
        )

    parts.append(
        "Write a patch for this step.\n"
        "Respond with a JSON object containing these exact keys:\n"
        "- description: what this patch does\n"
        "- file_path: repo-relative path to the file\n"
        "- operation: one of MODIFY, CREATE, DELETE\n"
        "- search_block: exact existing code to replace (must match the file verbatim)\n"
        "- replace_block: new code to insert in place of search_block\n"
        "- full_content: full file content (only for CREATE operations, empty string otherwise)\n"
        "- confidence: float between 0.0 and 1.0\n"
        "- explanation: why this change fixes the issue\n\n"
        "Output ONLY the JSON."
    )
    return "\n".join(parts)


def verifier_prompt(
    task: str,
    step: dict[str, Any],
    verification_output: str,
    errors_parsed: list[Any],
) -> str:
    """Build the user-turn message for the Verifier agent."""
    step_json = json.dumps(step, indent=2)
    errors_json = json.dumps(errors_parsed, indent=2)
    return (
        f"TASK:\n{task}\n\n"
        f"PLAN STEP THAT WAS IMPLEMENTED:\n{step_json}\n\n"
        f"VERIFICATION COMMAND OUTPUT:\n{verification_output}\n\n"
        f"PARSED ERRORS:\n{errors_json}\n\n"
        "Interpret the verification output and decide what to do next.\n"
        "Respond with a JSON object containing these exact keys:\n"
        "- passed: boolean indicating whether verification succeeded\n"
        "- confidence: float between 0.0 and 1.0\n"
        "- failure_type: one of syntax, test, lint, type, none\n"
        "- error_summary: one-line summary of the primary issue\n"
        "- affected_files: list of files that have errors\n"
        "- next_action: one of continue, retry, escalate, abort\n"
        "- retry_instructions: specific guidance for the coder if retrying\n\n"
        "Output ONLY the JSON."
    )


def reflector_prompt(
    task: str,
    step: dict[str, Any],
    attempts: list[dict[str, Any]],
    errors: list[str],
) -> str:
    """Build the user-turn message for the Reflector agent."""
    step_json = json.dumps(step, indent=2)
    attempts_json = json.dumps(attempts, indent=2)
    errors_text = "\n".join(f"  - {e}" for e in errors)
    return (
        f"TASK:\n{task}\n\n"
        f"PLAN STEP:\n{step_json}\n\n"
        f"PREVIOUS ATTEMPTS ({len(attempts)} total):\n{attempts_json}\n\n"
        f"ERRORS ENCOUNTERED:\n{errors_text}\n\n"
        "Analyze why the previous attempts failed and suggest a revised approach.\n"
        "Respond with a JSON object containing these exact keys:\n"
        "- failure_analysis: why previous attempts failed\n"
        "- root_cause: the actual underlying issue\n"
        "- revised_approach: a completely different strategy from what was tried\n"
        "- specific_instructions: exact instructions for the coder agent\n"
        "- alternative_files: list of files that might need to be changed instead\n"
        "- should_skip: boolean indicating whether this step should be skipped entirely\n"
        "- skip_reason: reason for skipping (null if should_skip is false)\n\n"
        "Output ONLY the JSON."
    )


def summarizer_prompt(
    task: str,
    patches: list[Any],
    verification_results: list[Any],
    iterations: int,
) -> str:
    """Build the user-turn message for the Summarizer agent."""
    patches_json = json.dumps(
        [p if isinstance(p, dict) else p.model_dump() for p in patches],
        indent=2,
        default=str,
    )
    verifications_json = json.dumps(
        [v if isinstance(v, dict) else v.model_dump() for v in verification_results],
        indent=2,
        default=str,
    )
    return (
        f"TASK:\n{task}\n\n"
        f"PATCHES APPLIED ({len(patches)} total):\n{patches_json}\n\n"
        f"VERIFICATION RESULTS:\n{verifications_json}\n\n"
        f"TOTAL ITERATIONS: {iterations}\n\n"
        "Write a human-readable summary of everything that was done.\n"
        "Respond with a JSON object containing these exact keys:\n"
        "- task_completed: boolean indicating overall success\n"
        "- summary: human-readable paragraph summarizing the work\n"
        "- files_modified: list of files that were modified\n"
        "- files_created: list of files that were created\n"
        "- files_deleted: list of files that were deleted\n"
        "- tests_passed: boolean indicating whether tests passed\n"
        "- total_iterations: integer count of iterations used\n"
        "- key_changes: list of bullet-point descriptions of main changes\n\n"
        "Output ONLY the JSON."
    )


def orchestrator_prompt(
    task: str,
    current_state: dict[str, Any],
    available_agents: list[str],
) -> str:
    """Build the user-turn message for the Orchestrator agent."""
    state_json = json.dumps(current_state, indent=2, default=str)
    agents_list = ", ".join(available_agents)
    return (
        f"TASK:\n{task}\n\n"
        f"CURRENT STATE:\n{state_json}\n\n"
        f"AVAILABLE AGENTS: {agents_list}\n\n"
        "Decide which agent should run next and why.\n"
        "Respond with a JSON object containing these exact keys:\n"
        "- next_agent: one of ANALYZER, PLANNER, CODER, VERIFIER, REFLECTOR, SUMMARIZER\n"
        "- reason: why this agent should run next\n"
        "- instruction: specific instruction to pass to that agent\n"
        "- task_complete: boolean indicating if the task is fully done\n"
        "- completion_reason: reason the task is complete (null if not complete)\n\n"
        "Output ONLY the JSON."
    )
