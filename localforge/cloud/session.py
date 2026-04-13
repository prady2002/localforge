"""Cloud chat session — extends the base ChatSession with API conversation state."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class CloudChatMessage(BaseModel):
    """A single message in the cloud chat session."""

    role: str = Field(description="user or assistant")
    content: str
    thinking: str = ""
    timestamp: float = Field(default_factory=time.time)


class CloudChatSession(BaseModel):
    """Persistent conversation session for the cloud chat engine.

    Stores both the localforge message history and the Bell API's
    ``conversation_id`` + raw API message history so conversations
    can resume across restarts.
    """

    session_id: str = ""
    repo_path: str = "."
    model: str = "gemini-3.1-pro-preview"

    # Localforge-level messages (for display / slash commands)
    messages: list[CloudChatMessage] = Field(default_factory=list)

    # Bell API conversation state
    conversation_id: str = ""
    api_messages: list[dict[str, Any]] = Field(default_factory=list)

    # Focus paths (same as base ChatSession)
    focus_paths: list[str] = Field(default_factory=list)

    created_at: float = Field(default_factory=time.time)

    # ------------------------------------------------------------------
    # Focus path management (mirrors ChatSession)
    # ------------------------------------------------------------------

    def add_focus_path(self, path: str) -> bool:
        normalised = path.replace("\\", "/").strip("/")
        if not normalised:
            return False
        if normalised not in self.focus_paths:
            self.focus_paths.append(normalised)
            return True
        return False

    def remove_focus_path(self, path: str) -> int:
        normalised = path.replace("\\", "/").strip("/")
        before = len(self.focus_paths)
        self.focus_paths = [
            p for p in self.focus_paths
            if normalised not in p and p != normalised
        ]
        return before - len(self.focus_paths)

    def clear_focus_paths(self) -> None:
        self.focus_paths.clear()

    def has_focus(self) -> bool:
        return bool(self.focus_paths)

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def add_user_message(self, content: str) -> None:
        self.messages.append(CloudChatMessage(role="user", content=content))

    def add_assistant_message(self, content: str, thinking: str = "") -> None:
        self.messages.append(
            CloudChatMessage(role="assistant", content=content, thinking=thinking)
        )

    def get_messages_for_display(self, max_messages: int = 200) -> list[dict[str, str]]:
        """Return messages suitable for display, capped at *max_messages*."""
        tail = self.messages[-max_messages:] if len(self.messages) > max_messages else self.messages
        return [{"role": m.role, "content": m.content} for m in tail]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            self.model_dump_json(indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> CloudChatSession:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)

    def clear(self) -> None:
        self.messages.clear()
        self.conversation_id = ""
        self.api_messages.clear()
