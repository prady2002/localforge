"""Async HTTP client for the cloud API.

Implements the same public interface as ``OllamaClient`` so the chat engine
can swap between local and cloud backends transparently.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import ssl
from collections.abc import AsyncIterator
from typing import Any

import httpx
from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner

from localforge.cloud.exceptions import (
    APIError,
    AuthExpiredError,
    RateLimitError,
    VPNError,
)

logger = logging.getLogger(__name__)
_console = Console()

# Retry tunables
_MAX_RETRIES = 5
_BASE_RETRY_DELAY = 1.5
_MAX_RETRY_DELAY = 30.0

_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.CloseError,
    TimeoutError,
    ConnectionError,
)

# Marker prefix for thinking tokens so the engine can render them differently.
THINKING_TOKEN_PREFIX = "\x00THINK:"

# Cloud model fixed properties
CLOUD_MODEL_NAME = "gemini-3.1-pro-preview"
CLOUD_CONTEXT_WINDOW = 131072  # 128K tokens


def _retry_delay(attempt: int) -> float:
    delay = min(_BASE_RETRY_DELAY * (2 ** attempt), _MAX_RETRY_DELAY)
    jitter = delay * 0.2 * (2 * random.random() - 1)
    return max(0.5, delay + jitter)


def _dns_retry_delay(attempt: int) -> float:
    """Short retry delay for DNS/getaddrinfo errors.

    DNS blips on Windows corporate VPN typically resolve in <1 s,
    so we use much shorter delays than the general retry path.
    """
    delay = min(0.5 * (2 ** attempt), 5.0)  # 0.5, 1, 2, 4, 5
    jitter = delay * 0.15 * (2 * random.random() - 1)
    return max(0.3, delay + jitter)


def _is_dns_error(exc: BaseException) -> bool:
    """Return True if *exc* looks like a transient DNS resolution failure."""
    detail = str(exc).lower()
    return "getaddrinfo" in detail or "name or service not known" in detail or "nodename nor servname" in detail


# ---------------------------------------------------------------------------
# Incremental JSON stream parser
# ---------------------------------------------------------------------------


def _split_concatenated_json(text: str) -> list[str]:
    """Split a string of concatenated JSON objects into individual JSON strings.

    Some cloud APIs return responses as concatenated JSON: ``{...}{...}{...}``
    without any delimiter.  We track brace depth to find object boundaries.
    """
    results: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escape_next = False

    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            if in_string:
                escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                results.append(text[start : i + 1])
                start = -1

    return results


def _parse_stream_chunks(raw_text: str) -> list[dict[str, Any]]:
    """Parse concatenated JSON response into a list of chunk dicts."""
    json_strings = _split_concatenated_json(raw_text)
    chunks: list[dict[str, Any]] = []
    for s in json_strings:
        try:
            chunks.append(json.loads(s))
        except json.JSONDecodeError:
            logger.debug("Failed to parse JSON chunk: %s", s[:200])
    return chunks


# ---------------------------------------------------------------------------
# CloudClient
# ---------------------------------------------------------------------------


class CloudClient:
    """Async HTTP client for the cloud digital-assistant API.

    Drop-in replacement for ``OllamaClient`` — provides the same public
    methods so ``CloudChatEngine`` can use it interchangeably.
    """

    def __init__(self, auth_data: dict[str, Any]) -> None:
        self.base_url: str = auth_data["base_url"]
        self.api_path: str = auth_data["api_path"]
        self.model = CLOUD_MODEL_NAME

        # Build headers from parsed auth data
        self._headers = dict(auth_data.get("headers", {}))
        # Ensure critical defaults
        self._headers.setdefault("Accept", "*/*")
        self._headers.setdefault("content-type", "application/json")
        self._headers.setdefault("Cache-Control", "no-cache")
        self._headers.setdefault("Pragma", "no-cache")

        # Construct the full API URL
        self._api_url = f"{self.base_url}{self.api_path}"

        # Use the OS-native certificate store (Windows, macOS) so that
        # corporate / VPN-injected CA certificates are trusted.  Without
        # this, httpx falls back to certifi which doesn't include them,
        # causing SSL: CERTIFICATE_VERIFY_FAILED on corporate networks.
        self._ssl_context = ssl.create_default_context()

        self._client = self._new_httpx_client()

        # Conversation state
        self.conversation_id: str = ""
        self._api_messages: list[dict[str, Any]] = []

        # Capability flags (matching OllamaClient interface)
        self.supports_tools: bool = False  # we use XML fallback
        self.supports_json_mode: bool = True
        self.model_family: str = "gemini"
        self._detected_context_window: int = CLOUD_CONTEXT_WINDOW

        # Console streaming control
        self.stream_to_console: bool = True

    def _new_httpx_client(self) -> httpx.AsyncClient:
        """Create a fresh ``httpx.AsyncClient`` with current settings.

        Uses ``max_keepalive_connections=0`` to disable connection pooling.
        On Windows ``ProactorEventLoop`` with corporate SSL inspection,
        reusing pooled connections causes spurious ``getaddrinfo`` failures
        on the second request.  Disabling keep-alive forces a fresh TCP+TLS
        handshake per request — slightly slower but 100 % reliable.
        """
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=0,  # no keep-alive → no stale pool
            ),
            follow_redirects=True,
            verify=self._ssl_context,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await self._client.aclose()

    async def preload_model(self) -> None:
        """No-op — cloud model is always loaded."""

    # ------------------------------------------------------------------
    # Health & discovery
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Verify API connectivity and auth validity.

        Retries up to 3 times on transient connection / DNS errors.
        Raises domain-specific exceptions on failure so callers can
        present clear messages to the user.
        """
        _HC_ATTEMPTS = 5
        last_exc: Exception | None = None
        dns_failures = 0

        for attempt in range(_HC_ATTEMPTS):
            try:
                # Use empty conversation_id for health check so stale
                # server-side conversation state cannot cause a false 401.
                payload = self._build_payload("ping", include_history=False)
                payload["conversation_id"] = ""

                resp = await self._client.post(
                    self.api_path,
                    json=payload,
                    timeout=httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=15.0),
                )

                if resp.status_code in (401, 403):
                    raise AuthExpiredError()
                if resp.status_code == 429:
                    raise RateLimitError()
                if resp.status_code >= 500:
                    if attempt < _HC_ATTEMPTS - 1:
                        logger.debug(
                            "Health check server error %d (attempt %d/%d), retrying…",
                            resp.status_code, attempt + 1, _HC_ATTEMPTS,
                        )
                        with contextlib.suppress(Exception):
                            await self._client.aclose()
                        self._client = self._new_httpx_client()
                        await asyncio.sleep(_dns_retry_delay(attempt))
                        continue
                    raise APIError(resp.status_code, "Server error during health check.")

                if dns_failures:
                    logger.info("Health check succeeded after %d DNS retries.", dns_failures)
                # Any 2xx is success
                return 200 <= resp.status_code < 400

            except (AuthExpiredError, RateLimitError, APIError):
                raise
            except (httpx.ConnectError, httpx.ConnectTimeout, OSError) as exc:
                last_exc = exc
                detail = str(exc)
                # SSL errors won't be fixed by a retry
                if "CERTIFICATE_VERIFY_FAILED" in detail or "SSL" in detail:
                    raise VPNError(
                        f"SSL certificate verification failed for {self.base_url}.\n"
                        "  Your corporate network likely uses SSL inspection.\n"
                        "  Detail: " + detail
                    ) from exc
                if attempt < _HC_ATTEMPTS - 1:
                    if _is_dns_error(exc):
                        dns_failures += 1
                        logger.debug(
                            "Health check DNS error (attempt %d/%d), retrying…",
                            attempt + 1, _HC_ATTEMPTS,
                        )
                    else:
                        logger.debug(
                            "Health check connection error (attempt %d/%d): %s",
                            attempt + 1, _HC_ATTEMPTS, exc,
                        )
                    with contextlib.suppress(Exception):
                        await self._client.aclose()
                    self._client = self._new_httpx_client()
                    await asyncio.sleep(_dns_retry_delay(attempt))
                    continue
                raise VPNError(
                    f"Cannot reach {self.base_url} after {_HC_ATTEMPTS} attempts. "
                    "Make sure you are connected to the required VPN or network.\n"
                    f"  Detail: {detail}"
                ) from exc
            except VPNError:
                raise
            except _RETRYABLE_EXCEPTIONS as exc:
                last_exc = exc
                if attempt < _HC_ATTEMPTS - 1:
                    logger.debug(
                        "Health check transient error (attempt %d/%d): %s",
                        attempt + 1, _HC_ATTEMPTS, exc,
                    )
                    with contextlib.suppress(Exception):
                        await self._client.aclose()
                    self._client = self._new_httpx_client()
                    await asyncio.sleep(_retry_delay(attempt))
                    continue
                raise APIError(0, f"Unexpected HTTP error: {exc}") from exc
            except httpx.HTTPError as exc:
                raise APIError(0, f"Unexpected HTTP error: {exc}") from exc

        # Should not reach here
        raise VPNError(
            f"Health check failed after {_HC_ATTEMPTS} attempts: {last_exc}"
        )

    async def list_models(self) -> list[str]:
        return [CLOUD_MODEL_NAME]

    async def detect_context_window(self, model: str | None = None) -> int:
        return CLOUD_CONTEXT_WINDOW

    async def detect_capabilities(self) -> dict[str, Any]:
        return {
            "model": CLOUD_MODEL_NAME,
            "family": "gemini",
            "supports_tools": False,
            "supports_json_mode": True,
            "context_window": CLOUD_CONTEXT_WINDOW,
        }

    # ------------------------------------------------------------------
    # Payload construction
    # ------------------------------------------------------------------

    @staticmethod
    def _user_message_metadata(temperature: float = 1.0) -> dict[str, Any]:
        return {
            "model_value": CLOUD_MODEL_NAME,
            "language": "EN",
            "temperature": temperature,
            "top_p": 0.8,
            "output_limit": 32768,
            "thinking": True,
            "web_search": False,
            "self_serve_rag": False,
        }

    def _build_payload(
        self,
        user_content: str,
        *,
        include_history: bool = True,
        temperature: float = 1.0,
    ) -> dict[str, Any]:
        """Build the full API payload with conversation history."""
        messages: list[dict[str, Any]] = []

        if include_history:
            messages.extend(self._api_messages)

        # Append the new user message
        messages.append({
            "role": "user",
            "content": user_content,
            "documents": [],
            "message_metadata": self._user_message_metadata(temperature),
        })

        return {
            "messages": messages,
            "conversation_id": self.conversation_id,
            "system_purpose": "code",
        }

    def _build_payload_from_messages(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        temperature: float = 1.0,
    ) -> dict[str, Any]:
        """Convert localforge-style messages to cloud API format.

        ``messages`` is a list of dicts with ``role`` (user/assistant/system),
        ``content``, and optionally ``thinking`` keys.
        ``system`` is an optional system prompt prepended to the first user
        message (the API doesn't have a native system role — we fold it into
        the first user message's content).
        """
        api_messages: list[dict[str, Any]] = []
        system_injected = False

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role in ("user", "human"):
                # Inject system prompt into the first user message
                if system and not system_injected:
                    content = f"[SYSTEM INSTRUCTIONS — follow these at all times]\n{system}\n[END SYSTEM INSTRUCTIONS]\n\n{content}"
                    system_injected = True
                api_messages.append({
                    "role": "user",
                    "content": content,
                    "documents": [],
                    "message_metadata": self._user_message_metadata(temperature),
                })
            elif role in ("assistant", "system"):
                api_messages.append({
                    "role": "system",
                    "content": content,
                    "documents": None,
                    "message_metadata": {
                        "model_value": CLOUD_MODEL_NAME,
                    },
                    "thinking": msg.get("thinking", "") or "",
                    "web_search_response": [],
                    "image_generation_limit": None,
                })
            # Skip 'tool' role messages — their results are folded inline

        # If system was provided but no user message existed yet, add a wrapper
        if system and not system_injected:
            api_messages.insert(0, {
                "role": "user",
                "content": f"[SYSTEM INSTRUCTIONS]\n{system}\n[END SYSTEM INSTRUCTIONS]",
                "documents": [],
                "message_metadata": self._user_message_metadata(temperature),
            })

        return {
            "messages": api_messages,
            "conversation_id": self.conversation_id,
            "system_purpose": "code",
        }

    # ------------------------------------------------------------------
    # Response processing
    # ------------------------------------------------------------------

    def _process_response_chunks(
        self,
        chunks: list[dict[str, Any]],
    ) -> tuple[str, str, str]:
        """Extract content, thinking, and conversation_id from parsed chunks.

        Returns (full_content, full_thinking, conversation_id).
        """
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        conv_id = self.conversation_id

        for chunk in chunks:
            resp = chunk.get("model_response", {})
            c = resp.get("content", "") or ""
            t = resp.get("thinking", "") or ""
            if c:
                content_parts.append(c)
            if t:
                thinking_parts.append(t)

            cid = chunk.get("conversation_id", "")
            if cid:
                conv_id = cid

            error = chunk.get("error")
            if error:
                logger.warning("API returned error in chunk: %s", error)

        return "".join(content_parts), "".join(thinking_parts), conv_id

    def _record_exchange(
        self,
        user_content: str,
        assistant_content: str,
        thinking: str,
        temperature: float = 1.0,
    ) -> None:
        """Append an exchange to the internal API message history."""
        self._api_messages.append({
            "role": "user",
            "content": user_content,
            "documents": [],
            "message_metadata": self._user_message_metadata(temperature),
        })
        self._api_messages.append({
            "role": "system",
            "content": assistant_content,
            "documents": None,
            "message_metadata": {
                "model_value": CLOUD_MODEL_NAME,
            },
            "thinking": thinking,
        })

    # ------------------------------------------------------------------
    # Core streaming
    # ------------------------------------------------------------------

    async def _make_request(
        self,
        payload: dict[str, Any],
    ) -> str:
        """POST to the API and return the full response text.

        Includes retry logic with httpx client recreation to handle
        Windows ``ProactorEventLoop`` connection-pool failures where
        the second request through a pooled connection gets a spurious
        ``getaddrinfo`` error.

        Raises ``AuthExpiredError`` or ``VPNError`` on recognised failures.
        """
        last_exc: Exception | None = None
        session_reset_attempted = False

        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.post(
                    self.api_path,
                    json=payload,
                )

                resp_text = resp.text or ""

                # Conversation IDs can expire server-side while auth cookies are still
                # valid. Reset local conversation state and retry once before forcing
                # the user through re-auth.
                if resp.status_code in (400, 401, 403):
                    has_conversation_state = bool(
                        payload.get("conversation_id")
                        or self.conversation_id
                        or self._api_messages
                    )
                    looks_like_bad_conversation = False
                    if resp.status_code == 400:
                        lower_body = resp_text.lower()
                        looks_like_bad_conversation = (
                            "conversation" in lower_body
                            and any(
                                marker in lower_body
                                for marker in (
                                    "not found",
                                    "invalid",
                                    "expired",
                                    "unknown",
                                    "does not exist",
                                )
                            )
                        )

                    if (
                        has_conversation_state
                        and not session_reset_attempted
                        and (resp.status_code in (401, 403) or looks_like_bad_conversation)
                    ):
                        logger.warning(
                            "HTTP %d with existing conversation state; resetting conversation and retrying once.",
                            resp.status_code,
                        )
                        self.reset_conversation()
                        payload["conversation_id"] = ""
                        session_reset_attempted = True

                        with contextlib.suppress(Exception):
                            await self._client.aclose()
                        self._client = self._new_httpx_client()
                        continue

                if resp.status_code in (401, 403):
                    raise AuthExpiredError()
                if resp.status_code == 400:
                    raise APIError(resp.status_code, resp_text[:500] or "Bad request.")
                if resp.status_code == 429:
                    # Rate limited — retry with longer backoff
                    if attempt < _MAX_RETRIES - 1:
                        retry_after = float(resp.headers.get("Retry-After", _retry_delay(attempt) * 2))
                        logger.warning(
                            "Rate limited (attempt %d/%d), waiting %.1fs…",
                            attempt + 1, _MAX_RETRIES, retry_after,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    raise RateLimitError()
                if resp.status_code >= 500:
                    # Server errors are retryable
                    if attempt < _MAX_RETRIES - 1:
                        logger.warning(
                            "Server error %d (attempt %d/%d), retrying…",
                            resp.status_code, attempt + 1, _MAX_RETRIES,
                        )
                        with contextlib.suppress(Exception):
                            await self._client.aclose()
                        self._client = self._new_httpx_client()
                        await asyncio.sleep(_retry_delay(attempt))
                        continue
                    raise APIError(resp.status_code, resp_text[:500])
                resp.raise_for_status()

                return resp_text

            except (AuthExpiredError, RateLimitError):
                raise
            except APIError:
                raise
            except _RETRYABLE_EXCEPTIONS as exc:
                last_exc = exc
                detail = str(exc)

                # SSL errors won't be fixed by a retry
                if "CERTIFICATE_VERIFY_FAILED" in detail or "SSL" in detail:
                    raise VPNError(
                        f"SSL certificate verification failed for {self.base_url}.\n"
                        "  Your corporate network likely uses SSL inspection.\n"
                        f"  Detail: {detail}"
                    ) from exc

                if attempt < _MAX_RETRIES - 1:
                    is_dns = _is_dns_error(exc)
                    # DNS blips are common on Windows corporate VPN — log
                    # at debug level and use a fast retry cadence.
                    if is_dns:
                        logger.debug(
                            "DNS error (attempt %d/%d), recreating client",
                            attempt + 1, _MAX_RETRIES,
                        )
                    else:
                        logger.warning(
                            "Connection error (attempt %d/%d), recreating client: %s",
                            attempt + 1, _MAX_RETRIES, exc,
                        )
                    # Recreate httpx client to get a fresh connection pool
                    with contextlib.suppress(Exception):
                        await self._client.aclose()
                    self._client = self._new_httpx_client()
                    await asyncio.sleep(_dns_retry_delay(attempt) if is_dns else _retry_delay(attempt))
                    continue

                raise VPNError(
                    f"Cannot reach {self.base_url} after {_MAX_RETRIES} attempts. "
                    f"Check your VPN.\n  Detail: {detail}"
                ) from exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 403):
                    raise AuthExpiredError() from exc
                if exc.response.status_code >= 500 and attempt < _MAX_RETRIES - 1:
                    logger.warning(
                        "HTTP %d (attempt %d/%d), retrying…",
                        exc.response.status_code, attempt + 1, _MAX_RETRIES,
                    )
                    with contextlib.suppress(Exception):
                        await self._client.aclose()
                    self._client = self._new_httpx_client()
                    await asyncio.sleep(_retry_delay(attempt))
                    continue
                raise APIError(exc.response.status_code, str(exc)) from exc

        # Should not reach here, but just in case
        raise VPNError(
            f"Request failed after {_MAX_RETRIES} attempts: {last_exc}"
        )

    # ------------------------------------------------------------------
    # Public chat interface (matches OllamaClient)
    # ------------------------------------------------------------------

    async def chat_stream_tokens(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        temperature: float = 0.4,
    ) -> AsyncIterator[str]:
        """Yield response tokens one at a time.

        Streams thinking tokens (prefixed with ``THINKING_TOKEN_PREFIX``)
        followed by content tokens.  The caller decides how to render them.
        """
        payload = self._build_payload_from_messages(messages, system, temperature)

        raw_text = await self._make_request(payload)

        if not raw_text:
            return

        parsed = _parse_stream_chunks(raw_text)

        for chunk in parsed:
            resp = chunk.get("model_response", {})

            thinking = resp.get("thinking", "") or ""
            if thinking:
                yield THINKING_TOKEN_PREFIX + thinking

            content = resp.get("content", "") or ""
            if content:
                yield content

            cid = chunk.get("conversation_id", "")
            if cid:
                self.conversation_id = cid

    async def chat_with_tools_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        temperature: float = 0.4,
        tool_calls_out: list[dict[str, Any]] | None = None,
        num_predict: int | None = None,
    ) -> AsyncIterator[str]:
        """Stream tokens with tool-calling support (XML-style fallback).

        The cloud API doesn't support native tool calling, so tool schemas
        are embedded in the system prompt and the model uses ``<tool_call>``
        XML blocks.  ``tool_calls_out`` is unused here (tool calls are
        parsed from the text by the engine).
        """
        # tools & tool_calls_out are accepted for interface compatibility
        # but the cloud model uses XML tool calling via the system prompt
        async for token in self.chat_stream_tokens(messages, system, temperature):
            yield token

    async def chat(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        temperature: float = 0.1,
        stream: bool = True,
        agent_role: str = "agent",
    ) -> str:
        """Send a chat request and collect the full response.

        When ``stream`` is True, tokens are displayed in the console.
        """
        parts: list[str] = []

        if stream and self.stream_to_console:
            _console.print(f"[bold cyan]{agent_role}[/bold cyan] ", end="")
            async for token in self.chat_stream_tokens(messages, system, temperature):
                if token.startswith(THINKING_TOKEN_PREFIX):
                    continue  # skip thinking in non-interactive mode
                parts.append(token)
                _console.print(token, end="", highlight=False)
            _console.print()
        elif stream:
            with Live(
                Spinner("dots", text=f"[bold cyan]{agent_role}[/] thinking…"),
                refresh_per_second=10,
                transient=True,
            ):
                async for token in self.chat_stream_tokens(messages, system, temperature):
                    if token.startswith(THINKING_TOKEN_PREFIX):
                        continue
                    parts.append(token)
        else:
            async for token in self.chat_stream_tokens(messages, system, temperature):
                if token.startswith(THINKING_TOKEN_PREFIX):
                    continue
                parts.append(token)

        return "".join(parts)

    async def chat_structured(
        self,
        messages: list[dict[str, str]],
        system: str,
        response_schema: str,
        agent_role: str = "agent",
    ) -> str:
        """Chat expecting valid JSON matching *response_schema*.

        Retries up to 3 times on invalid JSON.
        """
        schema_instruction = (
            "\n\nCRITICAL: Your response MUST be valid JSON matching this schema:\n"
            f"{response_schema}\n"
            "Output ONLY the JSON. No markdown, no explanation."
        )
        full_system = system + schema_instruction

        working_messages = list(messages)
        last_raw = ""

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

                cleaned = last_raw.strip()
                if cleaned.startswith("```"):
                    first_nl = cleaned.index("\n") if "\n" in cleaned else 3
                    cleaned = cleaned[first_nl + 1:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                cleaned = cleaned.strip()

                try:
                    json.loads(cleaned)
                    return cleaned
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Structured response attempt %d/3 failed: %s",
                        attempt + 1, exc,
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

        logger.error("Structured response failed after 3 attempts")
        return json.dumps({
            "error": "Failed to get valid JSON after 3 attempts",
            "raw_response": last_raw[:500] if last_raw else "(empty)",
        })

    # ------------------------------------------------------------------
    # Conversation management
    # ------------------------------------------------------------------

    def reset_conversation(self) -> None:
        """Clear conversation state for a fresh chat."""
        self.conversation_id = ""
        self._api_messages.clear()
