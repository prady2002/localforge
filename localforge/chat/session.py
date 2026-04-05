"""Chat session — manages conversation history and persistence."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ChatMessage(BaseModel):
    """A single chat message."""

    role: str = Field(description="user or assistant")
    content: str
    timestamp: float = Field(default_factory=time.time)


class ChatSession(BaseModel):
    """Persistent conversation session."""

    session_id: str = ""
    repo_path: str = "."
    messages: list[ChatMessage] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    model: str = ""

    def add_user_message(self, content: str) -> None:
        self.messages.append(ChatMessage(role="user", content=content))

    def add_assistant_message(self, content: str) -> None:
        self.messages.append(ChatMessage(role="assistant", content=content))

    def get_ollama_messages(self, max_messages: int = 80) -> list[dict[str, str]]:
        """Return messages in Ollama's expected format, limited to recent history.

        Keeps the first 2 messages (initial context) and the most recent
        messages to maximise useful conversation context.
        """
        if len(self.messages) <= max_messages:
            return [{"role": m.role, "content": m.content} for m in self.messages]

        # Keep first 2 (initial context) + most recent messages
        head = self.messages[:2]
        tail = self.messages[-(max_messages - 2):]
        kept = head + tail
        return [{"role": m.role, "content": m.content} for m in kept]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            self.model_dump_json(indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> ChatSession:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)

    def clear(self) -> None:
        self.messages.clear()
