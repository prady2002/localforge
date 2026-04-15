"""Resilience tests for cloud auth/session recovery behavior."""

from __future__ import annotations

import pytest

from localforge.cloud.client import CloudClient, _is_dns_error, _dns_retry_delay
from localforge.cloud.engine import CloudChatEngine
from localforge.cloud.exceptions import AuthExpiredError, VPNError
from localforge.core.config import LocalForgeConfig


@pytest.fixture()
def cloud_client() -> CloudClient:
    """Return a CloudClient configured with dummy auth headers."""
    auth = {
        "base_url": "https://example.com",
        "api_path": "/api/messages",
        "headers": {"Cookie": "session=abc; token=xyz"},
    }
    return CloudClient(auth)


# -----------------------------------------------------------------------
# CloudClient._make_request — stale conversation recovery
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_make_request_recovers_from_stale_conversation(
    cloud_client: CloudClient,
    mocker,
) -> None:
    """A 401 with stale conversation state should reset and retry once."""
    resp_401 = mocker.MagicMock(status_code=401, text="Unauthorized", headers={})
    resp_200 = mocker.MagicMock(status_code=200, text="ok", headers={})
    resp_200.raise_for_status = mocker.MagicMock()

    mock_http = mocker.MagicMock()
    mock_http.post = mocker.AsyncMock(side_effect=[resp_401, resp_200])
    mock_http.aclose = mocker.AsyncMock()

    cloud_client._client = mock_http
    mocker.patch.object(cloud_client, "_new_httpx_client", return_value=mock_http)

    cloud_client.conversation_id = "stale-conversation"
    cloud_client._api_messages = [{"role": "user", "content": "hi"}]

    payload = {
        "messages": [{"role": "user", "content": "hello"}],
        "conversation_id": "stale-conversation",
        "system_purpose": "code",
    }

    result = await cloud_client._make_request(payload)

    assert result == "ok"
    assert mock_http.post.await_count == 2
    assert payload["conversation_id"] == ""
    assert cloud_client.conversation_id == ""
    assert cloud_client._api_messages == []


@pytest.mark.asyncio
async def test_make_request_raises_auth_error_without_conversation_state(
    cloud_client: CloudClient,
    mocker,
) -> None:
    """A 401 without conversation state should require re-auth immediately."""
    resp_401 = mocker.MagicMock(status_code=401, text="Unauthorized", headers={})

    mock_http = mocker.MagicMock()
    mock_http.post = mocker.AsyncMock(return_value=resp_401)
    mock_http.aclose = mocker.AsyncMock()

    cloud_client._client = mock_http
    cloud_client.conversation_id = ""
    cloud_client._api_messages = []

    payload = {
        "messages": [{"role": "user", "content": "hello"}],
        "conversation_id": "",
        "system_purpose": "code",
    }

    with pytest.raises(AuthExpiredError):
        await cloud_client._make_request(payload)

    assert mock_http.post.await_count == 1


# -----------------------------------------------------------------------
# CloudClient.health_check — retries on transient DNS failures
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_retries_on_dns_failure(
    cloud_client: CloudClient,
    mocker,
) -> None:
    """health_check should retry on transient DNS/connection errors."""
    import httpx

    resp_200 = mocker.MagicMock(status_code=200, text="ok", headers={})
    resp_200.raise_for_status = mocker.MagicMock()

    # Fail 4 times out of 5 allowed, succeed on 5th
    mock_http = mocker.MagicMock()
    mock_http.post = mocker.AsyncMock(
        side_effect=[
            httpx.ConnectError("[Errno 11001] getaddrinfo failed"),
            httpx.ConnectError("[Errno 11001] getaddrinfo failed"),
            httpx.ConnectError("[Errno 11001] getaddrinfo failed"),
            httpx.ConnectError("[Errno 11001] getaddrinfo failed"),
            resp_200,
        ]
    )
    mock_http.aclose = mocker.AsyncMock()

    cloud_client._client = mock_http
    mocker.patch.object(cloud_client, "_new_httpx_client", return_value=mock_http)
    mocker.patch("localforge.cloud.client.asyncio.sleep", return_value=None)

    result = await cloud_client.health_check()
    assert result is True
    assert mock_http.post.await_count == 5


@pytest.mark.asyncio
async def test_health_check_raises_vpn_after_all_retries(
    cloud_client: CloudClient,
    mocker,
) -> None:
    """health_check should raise VPNError after exhausting retries."""
    import httpx

    mock_http = mocker.MagicMock()
    mock_http.post = mocker.AsyncMock(
        side_effect=httpx.ConnectError("[Errno 11001] getaddrinfo failed"),
    )
    mock_http.aclose = mocker.AsyncMock()

    cloud_client._client = mock_http
    mocker.patch.object(cloud_client, "_new_httpx_client", return_value=mock_http)
    mocker.patch("localforge.cloud.client.asyncio.sleep", return_value=None)

    with pytest.raises(VPNError, match="after 5 attempts"):
        await cloud_client.health_check()

    assert mock_http.post.await_count == 5


def test_is_dns_error_detects_getaddrinfo():
    assert _is_dns_error(OSError("[Errno 11001] getaddrinfo failed"))
    assert _is_dns_error(Exception("Name or service not known"))
    assert not _is_dns_error(Exception("Connection refused"))
    assert not _is_dns_error(Exception("SSL certificate error"))


def test_dns_retry_delay_is_short():
    d0 = _dns_retry_delay(0)
    d1 = _dns_retry_delay(1)
    assert 0.3 <= d0 <= 1.0, f"First DNS retry delay too long: {d0}"
    assert d1 <= 2.0, f"Second DNS retry delay too long: {d1}"


@pytest.mark.asyncio
async def test_health_check_uses_empty_conversation_id(
    cloud_client: CloudClient,
    mocker,
) -> None:
    """health_check should send empty conversation_id to avoid stale state."""
    cloud_client.conversation_id = "old-stale-id"

    resp_200 = mocker.MagicMock(status_code=200, text="ok", headers={})
    resp_200.raise_for_status = mocker.MagicMock()

    mock_http = mocker.MagicMock()
    mock_http.post = mocker.AsyncMock(return_value=resp_200)

    cloud_client._client = mock_http

    await cloud_client.health_check()

    # Verify the payload had empty conversation_id
    call_args = mock_http.post.call_args
    payload = call_args.kwargs.get("json") or call_args[1].get("json")
    assert payload["conversation_id"] == ""


# -----------------------------------------------------------------------
# CloudChatEngine — session state recovery helpers
# -----------------------------------------------------------------------


class _DummyClient:
    def __init__(self) -> None:
        self.model = "dummy"
        self.conversation_id = "abc"
        self._api_messages = [{"role": "user", "content": "hello"}]
        self.reset_calls = 0

    def reset_conversation(self) -> None:
        self.reset_calls += 1
        self.conversation_id = ""
        self._api_messages = []


@pytest.mark.asyncio
async def test_engine_recover_from_auth_error_resets_remote_state(
    tmp_path,
    mock_config: LocalForgeConfig,
) -> None:
    """Engine should reset remote state once before forcing re-auth."""
    client = _DummyClient()
    engine = CloudChatEngine(mock_config, client, tmp_path, credential_store=None)
    engine.session.conversation_id = "stale"
    engine.session.api_messages = [{"role": "user", "content": "x"}]

    recovered = await engine._recover_from_auth_error(allow_session_reset=True)

    assert recovered is True
    assert client.reset_calls == 1
    assert engine.session.conversation_id == ""
    assert engine.session.api_messages == []


@pytest.mark.asyncio
async def test_engine_recover_from_auth_error_returns_false_without_store(
    tmp_path,
    mock_config: LocalForgeConfig,
) -> None:
    """Without credential store and no reset allowed, recovery should fail."""
    client = _DummyClient()
    engine = CloudChatEngine(mock_config, client, tmp_path, credential_store=None)

    recovered = await engine._recover_from_auth_error(allow_session_reset=False)

    assert recovered is False


# -----------------------------------------------------------------------
# CloudChatEngine — focus persistence (save on /clear-focus, /add, /remove)
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_focus_persists_to_disk(
    tmp_path,
    mock_config: LocalForgeConfig,
) -> None:
    """After /clear-focus the session should be saved so focus doesn't reappear."""
    client = _DummyClient()
    engine = CloudChatEngine(mock_config, client, tmp_path, credential_store=None)
    engine.session.add_focus_path("src/main.py")
    engine.save_session()

    # Simulate /clear-focus
    await engine._handle_command("/clear-focus")

    assert not engine.session.has_focus()

    # Reload from disk and verify focus is gone
    from localforge.cloud.session import CloudChatSession

    reloaded = CloudChatSession.load(engine._get_session_path())
    assert not reloaded.has_focus()
    assert reloaded.focus_paths == []


@pytest.mark.asyncio
async def test_add_focus_persists_to_disk(
    tmp_path,
    mock_config: LocalForgeConfig,
) -> None:
    """/add should persist focus state to disk."""
    client = _DummyClient()
    engine = CloudChatEngine(mock_config, client, tmp_path, credential_store=None)

    # Create a file so /add can find it
    test_file = tmp_path / "hello.py"
    test_file.write_text("print('hi')", encoding="utf-8")

    await engine._handle_command("/add hello.py")

    assert engine.session.has_focus()

    # Reload from disk and verify focus persisted
    from localforge.cloud.session import CloudChatSession

    reloaded = CloudChatSession.load(engine._get_session_path())
    assert reloaded.has_focus()
    assert "hello.py" in reloaded.focus_paths


@pytest.mark.asyncio
async def test_remove_focus_persists_to_disk(
    tmp_path,
    mock_config: LocalForgeConfig,
) -> None:
    """/remove should persist focus state to disk."""
    client = _DummyClient()
    engine = CloudChatEngine(mock_config, client, tmp_path, credential_store=None)
    engine.session.add_focus_path("src/main.py")
    engine.session.add_focus_path("src/utils.py")
    engine.save_session()

    await engine._handle_command("/remove src/main.py")

    # Reload from disk and verify only utils.py remains
    from localforge.cloud.session import CloudChatSession

    reloaded = CloudChatSession.load(engine._get_session_path())
    assert reloaded.focus_paths == ["src/utils.py"]


# -----------------------------------------------------------------------
# CloudClient.health_check — retries on transient httpx errors
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_retries_on_read_timeout(
    cloud_client: CloudClient,
    mocker,
) -> None:
    """health_check should retry on ReadTimeout (not raise APIError immediately)."""
    import httpx

    resp_200 = mocker.MagicMock(status_code=200, text="ok", headers={})

    mock_http = mocker.MagicMock()
    mock_http.post = mocker.AsyncMock(
        side_effect=[
            httpx.ReadTimeout("Read timed out"),
            resp_200,
        ]
    )
    mock_http.aclose = mocker.AsyncMock()

    cloud_client._client = mock_http
    mocker.patch.object(cloud_client, "_new_httpx_client", return_value=mock_http)
    mocker.patch("localforge.cloud.client.asyncio.sleep", return_value=None)

    result = await cloud_client.health_check()
    assert result is True
    assert mock_http.post.await_count == 2


@pytest.mark.asyncio
async def test_health_check_retries_on_pool_timeout(
    cloud_client: CloudClient,
    mocker,
) -> None:
    """health_check should retry on PoolTimeout."""
    import httpx

    resp_200 = mocker.MagicMock(status_code=200, text="ok", headers={})

    mock_http = mocker.MagicMock()
    mock_http.post = mocker.AsyncMock(
        side_effect=[
            httpx.PoolTimeout("Pool timed out"),
            httpx.PoolTimeout("Pool timed out"),
            resp_200,
        ]
    )
    mock_http.aclose = mocker.AsyncMock()

    cloud_client._client = mock_http
    mocker.patch.object(cloud_client, "_new_httpx_client", return_value=mock_http)
    mocker.patch("localforge.cloud.client.asyncio.sleep", return_value=None)

    result = await cloud_client.health_check()
    assert result is True
    assert mock_http.post.await_count == 3
