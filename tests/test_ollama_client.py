"""Tests for localforge.core.ollama_client — OllamaClient with mocked httpx."""

from __future__ import annotations

import json

import httpx
import pytest

from localforge.core.config import LocalForgeConfig
from localforge.core.ollama_client import OllamaClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(mock_config: LocalForgeConfig) -> OllamaClient:
    return OllamaClient(mock_config)


# ---------------------------------------------------------------------------
# test_health_check_success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_success(client: OllamaClient, mocker) -> None:
    """health_check returns True when the server responds with 200."""
    mock_resp = mocker.MagicMock()
    mock_resp.status_code = 200

    mocker.patch.object(client._client, "get", return_value=mock_resp)

    assert await client.health_check() is True


# ---------------------------------------------------------------------------
# test_health_check_failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_failure(client: OllamaClient, mocker) -> None:
    """health_check returns False when the server is unreachable."""
    mocker.patch.object(
        client._client, "get", side_effect=httpx.ConnectError("refused"),
    )

    assert await client.health_check() is False


# ---------------------------------------------------------------------------
# test_chat_returns_content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_returns_content(client: OllamaClient, mocker) -> None:
    """chat() with stream=False should return the message content."""
    mock_resp = mocker.MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "message": {"content": "Hello, world!"},
    }
    mock_resp.raise_for_status = mocker.MagicMock()

    mocker.patch.object(client._client, "post", return_value=mock_resp)

    result = await client.chat(
        [{"role": "user", "content": "Hi"}],
        stream=False,
    )
    assert result == "Hello, world!"


# ---------------------------------------------------------------------------
# test_chat_structured_retries_on_invalid_json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_structured_retries_on_invalid_json(
    client: OllamaClient, mocker,
) -> None:
    """chat_structured should retry when the LLM returns invalid JSON."""
    valid_json = '{"answer": 42}'

    call_count = 0

    async def fake_chat(messages, system=None, temperature=0.1, stream=True, agent_role="agent"):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "not valid json {{"
        return valid_json

    mocker.patch.object(client, "chat", side_effect=fake_chat)

    result = await client.chat_structured(
        [{"role": "user", "content": "test"}],
        system="You are helpful.",
        response_schema='{"type": "object"}',
    )

    parsed = json.loads(result)
    assert parsed["answer"] == 42
    assert call_count == 2  # first failed, second succeeded


# ---------------------------------------------------------------------------
# test_chat_retry_on_connection_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_retry_on_connection_error(
    client: OllamaClient, mocker,
) -> None:
    """chat() should retry up to 5 times on connection errors, then raise."""
    mocker.patch.object(
        client, "_chat_stream",
        side_effect=httpx.ConnectError("refused"),
    )
    mocker.patch("localforge.core.ollama_client.asyncio.sleep", return_value=None)

    with pytest.raises(ConnectionError, match="Failed to reach Ollama"):
        await client.chat(
            [{"role": "user", "content": "test"}],
            stream=True,
        )

    # _chat_stream should have been called 5 times (max retries)
    assert client._chat_stream.call_count == 5


# ---------------------------------------------------------------------------
# test_chat_structured_strips_markdown_fences
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_structured_strips_markdown_fences(
    client: OllamaClient, mocker,
) -> None:
    """chat_structured should strip markdown fences from LLM output."""

    async def fake_chat(*args, **kwargs):
        return '```json\n{"key": "value"}\n```'

    mocker.patch.object(client, "chat", side_effect=fake_chat)

    result = await client.chat_structured(
        [{"role": "user", "content": "test"}],
        system="sys",
        response_schema="{}",
    )
    parsed = json.loads(result)
    assert parsed["key"] == "value"
