"""Abstract base class for all localforge agents."""

from __future__ import annotations

import abc
import json
import logging

from localforge.context_manager.assembler import ContextAssembler
from localforge.context_manager.budget import TokenBudgetManager
from localforge.core.config import LocalForgeConfig
from localforge.core.models import AgentHandoff, AgentMessage, AgentRole, FileChunk
from localforge.core.ollama_client import OllamaClient


class BaseAgent(abc.ABC):
    """Base class that every specialist agent inherits from.

    Subclasses must set ``self.system_prompt`` and implement :meth:`execute`.
    """

    def __init__(
        self,
        role: AgentRole,
        ollama: OllamaClient,
        assembler: ContextAssembler,
        budget_manager: TokenBudgetManager,
        config: LocalForgeConfig,
    ) -> None:
        self.role = role
        self.ollama = ollama
        self.assembler = assembler
        self.budget_manager = budget_manager
        self.config = config
        self.system_prompt: str = ""  # must be set by subclass
        self.message_history: list[AgentMessage] = []
        self._last_tokens_used: int = 0
        self.logger = logging.getLogger(f"localforge.agent.{role.value.lower()}")

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def execute(self, handoff: AgentHandoff) -> AgentMessage:
        """Process a handoff from the orchestrator and return a message."""
        ...

    # ------------------------------------------------------------------
    # LLM interaction
    # ------------------------------------------------------------------

    async def _call_llm(self, user_prompt: str, schema: str) -> dict:
        """Send a structured request to the LLM and return parsed JSON.

        Steps
        -----
        1. Count tokens in system prompt + user prompt.
        2. If over budget, truncate the user prompt's context section.
        3. Call ``ollama.chat_structured``.
        4. Parse the JSON response.
        5. Record the exchange in ``message_history``.
        6. Return the parsed dict.
        """
        budget = self.config.max_context_tokens
        prompt_tokens = self.budget_manager.count_tokens(
            self.system_prompt + user_prompt
        )

        if prompt_tokens > budget:
            allowed = max(budget - self.budget_manager.count_tokens(self.system_prompt) - 1024, 0)
            user_prompt = self.budget_manager._truncate_to_tokens(user_prompt, allowed)

        messages = [{"role": "user", "content": user_prompt}]

        raw = await self.ollama.chat_structured(
            messages,
            system=self.system_prompt,
            response_schema=schema,
            agent_role=self.role.value,
        )

        self._last_tokens_used = self.budget_manager.count_tokens(
            self.system_prompt + user_prompt + raw
        )

        try:
            parsed = json.loads(raw)
            return parsed
        except json.JSONDecodeError as exc:
            self.logger.warning("Failed to parse LLM response as JSON: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # Budget helpers
    # ------------------------------------------------------------------

    def _get_token_budget(self) -> int:
        """Return the number of tokens available for context in this call."""
        return max(
            self.config.max_context_tokens
            - self.budget_manager.count_tokens(self.system_prompt)
            - 1024,
            0,
        )

    # ------------------------------------------------------------------
    # Context formatting
    # ------------------------------------------------------------------

    def _format_context(self, chunks: list[FileChunk]) -> str:
        """Format *chunks* into a context string within the token budget."""
        budget = self._get_token_budget()
        return self.assembler.assemble_retrieval_context(chunks, budget=budget)

    # ------------------------------------------------------------------
    # Message recording
    # ------------------------------------------------------------------

    def _record_message(
        self,
        content: str,
        structured_data: dict,
        success: bool,
        tokens: int,
    ) -> AgentMessage:
        """Create an :class:`AgentMessage`, append it to history, and return it."""
        msg = AgentMessage(
            role=self.role,
            content=content,
            structured_data=structured_data,
            tokens_used=tokens,
            success=success,
        )
        self.message_history.append(msg)
        return msg
