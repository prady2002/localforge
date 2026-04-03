"""Persistence for multi-agent orchestrator state."""

from __future__ import annotations

import hashlib
from pathlib import Path

from localforge.core.models import MultiAgentState


class StateManager:
    """Save and load :class:`MultiAgentState` snapshots to disk."""

    def __init__(self, base_dir: str = ".localforge/states") -> None:
        self.base_dir = Path(base_dir)

    def get_state_path(self, task: str) -> Path:
        """Derive a deterministic file path from a task description."""
        digest = hashlib.sha256(task.encode()).hexdigest()[:16]
        return self.base_dir / f"{digest}.json"

    def save_state(self, state: MultiAgentState, path: Path) -> None:
        """Serialise *state* to *path* as JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    def load_state(self, path: Path) -> MultiAgentState:
        """Deserialise a :class:`MultiAgentState` from *path*."""
        raw = path.read_text(encoding="utf-8")
        return MultiAgentState.model_validate_json(raw)
