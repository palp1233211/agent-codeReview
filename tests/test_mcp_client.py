"""HTTP MCP client and OpenAI bridge tests."""
from __future__ import annotations

import threading
from unittest.mock import Mock

import pytest
import requests

from src.agents.mcp_client import (
    HttpMcpClient,
    McpClientError,
    McpTool,
    SseMcpClient,
    create_http_mcp_clients,
)
from src.agents.runtime import OpenAIAgentRuntime, RuntimeOptions


def _response(payload: dict, *, status_code: int = 200, headers: dict | None = None) -> Mock:
    response = Mock()
    response.status_code = status_code
    response.headers = headers or {"content-type": "application/json"}
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _sse_response(events: list[dict]) -> Mock:
    response = Mock()
    response.status_code = 200
    response.headers = {"content-type": "text/event-stream"}
    response.text = "\n\n".join(
        f"data: {__import__('json').dumps(event)}" for event in events
    )
    response.iter_lines.return_value = iter(response.text.splitlines())
    response.raise_for_status.return_value = None
    return response


def test_http_mcp_client_lists_and_calls_tools():
    client = HttpMcpClient(
        server_label="yunxiao",
        server_url="https://example.test/mcp",
        headers={"Authorization": "Bearer secret"},
    )
    client._session.post = Mock(  # noqa: SLF001 - transport is isolated in this unit test
        side_effect=[
            _response(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"protocolVersion": "2025-03-26", "capabilities": {}},
                },
                headers={"content-type": "application/json", "Mcp-Session-Id": "session-1"},
            ),
            _response({}, status_code=202, headers={}),
            _response(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "tools": [
                            {
                                "name": "get_change_request",
                                "description": "Get an MR",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"localId": {"type": "string"}},
                                    "required": ["localId"],
                                },
                            }
                        ]
                    },
                }
            ),
            _response(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "result": {"content": [{"type": "text", "text": "MR 1185"}]},
                }
            ),
        ]
    )

    tools = client.list_tools()
    result = client.call_tool("get_change_request", {"localId": "1185"})

    assert tools == [
        McpTool(
            exposed_name="mcp__yunxiao__get_change_request",
            server_name="get_change_request",
            description="Get an MR",
            input_schema={
                "type": "object",
                "properties": {"localId": {"type": "string"}},
                "required": ["localId"],
            },
        )
    ]
    assert result["content"][0]["text"] == "MR 1185"
    assert client._session.headers["Mcp-Session-Id"] == "session-1"  # noqa: SLF001
    assert client._session.headers["Authorization"] == "Bearer secret"  # noqa: SLF001
    assert client._session.post.call_args_list[0].kwargs["stream"] is True  # noqa: SLF001


def test_http_mcp_client_surfaces_json_rpc_errors():
    client = HttpMcpClient(server_label="yunxiao", server_url="https://example.test/mcp")
    client._initialized = True  # noqa: SLF001
    client._session.post = Mock(  # noqa: SLF001
        return_value=_response(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32603, "message": "403 FORBIDDEN"},
            }
        )
    )

    with pytest.raises(McpClientError, match="403 FORBIDDEN"):
        client.call_tool("get_change_request", {})


def test_http_mcp_client_retries_transient_connection_error():
    client = HttpMcpClient(server_label="yunxiao", server_url="https://example.test/mcp")
    client._initialized = True  # noqa: SLF001
    client._session.post = Mock(  # noqa: SLF001
        side_effect=[
            requests.ConnectionError("remote disconnected"),
            _response({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}),
        ]
    )

    assert client.call_tool("get_change_request", {}) == {"ok": True}
    assert client._session.post.call_count == 2  # noqa: SLF001


def test_http_mcp_client_does_not_retry_comment_writes():
    client = HttpMcpClient(server_label="yunxiao", server_url="https://example.test/mcp")
    client._initialized = True  # noqa: SLF001
    client._session.post = Mock(  # noqa: SLF001
        side_effect=requests.ConnectionError("remote disconnected")
    )

    with pytest.raises(requests.ConnectionError):
        client.call_tool("create_change_request_comment", {})

    assert client._session.post.call_count == 1  # noqa: SLF001


def test_http_mcp_client_sse_skips_notifications_and_matches_request_id():
    client = HttpMcpClient(server_label="yunxiao", server_url="https://example.test/mcp")
    client._initialized = True  # noqa: SLF001
    client._session.post = Mock(  # noqa: SLF001
        return_value=_sse_response(
            [
                {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 50}},
                {"jsonrpc": "2.0", "id": 99, "result": {"ignored": True}},
                {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "matched"}]}},
            ]
        )
    )

    result = client.call_tool("get_change_request", {})

    assert result["content"][0]["text"] == "matched"


def test_http_mcp_client_stops_stream_after_matching_response():
    response = _sse_response([])

    def lines():
        yield 'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}'
        yield ""
        raise AssertionError("匹配响应后不应继续等待长连接")

    response.iter_lines.return_value = lines()
    client = HttpMcpClient(server_label="yunxiao", server_url="https://example.test/mcp")
    client._initialized = True  # noqa: SLF001
    client._session.post = Mock(return_value=response)  # noqa: SLF001

    assert client.call_tool("ping", {}) == {"ok": True}
    assert client._session.post.call_args.kwargs["stream"] is True  # noqa: SLF001
    response.close.assert_called_once_with()


def test_http_mcp_client_parses_multiline_sse_data():
    response = _sse_response([])
    response.iter_lines.return_value = iter(
        [
            'data: {"jsonrpc":"2.0",',
            'data: "id":1,"result":{"ok":true}}',
            "",
        ]
    )
    client = HttpMcpClient(server_label="yunxiao", server_url="https://example.test/mcp")
    client._initialized = True  # noqa: SLF001
    client._session.post = Mock(return_value=response)  # noqa: SLF001

    assert client.call_tool("ping", {}) == {"ok": True}


def test_http_mcp_client_rejects_mismatched_json_rpc_id():
    client = HttpMcpClient(server_label="yunxiao", server_url="https://example.test/mcp")
    client._initialized = True  # noqa: SLF001
    client._session.post = Mock(  # noqa: SLF001
        return_value=_response({"jsonrpc": "2.0", "id": 999, "result": {"ignored": True}})
    )

    with pytest.raises(McpClientError, match="响应 id 不匹配"):
        client.call_tool("get_change_request", {})


@pytest.mark.parametrize("invalid_id", [True, "1"])
def test_http_mcp_client_rejects_coerced_json_rpc_ids(invalid_id):
    client = HttpMcpClient(server_label="yunxiao", server_url="https://example.test/mcp")
    client._initialized = True  # noqa: SLF001
    client._session.post = Mock(  # noqa: SLF001
        return_value=_response({"jsonrpc": "2.0", "id": invalid_id, "result": {"ignored": True}})
    )

    with pytest.raises(McpClientError, match="响应 id 不匹配"):
        client.call_tool("get_change_request", {})


def test_http_mcp_client_accepts_equivalent_numeric_json_rpc_id():
    client = HttpMcpClient(server_label="yunxiao", server_url="https://example.test/mcp")
    client._initialized = True  # noqa: SLF001
    client._session.post = Mock(  # noqa: SLF001
        return_value=_response({"jsonrpc": "2.0", "id": 1.0, "result": {"ok": True}})
    )

    assert client.call_tool("get_change_request", {}) == {"ok": True}


@pytest.mark.parametrize("status_code", [401, 403, 500])
def test_http_mcp_client_closes_http_error_responses(status_code):
    response = _response({}, status_code=status_code)
    response.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status_code}")
    client = HttpMcpClient(server_label="yunxiao", server_url="https://example.test/mcp")
    client._initialized = True  # noqa: SLF001
    client._session.post = Mock(return_value=response)  # noqa: SLF001

    with pytest.raises(requests.HTTPError, match=str(status_code)):
        client.call_tool("get_change_request", {})

    response.close.assert_called_once_with()


def test_http_mcp_client_close_releases_session():
    client = HttpMcpClient(server_label="yunxiao", server_url="https://example.test/mcp")
    session = client._session  # noqa: SLF001
    session.close = Mock()

    client.close()

    session.close.assert_called_once_with()


def test_mcp_client_factory_keeps_http_default_for_sse_suffix_url():
    clients = create_http_mcp_clients(
        [{"server_label": "yunxiao", "server_url": "http://192.168.7.71:3000/sse"}]
    )

    try:
        assert len(clients) == 1
        assert isinstance(clients[0], HttpMcpClient)
    finally:
        for client in clients:
            client.close()


def test_legacy_sse_skips_notifications_and_matches_request_id():
    client = SseMcpClient(
        server_label="yunxiao",
        server_url="https://example.test/sse",
    )
    client._initialized = True  # noqa: SLF001
    client._messages_url = "https://example.test/messages"  # noqa: SLF001
    client._session.post = Mock(return_value=_response({}))  # noqa: SLF001
    client._events.put(  # noqa: SLF001
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 50}}
    )
    client._events.put({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})  # noqa: SLF001

    assert client.call_tool("ping", {}) == {"ok": True}


def test_legacy_sse_retries_transient_message_connection(monkeypatch):
    import src.agents.mcp_client as mcp_client

    monkeypatch.setattr(mcp_client.time, "sleep", lambda _seconds: None)
    client = SseMcpClient(
        server_label="yunxiao",
        server_url="https://example.test/sse",
    )
    client._initialized = True  # noqa: SLF001
    client._messages_url = "https://example.test/messages"  # noqa: SLF001
    client._session.post = Mock(  # noqa: SLF001
        side_effect=[
            requests.ConnectionError("remote disconnected"),
            _response({}),
        ]
    )
    client._events.put({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})  # noqa: SLF001

    assert client.call_tool("get_change_request", {}) == {"ok": True}
    assert client._session.post.call_count == 2  # noqa: SLF001


def test_legacy_sse_does_not_retry_comment_writes(monkeypatch):
    import src.agents.mcp_client as mcp_client

    monkeypatch.setattr(mcp_client.time, "sleep", lambda _seconds: None)
    client = SseMcpClient(
        server_label="yunxiao",
        server_url="https://example.test/sse",
    )
    client._initialized = True  # noqa: SLF001
    client._messages_url = "https://example.test/messages"  # noqa: SLF001
    client._session.post = Mock(  # noqa: SLF001
        side_effect=requests.ConnectionError("remote disconnected")
    )

    with pytest.raises(requests.ConnectionError):
        client.call_tool("create_change_request_comment", {})

    assert client._session.post.call_count == 1  # noqa: SLF001


def test_legacy_sse_timeout_is_structured_error():
    client = SseMcpClient(
        server_label="yunxiao",
        server_url="https://example.test/sse",
        timeout=0.01,
    )
    client._initialized = True  # noqa: SLF001
    client._messages_url = "https://example.test/messages"  # noqa: SLF001
    client._session.post = Mock(return_value=_response({}))  # noqa: SLF001

    with pytest.raises(McpClientError, match="tools/call 超时"):
        client.call_tool("ping", {})


def test_legacy_sse_connect_sends_configured_headers():
    response = Mock()
    response.raise_for_status.return_value = None
    response.iter_lines.return_value = iter(
        [
            b"event: endpoint",
            b"data: /messages?sessionId=session-1",
            b"",
        ]
    )
    client = SseMcpClient(
        server_label="yunxiao",
        server_url="https://example.test/sse",
        headers={"X-Yunxiao-Token": "secret"},
        timeout=0.01,
    )
    client._stream_session.get = Mock(return_value=response)  # noqa: SLF001

    client._connect()  # noqa: SLF001

    headers = client._stream_session.get.call_args.kwargs["headers"]  # noqa: SLF001
    assert headers["Accept"] == "text/event-stream"
    assert headers["X-Yunxiao-Token"] == "secret"


def test_legacy_sse_connect_timeout_stops_background_thread():
    import threading

    stopped = threading.Event()
    response = Mock()
    response.raise_for_status.return_value = None

    def blocked_lines(**_kwargs):
        stopped.wait(timeout=1)
        return iter(())

    response.iter_lines.side_effect = blocked_lines
    response.close.side_effect = stopped.set
    client = SseMcpClient(
        server_label="yunxiao",
        server_url="https://example.test/sse",
        timeout=0.02,
    )
    client._stream_session.get = Mock(return_value=response)  # noqa: SLF001

    with pytest.raises(McpClientError, match="连接超时"):
        client.initialize()

    assert stopped.is_set()
    assert client._stream_thread is None  # noqa: SLF001


def test_legacy_sse_close_releases_sessions_and_thread():
    client = SseMcpClient(server_label="yunxiao", server_url="https://example.test/sse")
    stream_response = Mock()
    stream_thread = Mock()
    stream_thread.is_alive.side_effect = [True, False]
    client._stream_response = stream_response  # noqa: SLF001
    client._stream_thread = stream_thread  # noqa: SLF001
    client._stream_session.close = Mock()  # noqa: SLF001
    client._session.close = Mock()  # noqa: SLF001

    client.close()

    stream_response.close.assert_called_once_with()
    client._stream_session.close.assert_called_once_with()  # noqa: SLF001
    client._session.close.assert_called_once_with()  # noqa: SLF001
    stream_thread.join.assert_called_once()
    assert client._stream_thread is None  # noqa: SLF001
    assert client._messages_url is None  # noqa: SLF001


def test_legacy_sse_close_interrupts_blocked_reader_without_waiting_for_response_close():
    import socket
    import time

    reader, writer = socket.socketpair()
    started = threading.Event()
    response = Mock()
    response.raw._connection.sock = reader
    response.close.side_effect = lambda: time.sleep(10)

    def consume() -> None:
        started.set()
        try:
            reader.recv(1)
        except OSError:
            pass

    client = SseMcpClient(server_label="yunxiao", server_url="https://example.test/sse")
    client._stream_response = response  # noqa: SLF001
    client._stream_thread = threading.Thread(target=consume, daemon=True)  # noqa: SLF001
    client._stream_thread.start()  # noqa: SLF001
    assert started.wait(timeout=1)

    started_at = time.monotonic()
    client._stop_stream()  # noqa: SLF001

    assert time.monotonic() - started_at < 1.2
    assert client._stream_thread is None  # noqa: SLF001
    writer.close()


def test_legacy_sse_reconnects_after_eof_and_close():
    import queue

    def stream_response(endpoint: str):
        lines = queue.Queue()
        lines.put(b"event: endpoint")
        lines.put(f"data: {endpoint}".encode())
        lines.put(b"")
        response = Mock()
        response.raise_for_status.return_value = None

        def consume():
            while True:
                line = lines.get(timeout=1)
                if line is None:
                    return
                yield line

        response.iter_lines.side_effect = lambda **_kwargs: consume()
        response.close.side_effect = lambda: lines.put(None)
        return response, lines

    first, first_lines = stream_response("/messages/one")
    second, second_lines = stream_response("/messages/two")
    client = SseMcpClient(server_label="yunxiao", server_url="https://example.test/sse")
    client._stream_session.get = Mock(side_effect=[first, second])  # noqa: SLF001

    def post_response(_url, *, json, **_kwargs):
        request_id = json.get("id")
        target = first_lines if request_id in {1, 2, 3} else second_lines
        if request_id in {1, 2, 4, 5}:
            target.put(b"event: message")
            target.put(
                (
                    'data: {"jsonrpc":"2.0","id":%d,"result":{"tools":[]}}'
                    % request_id
                ).encode()
            )
            target.put(b"")
        return _response({})

    client._session.post = Mock(side_effect=post_response)  # noqa: SLF001

    assert client.list_tools() == []
    first_lines.put(None)
    client._stream_thread.join(timeout=1)  # noqa: SLF001
    with pytest.raises(McpClientError, match="SSE 连接断开.*流已结束"):
        client.call_tool("after-eof", {})
    client.close()
    assert client.list_tools() == []
    client.close()

    assert client._stream_session.get.call_count == 2  # noqa: SLF001
    assert first.close.call_count == 1
    assert second.close.call_count == 1


@pytest.mark.asyncio
async def test_openai_runtime_imports_mcp_as_function_tools(monkeypatch):
    client = Mock()
    client.server_label = "yunxiao"
    tool = McpTool(
        exposed_name="mcp__yunxiao__get_change_request",
        server_name="get_change_request",
        description="Get an MR",
        input_schema={"type": "object", "properties": {}},
    )
    client.list_tools.return_value = [tool]
    monkeypatch.setattr(
        "src.agents.runtime.create_http_mcp_clients",
        lambda _configs: [client],
    )

    runtime = OpenAIAgentRuntime()
    imported = await runtime._load_mcp_tools(  # noqa: SLF001
        RuntimeOptions(
            allowed_tools=[tool.exposed_name],
            remote_mcp_servers=[{"server_url": "https://example.test/mcp"}],
        ),
        {tool.exposed_name},
    )

    schema, imported_client, imported_tool = imported[tool.exposed_name]
    assert schema == {
        "type": "function",
        "name": tool.exposed_name,
        "description": "Get an MR",
        "parameters": {"type": "object", "properties": {}},
    }
    assert imported_client is client
    assert imported_tool is tool
