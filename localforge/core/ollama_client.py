"""Async Ollama HTTP client for localforge."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
from rich.live import Live
from rich.spinner import Spinner

from localforge.core.config import LocalForgeConfig

logger = logging.getLogger(__name__)


def get_model_context_window(model_name: str) -> int:
    """Estimate context window size from the model name."""
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
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

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

    # -- chat -----------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict],
        system: str | None = None,
        temperature: float = 0.1,
        stream: bool = True,
        agent_role: str = "agent",
    ) -> str:
        """Send a chat request and return the full response text.

        Retries up to 3 times on connection errors with exponential backoff.
        When *stream* is ``True`` a Rich spinner is displayed while tokens
        arrive.
        """
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "options": {"temperature": temperature},
        }
        if system:
            payload["messages"] = [{"role": "system", "content": system}, *messages]

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                if stream:
                    return await self._chat_stream(payload, agent_role)
                else:
                    return await self._chat_sync(payload)
            except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadTimeout) as exc:
                last_error = exc
                wait = 2 ** attempt
                logger.warning("Ollama connection error (attempt %d/3): %s", attempt + 1, exc)
                await asyncio.sleep(wait)

        raise ConnectionError(
            f"Failed to reach Ollama after 3 attempts: {last_error}"
        ) from last_error

    async def _chat_stream(self, payload: dict, agent_role: str) -> str:
        parts: list[str] = []
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

    async def _chat_sync(self, payload: dict) -> str:
        resp = await self._client.post("/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("message", {}).get("content", "")

    # -- structured chat ------------------------------------------------------

    async def chat_structured(
        self,
        messages: list[dict],
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

        # Return whatever we got on the last attempt.
        return last_raw

    # -- embeddings -----------------------------------------------------------

    async def embed(self, text: str) -> list[float]:
        """Return the embedding vector for *text*, or ``[]`` on failure."""
        try:
            resp = await self._client.post(
                "/api/embeddings",
                json={"model": self.model, "prompt": text},
            )
            resp.raise_for_status()
            return resp.json().get("embedding", [])
        except httpx.HTTPError:
            return []
