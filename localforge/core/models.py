"""Domain models shared across localforge subsystems."""

from __future__ import annotations

import enum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


class FileChunk(BaseModel):
    """A scored fragment of a source file returned by the retrieval system."""

    file_path: str = Field(description="Repo-relative path to the source file.")
    start_line: int = Field(description="1-based inclusive start line of the chunk.")
    end_line: int = Field(description="1-based inclusive end line of the chunk.")
    content: str = Field(description="Raw text content of the chunk.")
    score: float = Field(default=0.0, description="Relevance score assigned by retrieval.")


class RetrievalResult(BaseModel):
    """Aggregated output from a retrieval query."""

    chunks: list[FileChunk] = Field(default_factory=list)
    query: str = Field(description="The original search query.")
    total_found: int = Field(default=0, description="Total chunks matching the query.")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


class OperationType(str, enum.Enum):
    """The kind of file operation a plan step or patch describes."""

    CREATE = "CREATE"
    MODIFY = "MODIFY"
    DELETE = "DELETE"


class StepStatus(str, enum.Enum):
    """Execution status of a single plan step."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class PlanStep(BaseModel):
    """One discrete action within an agent plan."""

    step_id: int = Field(description="Sequential identifier for the step.")
    description: str = Field(description="Human-readable description of the action.")
    files_involved: list[str] = Field(default_factory=list, description="Repo-relative file paths.")
    operation: OperationType = Field(description="Type of file operation.")
    status: StepStatus = Field(default=StepStatus.PENDING, description="Current execution status.")


class AgentPlan(BaseModel):
    """A structured plan the agent intends to execute."""

    task: str = Field(description="Original user task description.")
    steps: list[PlanStep] = Field(default_factory=list, description="Ordered list of plan steps.")
    reasoning: str = Field(default="", description="Chain-of-thought reasoning behind the plan.")
    estimated_complexity: str = Field(
        default="medium",
        description="Rough complexity estimate (low, medium, high).",
    )


# ---------------------------------------------------------------------------
# Patching
# ---------------------------------------------------------------------------


class PatchOperation(BaseModel):
    """Represents a single file-level patch the agent wants to apply."""

    file_path: str = Field(description="Repo-relative path to the target file.")
    operation_type: OperationType = Field(description="CREATE, MODIFY, or DELETE.")
    original_content: str | None = Field(
        default=None,
        description="Content of the file before patching (None for CREATE).",
    )
    new_content: str | None = Field(
        default=None,
        description="Content of the file after patching (None for DELETE).",
    )
    diff: str = Field(default="", description="Unified diff representation of the change.")
    description: str = Field(default="", description="Human-readable summary of the patch.")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class VerificationResult(BaseModel):
    """Outcome of running a verification command (tests, lint, type-check, etc.)."""

    success: bool = Field(description="Whether the command exited cleanly.")
    command: str = Field(description="The shell command that was executed.")
    stdout: str = Field(default="", description="Captured standard output.")
    stderr: str = Field(default="", description="Captured standard error.")
    exit_code: int = Field(default=0, description="Process exit code.")
    error_count: int = Field(default=0, description="Number of errors detected in output.")
    warning_count: int = Field(default=0, description="Number of warnings detected in output.")


# ---------------------------------------------------------------------------
# Agent state
# ---------------------------------------------------------------------------


class AgentPhase(str, enum.Enum):
    """High-level phase the agent loop is currently in."""

    UNDERSTANDING = "UNDERSTANDING"
    PLANNING = "PLANNING"
    IMPLEMENTING = "IMPLEMENTING"
    VERIFYING = "VERIFYING"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"


class AgentState(BaseModel):
    """Snapshot of the agent's mutable state at any point in the loop."""

    task: str = Field(description="The user's original task description.")
    iteration: int = Field(default=0, description="Current iteration count.")
    phase: AgentPhase = Field(
        default=AgentPhase.UNDERSTANDING,
        description="Current high-level phase.",
    )
    retrieved_chunks: list[FileChunk] = Field(
        default_factory=list,
        description="Chunks retrieved so far for context.",
    )
    plan: AgentPlan | None = Field(default=None, description="The active plan, if any.")
    patches_applied: list[PatchOperation] = Field(
        default_factory=list,
        description="Patches that have been applied to disk.",
    )
    verification_results: list[VerificationResult] = Field(
        default_factory=list,
        description="Results from verification commands.",
    )
    completed: bool = Field(default=False, description="Whether the task finished successfully.")
    error: str | None = Field(default=None, description="Error message if the agent failed.")
    summary: str = Field(default="", description="Final summary of what was accomplished.")


# ---------------------------------------------------------------------------
# Multi-agent models
# ---------------------------------------------------------------------------


class AgentRole(str, enum.Enum):
    ORCHESTRATOR = "ORCHESTRATOR"
    ANALYZER = "ANALYZER"
    PLANNER = "PLANNER"
    CODER = "CODER"
    VERIFIER = "VERIFIER"
    REFLECTOR = "REFLECTOR"
    SUMMARIZER = "SUMMARIZER"


class AgentMessage(BaseModel):
    role: AgentRole
    content: str
    structured_data: dict | None = None
    tokens_used: int = 0
    iteration: int = 0
    success: bool = True
    error: str | None = None


class AgentHandoff(BaseModel):
    from_role: AgentRole
    to_role: AgentRole
    payload: dict
    context_chunks: list[FileChunk] = []
    instruction: str


class MultiAgentState(BaseModel):
    task: str
    iteration: int = 0
    current_agent: AgentRole = AgentRole.ORCHESTRATOR
    messages: list[AgentMessage] = []
    handoffs: list[AgentHandoff] = []
    agent_states: dict[str, dict] = {}
    plan: AgentPlan | None = None
    patches_applied: list[PatchOperation] = []
    verification_results: list[VerificationResult] = []
    completed: bool = False
    final_summary: str | None = None
    total_tokens_used: int = 0
