"""Async Ollama HTTP client for localforge."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator
from typing import Any

import httpx
from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner

from localforge.core.config import LocalForgeConfig

logger = logging.getLogger(__name__)

_console = Console()

# Retry configuration for reliable Ollama communication
_MAX_RETRIES = 5
_BASE_RETRY_DELAY = 1.0  # seconds
_MAX_RETRY_DELAY = 30.0  # seconds
_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    TimeoutError,
)


def _get_retry_delay(attempt: int) -> float:
    """Calculate exponential backoff with jitter for retry delay."""
    # Exponential backoff: 1s, 2s, 4s, 8s, 16s, 30s (capped)
    delay = min(_BASE_RETRY_DELAY * (2 ** attempt), _MAX_RETRY_DELAY)
    # Add jitter: ±20% of delay
    jitter = delay * 0.2 * (2 * random.random() - 1)
    return max(0.1, delay + jitter)


def get_model_context_window(model_name: str) -> int:
    """Estimate context window size from the model name.

    Falls back to heuristics when the Ollama API is not available.
    """
    name = model_name.lower()

    if "70b" in name:
        base = 32768
    elif "32b" in name:
        base = 16384
    elif "13b" in name or "14b" in name:
        base = 8192
    elif "7b" in name:
        base = 4096
    else:
        base = 4096

    if "coder" in name:
        base *= 2

    return base


class OllamaClient:
    """Async client for the Ollama REST API."""

    def __init__(self, config: LocalForgeConfig) -> None:
        self.config = config
        self.base_url = config.ollama_base_url.rstrip("/")
        self.model = config.model_name
        # Local models can be very slow — use generous timeouts.
        # read=600s handles large context generation on CPU-only machines.
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=60.0, read=600.0, write=60.0, pool=60.0),
        )
        # Controls whether streaming responses display tokens in real time.
        self.stream_to_console: bool = True
        # Detected context window (populated by detect_context_window)
        self._detected_context_window: int | None = None
        # Model capability flags (populated by detect_capabilities)
        self.supports_tools: bool = True  # Assume yes until proven otherwise
        self.supports_json_mode: bool = False
        self.model_family: str = ""  # e.g., "qwen", "llama", "deepseek", "mistral"

    def _default_options(self, temperature: float, num_predict: int | None = None) -> dict[str, Any]:
        """Build the Ollama ``options`` dict with num_ctx when known."""
        opts: dict[str, Any] = {"temperature": temperature}
        num_ctx = self._detected_context_window or self.config.max_context_tokens
        if num_ctx:
            opts["num_ctx"] = num_ctx
        if num_predict is not None:
            opts["num_predict"] = num_predict
        return opts

    async def detect_capabilities(self) -> dict[str, Any]:
        """Detect model capabilities: tool calling, JSON mode, model family.

        Returns a dict with detected capabilities for logging/display.
        """
        name = self.model.lower()

        # Detect model family
        family_map = {
            "qwen": "qwen", "llama": "llama", "codellama": "llama",
            "deepseek": "deepseek", "mistral": "mistral", "mixtral": "mistral",
            "gemma": "gemma", "codegemma": "gemma", "phi": "phi",
            "starcoder": "starcoder", "codestral": "mistral",
            "wizardcoder": "wizard", "openchat": "openchat",
        }
        for key, family in family_map.items():
            if key in name:
                self.model_family = family
                break

        # Tool calling is generally supported by modern models via Ollama
        # but some older/smaller ones may fail. We detect via a probe if needed.
        self.supports_tools = True

        # JSON mode is supported by most models via Ollama's format parameter
        self.supports_json_mode = True

        capabilities = {
            "model": self.model,
            "family": self.model_family or "unknown",
            "supports_tools": self.supports_tools,
            "supports_json_mode": self.supports_json_mode,
            "context_window": self._detected_context_window,
        }

        logger.info("Model capabilities: %s", capabilities)
        return capabilities

    async def close(self) -> None:
        await self._client.aclose()

    async def preload_model(self) -> None:
        """Ask Ollama to load the model into memory so the first real call is fast."""
        for attempt in range(2):
            try:
                await self._client.post(
                    "/api/generate",
                    json={"model": self.model, "keep_alive": "30m", "prompt": ""},
                    timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
                )
                return  # success
            except Exception:
                if attempt == 0:
                    logger.warning("Model preload attempt 1 failed, retrying…")
                else:
                    logger.warning(
                        "Model preload failed after 2 attempts. "
                        "First request may be slow while model loads."
                    )

    # -- health & discovery ---------------------------------------------------

    async def health_check(self) -> bool:
        """Return ``True`` if the Ollama server is reachable."""
        try:
            resp = await self._client.get("/api/tags")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_models(self) -> list[str]:
        """Return a list of model names available on the server."""
        resp = await self._client.get("/api/tags")
        resp.raise_for_status()
        data = resp.json()
        return [m["name"] for m in data.get("models", [])]

    async def get_model_info(self, model: str | None = None) -> dict[str, Any]:
        """Query the Ollama ``/api/show`` endpoint for model metadata.

        Returns a dict with model details including context length when
        available.  Returns an empty dict on failure.
        """
        try:
            resp = await self._client.post(
                "/api/show",
                json={"name": model or self.model},
            )
            resp.raise_for_status()
            return dict(resp.json())
        except httpx.HTTPError:
            return {}

    async def detect_context_window(self, model: str | None = None) -> int:
        """Auto-detect the model's context window from the Ollama API.

        Falls back to the heuristic :func:`get_model_context_window` when
        the API does not provide the information.  The detected value is
        cached in ``_detected_context_window`` so that every subsequent
        payload automatically includes the correct ``num_ctx``.
        """
        info = await self.get_model_info(model)

        # Ollama exposes model parameters as a text block in `modelfile` or
        # in the `model_info` dict.
        model_info = info.get("model_info", {})
        for key, value in model_info.items():
            if "context_length" in key:
                try:
                    ctx = int(value)
                    self._detected_context_window = ctx
                    return ctx
                except (ValueError, TypeError):
                    pass

        # Try the parameters section
        parameters = info.get("parameters", "")
        if isinstance(parameters, str):
            for line in parameters.splitlines():
                if "num_ctx" in line:
                    parts = line.split()
                    for p in parts:
                        try:
                            ctx = int(p)
                            self._detected_context_window = ctx
                            return ctx
                        except ValueError:
                            continue

        ctx = get_model_context_window(model or self.model)
        self._detected_context_window = ctx
        return ctx

    # -- chat -----------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        temperature: float = 0.1,
        stream: bool = True,
        agent_role: str = "agent",
    ) -> str:
        """Send a chat request and return the full response text.

        Retries up to 5 times on connection errors with exponential backoff + jitter.
        When *stream* is ``True`` a Rich spinner is displayed while tokens arrive.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "options": self._default_options(temperature),
            "keep_alive": "30m",
        }
        if system:
            payload["messages"] = [
                {"role": "system", "content": system}, *messages,
            ]

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                if stream:
                    return await self._chat_stream(payload, agent_role)
                else:
                    return await self._chat_sync(payload)
            except _RETRYABLE_EXCEPTIONS as exc:
                last_error = exc
                if attempt < _MAX_RETRIES - 1:
                    wait = _get_retry_delay(attempt)
                    logger.warning(
                        "Ollama connection error (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1, _MAX_RETRIES, wait, exc,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "Ollama connection error (final attempt %d/%d): %s",
                        attempt + 1, _MAX_RETRIES, exc,
                    )

        raise ConnectionError(
            f"Failed to reach Ollama after {_MAX_RETRIES} attempts: {last_error}"
        ) from last_error

    async def _chat_stream(self, payload: dict[str, Any], agent_role: str) -> str:
        parts: list[str] = []
        if self.stream_to_console:
            # Stream tokens to the terminal in real time
            _console.print(f"[bold cyan]{agent_role}[/bold cyan] ", end="")
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        parts.append(token)
                        _console.print(token, end="", highlight=False)
            _console.print()  # final newline
        else:
            # Spinner-only mode (used during structured/JSON calls)
            with Live(
                Spinner("dots", text=f"[bold cyan]{agent_role}[/] thinking…"),
                refresh_per_second=10,
                transient=True,
            ):
                async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        chunk = json.loads(line)
                        token = chunk.get("message", {}).get("content", "")
                        if token:
                            parts.append(token)
        return "".join(parts)

    async def _chat_sync(self, payload: dict[str, Any]) -> str:
        resp = await self._client.post("/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return str(data.get("message", {}).get("content", ""))

    # -- structured chat ------------------------------------------------------

    async def chat_structured(
        self,
        messages: list[dict[str, str]],
        system: str,
        response_schema: str,
        agent_role: str = "agent",
    ) -> str:
        """Chat expecting valid JSON output matching *response_schema*.

        Retries up to 3 times if the model returns invalid JSON, feeding back
        the parse error each time.
        """
        schema_instruction = (
            "\n\nCRITICAL: Your response MUST be valid JSON matching this schema:\n"
            f"{response_schema}\n"
            "Output ONLY the JSON. No markdown, no explanation."
        )
        full_system = system + schema_instruction

        working_messages = list(messages)
        last_raw = ""

        # Disable streaming output for structured (JSON) calls — use spinner
        prev = self.stream_to_console
        self.stream_to_console = False
        try:
            for attempt in range(3):
                last_raw = await self.chat(
                    working_messages,
                    system=full_system,
                    temperature=0.1,
                    stream=True,
                    agent_role=agent_role,
                )

                # Strip markdown fences the model may add despite instructions.
                cleaned = last_raw.strip()
                if cleaned.startswith("```"):
                    first_nl = cleaned.index("\n") if "\n" in cleaned else 3
                    cleaned = cleaned[first_nl + 1 :]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                cleaned = cleaned.strip()

                try:
                    json.loads(cleaned)
                    return cleaned
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Structured response attempt %d/3 failed to parse: %s", attempt + 1, exc
                    )
                    working_messages = [
                        *messages,
                        {"role": "assistant", "content": last_raw},
                        {
                            "role": "user",
                            "content": (
                                f"Your previous response was NOT valid JSON. "
                                f"Parse error: {exc}. "
                                f"Please output ONLY valid JSON matching the schema."
                            ),
                        },
                    ]
        finally:
            self.stream_to_console = prev

        # Return a valid JSON error object instead of raw string that crashes downstream
        logger.error("Structured response failed after 3 attempts, returning error JSON")
        return json.dumps({
            "error": "Failed to get valid JSON response after 3 attempts",
            "raw_response": last_raw[:500] if last_raw else "(empty)",
        })

    # -- embeddings -----------------------------------------------------------

    async def embed(self, text: str) -> list[float]:
        """Return the embedding vector for *text*, or ``[]`` on failure."""
        try:
            resp = await self._client.post(
                "/api/embeddings",
                json={"model": self.model, "prompt": text},
            )
            resp.raise_for_status()
            return list(resp.json().get("embedding", []))
        except httpx.HTTPError:
            return []

    # -- raw token iterator (for chat REPL) --------------------------------

    async def chat_stream_tokens(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        temperature: float = 0.4,
    ) -> AsyncIterator[str]:
        """Yield response tokens one at a time without displaying them.

        This is used by the interactive chat REPL which manages its own
        rendering.  Retries up to 5 times on timeout/connection errors.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": self._default_options(temperature),
            "keep_alive": "30m",
        }
        if system:
            payload["messages"] = [
                {"role": "system", "content": system}, *messages,
            ]

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            # Some servers emit occasional non-JSON keep-alives.
                            continue
                        token = chunk.get("message", {}).get("content", "")
                        if token:
                            yield token
                return  # success
            except _RETRYABLE_EXCEPTIONS as exc:
                last_error = exc
                if attempt < _MAX_RETRIES - 1:
                    wait = _get_retry_delay(attempt)
                    logger.debug(
                        "Stream connection error (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1, _MAX_RETRIES, wait, exc,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "Stream connection error (final attempt %d/%d): %s",
                        attempt + 1, _MAX_RETRIES, exc,
                    )
        if last_error:
            raise ConnectionError(
                f"Stream failed after {_MAX_RETRIES} retries: {last_error}"
            ) from last_error

    # -- native tool calling stream ----------------------------------------

    async def chat_with_tools_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        temperature: float = 0.4,
        tool_calls_out: list[dict[str, Any]] | None = None,
        num_predict: int | None = None,
    ) -> AsyncIterator[str]:
        """Stream tokens with native Ollama tool calling support.

        Yields content tokens as strings.  Any tool calls returned by the
        model are appended to *tool_calls_out* (a mutable list the caller
        provides).  After iteration, check ``tool_calls_out`` for actions.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "stream": True,
            "tools": tools,
            "options": self._default_options(temperature, num_predict=num_predict),
            "keep_alive": "30m",
        }
        if system:
            payload["messages"] = [
                {"role": "system", "content": system},
                *payload["messages"],
            ]

        if tool_calls_out is None:
            tool_calls_out = []

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with self._client.stream(
                    "POST", "/api/chat", json=payload,
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg = chunk.get("message", {})

                        # Capture native tool calls
                        calls = msg.get("tool_calls")
                        if calls:
                            tool_calls_out.extend(calls)

                        # Yield content tokens
                        token = msg.get("content", "")
                        if token:
                            yield token
                return  # success
            except _RETRYABLE_EXCEPTIONS as exc:
                last_error = exc
                if attempt < _MAX_RETRIES - 1:
                    wait = _get_retry_delay(attempt)
                    logger.debug(
                        "Tool stream connection error (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1, _MAX_RETRIES, wait, exc,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "Tool stream connection error (final attempt %d/%d): %s",
                        attempt + 1, _MAX_RETRIES, exc,
                    )
        if last_error:
            raise ConnectionError(
                f"Tool stream failed after {_MAX_RETRIES} retries: {last_error}"
            ) from last_error
