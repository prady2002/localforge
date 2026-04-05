"""Token budget management for context window packing."""

from __future__ import annotations

from typing import Protocol

from localforge.core.config import LocalForgeConfig
from localforge.core.models import FileChunk


class _TokenEncoder(Protocol):
    def encode(self, text: str) -> list[int]: ...
    def decode(self, tokens: list[int]) -> str: ...


class _FallbackEncoder:
    """Approximate token counter used when tiktoken is unavailable."""

    @staticmethod
    def encode(text: str) -> list[int]:
        """Return a list whose length approximates the token count.

        Uses a simple ``len(text) // 4`` heuristic (roughly one token per
        four characters for English text).
        """
        return [0] * (len(text) // 4)

    @staticmethod
    def decode(tokens: list[int]) -> str:
        return "" * len(tokens)


class TokenBudgetManager:
    """Manages token counting and budget allocation for LLM context windows.

    Uses the ``cl100k_base`` tiktoken encoding (shared across all instances)
    to measure text and greedily pack the highest-scored chunks into a
    fixed token budget.
    """

    _encoder: _TokenEncoder | None = None  # lazily initialised, shared across instances

    def __init__(self, config: LocalForgeConfig) -> None:
        """Initialise the budget manager.

        Parameters
        ----------
        config:
            Global localforge configuration (provides ``max_context_tokens``).
        """
        self.config = config

    # ------------------------------------------------------------------
    # Token counting
    # ------------------------------------------------------------------

    @classmethod
    def _get_encoder(cls) -> _TokenEncoder:
        """Return the cached tiktoken encoder, creating it on first call.

        Falls back to a simple word-based estimator if tiktoken cannot
        load its encoding data (e.g. network / SSL issues).
        """
        if cls._encoder is None:
            try:
                import tiktoken

                cls._encoder = tiktoken.get_encoding("cl100k_base")
            except Exception:
                # Fallback: rough estimate using len(text) // 4
                cls._encoder = _FallbackEncoder()
        return cls._encoder

    def count_tokens(self, text: str) -> int:
        """Count the number of tokens in *text* using cl100k_base.

        Parameters
        ----------
        text:
            The string to tokenise.

        Returns
        -------
        int
            Token count.
        """
        return len(self._get_encoder().encode(text))

    # ------------------------------------------------------------------
    # Budget computation
    # ------------------------------------------------------------------

    def get_available_tokens(
        self,
        system_prompt: str,
        task: str,
        reserved_output: int = 1024,
    ) -> int:
        """Compute remaining tokens available for retrieved context.

        Parameters
        ----------
        system_prompt:
            The system-level prompt that will be sent to the model.
        task:
            The user task description that will be included in the prompt.
        reserved_output:
            Tokens reserved for the model's reply.

        Returns
        -------
        int
            Number of tokens still available for injected context.
        """
        used = (
            self.count_tokens(system_prompt)
            + self.count_tokens(task)
            + reserved_output
        )
        return max(self.config.max_context_tokens - used, 0)

    # ------------------------------------------------------------------
    # Chunk packing
    # ------------------------------------------------------------------

    def fit_chunks_to_budget(
        self,
        chunks: list[FileChunk],
        budget: int,
    ) -> list[FileChunk]:
        """Greedily select chunks that fit within *budget* tokens.

        Chunks are considered in descending score order.  If a single chunk
        exceeds the remaining budget it is truncated (the beginning is kept
        and ``... [truncated]`` is appended) so that at least a portion of
        high-value context is preserved.

        Parameters
        ----------
        chunks:
            Candidate chunks, typically already ranked by relevance score.
        budget:
            Maximum number of tokens the selected chunks may consume.

        Returns
        -------
        list[FileChunk]
            Subset (possibly truncated) of *chunks* fitting the budget.
        """
        sorted_chunks = sorted(chunks, key=lambda c: c.score, reverse=True)
        selected: list[FileChunk] = []
        remaining = budget

        for chunk in sorted_chunks:
            tokens = self.count_tokens(chunk.content)
            if tokens <= remaining:
                selected.append(chunk)
                remaining -= tokens
            elif remaining > 0:
                # Truncate the chunk to fit the remaining budget
                truncated_content = self._truncate_to_tokens(
                    chunk.content, remaining
                )
                selected.append(
                    chunk.model_copy(update={"content": truncated_content})
                )
                remaining -= self.count_tokens(truncated_content)
            # else: skip — no room left

        return selected

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """Truncate *text* to at most *max_tokens*, appending a marker.

        Parameters
        ----------
        text:
            Source text to truncate.
        max_tokens:
            Hard upper-bound on tokens (including the truncation marker).

        Returns
        -------
        str
            Truncated text ending with ``... [truncated]``.
        """
        suffix = "\n... [truncated]"
        suffix_tokens = self.count_tokens(suffix)
        target = max(max_tokens - suffix_tokens, 1)

        enc = self._get_encoder()
        token_ids = enc.encode(text)[:target]
        return str(enc.decode(token_ids)) + suffix
