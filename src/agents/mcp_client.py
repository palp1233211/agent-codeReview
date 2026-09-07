"""Minimal Streamable HTTP MCP client used by OpenAI-compatible runtimes."""
from __future__ import annotations

import itertools
import json
import math
import queue
import socket
import threading
import time
from urllib.parse import urljoin
from dataclasses import dataclass
from typing import Any

import requests


class McpClientError(RuntimeError):
    """Raised when an MCP server returns a transport or JSON-RPC error."""


def _jsonrpc_id_matches(actual: Any, expected: int) -> bool:
    """Match numeric JSON-RPC IDs while rejecting bool and string coercion."""
    if isinstance(actual, bool):
        return False
    if isinstance(actual, int):
        return actual == expected
    return isinstance(actual, float) and math.isfinite(actual) and actual.is_integer() and actual == expected


@dataclass(frozen=True)
class McpTool:
    """A tool imported from an MCP server."""

    exposed_name: str
    server_name: str
    description: str
    input_schema: dict[str, Any]


class HttpMcpClient:
    """Small synchronous client for stateless and session-based HTTP MCP servers."""

    def __init__(
        self,
        *,
        server_label: str,
        server_url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self.server_label = server_label
        self.server_url = server_url
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self._session = requests.Session()
        self._session.headers.update(headers or {})
        self._session.headers.update(
            {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
        )
        self._request_ids = itertools.count(1)
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        self._request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "my-agent", "version": "1.0"},
            },
        )
        self._notify("notifications/initialized")
        self._initialized = True

    def list_tools(self) -> list[McpTool]:
        self.initialize()
        result = self._request("tools/list", {})
        tools = []
        for item in result.get("tools", []):
            server_name = str(item.get("name") or "")
            if not server_name:
                continue
            tools.append(
                McpTool(
                    exposed_name=f"mcp__{self.server_label}__{server_name}",
                    server_name=server_name,
                    description=str(item.get("description") or ""),
                    input_schema=item.get("inputSchema") or {"type": "object", "properties": {}},
                )
            )
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        return self._request("tools/call", {"name": name, "arguments": arguments})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._request_ids),
            "method": method,
            "params": params,
        }
        attempts = self.max_retries + 1 if self._is_retryable(method, params) else 1
        for attempt in range(attempts):
            response = None
            try:
                response = self._session.post(
                    self.server_url,
                    json=payload,
                    timeout=self.timeout,
                    stream=True,
                )
                self._capture_session_id(response)
                response.raise_for_status()
                data = self._decode_response(response, payload["id"])
                break
            except (requests.ConnectionError, requests.Timeout):
                if attempt >= attempts - 1:
                    raise
                time.sleep(min(2.0, 0.5 * (2**attempt)))
            finally:
                if response is not None:
                    response.close()
        else:  # pragma: no cover - the loop either breaks or raises
            raise McpClientError(f"MCP {self.server_label} {method} 请求失败")
        error = data.get("error")
        if error:
            raise McpClientError(
                f"MCP {self.server_label} {method} 失败: "
                f"{error.get('message') or json.dumps(error, ensure_ascii=False)}"
            )
        result = data.get("result", {})
        return result if isinstance(result, dict) else {"content": result}

    @staticmethod
    def _is_retryable(method: str, params: dict[str, Any]) -> bool:
        """Retry reads, but never retry a potentially duplicate comment write."""
        if method != "tools/call":
            return True
        return params.get("name") != "create_change_request_comment"

    def _notify(self, method: str) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        response = self._session.post(self.server_url, json=payload, timeout=self.timeout)
        try:
            self._capture_session_id(response)
            response.raise_for_status()
        finally:
            response.close()

    def _capture_session_id(self, response: requests.Response) -> None:
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self._session.headers["Mcp-Session-Id"] = session_id

    @staticmethod
    def _decode_response(response: requests.Response, request_id: int) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            payload = response.json()
            if not isinstance(payload, dict):
                raise McpClientError("MCP JSON-RPC 响应不是对象")
            if not _jsonrpc_id_matches(payload.get("id"), request_id):
                raise McpClientError(
                    f"MCP JSON-RPC 响应 id 不匹配: expected={request_id} actual={payload.get('id')}"
                )
            return payload

        data_lines: list[str] = []
        lines = response.iter_lines(decode_unicode=True)
        for raw_line in lines:
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
                continue
            if line or not data_lines:
                continue
            value = "\n".join(data_lines)
            data_lines = []
            if not value or value == "[DONE]":
                continue
            payload = json.loads(value)
            if isinstance(payload, dict) and _jsonrpc_id_matches(payload.get("id"), request_id):
                return payload
        if data_lines:
            payload = json.loads("\n".join(data_lines))
            if isinstance(payload, dict) and _jsonrpc_id_matches(payload.get("id"), request_id):
                return payload
        raise McpClientError(
            f"MCP SSE 响应中没有匹配 request id={request_id} 的 JSON-RPC 数据"
        )

    def close(self) -> None:
        self._session.close()


class SseMcpClient:
    """Synchronous client for the legacy MCP SSE + messages transport."""

    def __init__(
        self,
        *,
        server_label: str,
        server_url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self.server_label = server_label
        self.server_url = server_url
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self._session = requests.Session()
        self._session.headers.update(headers or {})
        self._stream_session = requests.Session()
        self._stream_session.headers.update(headers or {})
        self._request_ids = itertools.count(1)
        self._messages_url: str | None = None
        self._events: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._stream_response: requests.Response | None = None
        self._stream_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._initialize_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        with self._initialize_lock:
            if self._initialized:
                return
            self._connect()
            self._request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "my-agent", "version": "1.0"},
                },
            )
            self._notify("notifications/initialized")
            self._initialized = True

    def list_tools(self) -> list[McpTool]:
        self.initialize()
        result = self._request("tools/list", {})
        return [
            McpTool(
                exposed_name=f"mcp__{self.server_label}__{item['name']}",
                server_name=item["name"],
                description=str(item.get("description") or ""),
                input_schema=item.get("inputSchema") or {"type": "object", "properties": {}},
            )
            for item in result.get("tools", [])
            if item.get("name")
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        return self._request("tools/call", {"name": name, "arguments": arguments})

    def _connect(self) -> None:
        if self._stream_thread is not None:
            return
        self._events = queue.Queue()
        self._stop_event.clear()
        ready: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)

        def consume() -> None:
            try:
                response = self._stream_session.get(
                    self.server_url,
                    headers={**self._stream_session.headers, "Accept": "text/event-stream"},
                    stream=True,
                    timeout=(self.timeout, None),
                )
                self._stream_response = response
                response.raise_for_status()
                event_name = "message"
                data_lines: list[str] = []
                ready_sent = False
                for raw_line in response.iter_lines(decode_unicode=False, delimiter=b"\n"):
                    if self._stop_event.is_set():
                        break
                    line = (raw_line or b"").rstrip(b"\r").decode("utf-8")
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                        continue
                    if line or not data_lines:
                        continue
                    value = "\n".join(data_lines)
                    data_lines = []
                    if event_name == "endpoint":
                        self._messages_url = urljoin(self.server_url, value)
                        if not ready_sent:
                            ready.put(self._messages_url)
                            ready_sent = True
                    elif value and value != "[DONE]":
                        payload = json.loads(value)
                        if isinstance(payload, dict):
                            self._events.put(payload)
                    event_name = "message"
                if not ready_sent:
                    ready.put(McpClientError(f"MCP {self.server_label} SSE 未返回消息端点"))
                else:
                    self._events.put(McpClientError(f"MCP {self.server_label} SSE 流已结束"))
            except BaseException as exc:  # transport failures must wake waiting callers
                if ready.empty():
                    ready.put(exc)
                self._events.put(exc)

        self._stream_thread = threading.Thread(target=consume, daemon=True)
        self._stream_thread.start()
        try:
            outcome = ready.get(timeout=self.timeout)
        except queue.Empty as exc:
            self._stop_stream()
            raise McpClientError(
                f"MCP {self.server_label} SSE 连接超时，未收到消息端点"
            ) from exc
        if isinstance(outcome, BaseException):
            self._stop_stream()
            raise McpClientError(f"MCP {self.server_label} SSE 连接失败: {outcome}") from outcome

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._request_lock:
            if not self._messages_url:
                raise McpClientError(f"MCP {self.server_label} SSE 消息端点不可用")
            request_id = next(self._request_ids)
            attempts = self.max_retries + 1 if self._is_retryable(method, params) else 1
            for attempt in range(attempts):
                response = None
                try:
                    response = self._session.post(
                        self._messages_url,
                        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                        timeout=self.timeout,
                    )
                    response.raise_for_status()
                    while True:
                        try:
                            event = self._events.get(timeout=self.timeout)
                        except queue.Empty as exc:
                            raise McpClientError(f"MCP {self.server_label} {method} 超时") from exc
                        if isinstance(event, BaseException):
                            raise McpClientError(f"MCP {self.server_label} SSE 连接断开: {event}") from event
                        if not _jsonrpc_id_matches(event.get("id"), request_id):
                            continue
                        if event.get("error"):
                            error = event["error"]
                            raise McpClientError(
                                f"MCP {self.server_label} {method} 失败: "
                                f"{error.get('message') or json.dumps(error, ensure_ascii=False)}"
                            )
                        result = event.get("result", {})
                        return result if isinstance(result, dict) else {"content": result}
                except (requests.ConnectionError, requests.Timeout):
                    if attempt >= attempts - 1:
                        raise
                    time.sleep(min(2.0, 0.5 * (2**attempt)))
                finally:
                    if response is not None:
                        response.close()

            raise McpClientError(f"MCP {self.server_label} {method} 请求失败")  # pragma: no cover

    @staticmethod
    def _is_retryable(method: str, params: dict[str, Any]) -> bool:
        """Retry reads, but never retry a potentially duplicate comment write."""
        if method != "tools/call":
            return True
        return params.get("name") != "create_change_request_comment"

    def _notify(self, method: str) -> None:
        if not self._messages_url:
            raise McpClientError(f"MCP {self.server_label} SSE 消息端点不可用")
        response = self._session.post(
            self._messages_url,
            json={"jsonrpc": "2.0", "method": method, "params": {}},
            timeout=self.timeout,
        )
        response.raise_for_status()

    def _stop_stream(self) -> None:
        self._stop_event.set()
        response = self._stream_response
        if response is not None:
            # requests may be blocked indefinitely in ``iter_lines`` because
            # legacy SSE has no read timeout.  Calling Response.close() in the
            # caller thread can block on the same read, leaving the whole CLI
            # stuck after a successful model response.  Break the socket first
            # and keep Response.close() bounded in a daemon helper.
            self._interrupt_stream_socket(response)
            self._close_response_in_background(response)
        thread = self._stream_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=min(self.timeout, 1.0))
        if thread is not None and thread.is_alive():
            raise McpClientError(f"MCP {self.server_label} SSE 消费线程未能停止")
        self._stream_thread = None
        self._stream_response = None
        self._messages_url = None

    @staticmethod
    def _interrupt_stream_socket(response: requests.Response) -> None:
        """Wake a requests SSE reader without waiting for Response.close()."""
        raw = getattr(response, "raw", None)
        candidates = (
            getattr(getattr(raw, "_connection", None), "sock", None),
            getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None),
        )
        for candidate in candidates:
            sock = getattr(candidate, "_sock", candidate)
            if not isinstance(sock, socket.socket):
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
            return

    @staticmethod
    def _close_response_in_background(response: requests.Response) -> None:
        """Do not let a stuck urllib3 response block MCP client shutdown."""
        closer = threading.Thread(target=response.close, daemon=True)
        closer.start()
        closer.join(timeout=0.1)

    def close(self) -> None:
        self._stop_stream()
        self._stream_session.close()
        self._session.close()
        self._initialized = False


def create_http_mcp_clients(
    configs: list[dict[str, Any]],
) -> list[HttpMcpClient | SseMcpClient]:
    """Build HTTP MCP clients from runtime configuration."""
    clients = []
    for config in configs:
        server_url = config.get("server_url") or config.get("url")
        if config.get("command") and not server_url:
            raise McpClientError(
                "OpenAI runtime 不支持 command-only stdio MCP；请使用 http/sse transport 和 URL。"
            )
        transport = str(config.get("transport") or "http").strip().lower().replace("-", "_")
        if transport == "stdio":
            raise McpClientError(
                "OpenAI runtime 不支持 stdio MCP；请使用 http/sse transport。"
            )
        if transport not in {"http", "streamable_http", "sse"}:
            raise McpClientError(f"OpenAI runtime 不支持 MCP transport: {transport}")
        if not server_url:
            raise McpClientError(
                f"OpenAI MCP transport={transport} 缺少 server_url/url。"
            )
        if transport == "sse":
            clients.append(
                SseMcpClient(
                    server_label=str(config.get("server_label") or config.get("name") or "mcp"),
                    server_url=str(server_url),
                    headers=dict(config.get("headers") or {}),
                    timeout=float(config.get("timeout") or 30),
                )
            )
            continue
        clients.append(
            HttpMcpClient(
                server_label=str(config.get("server_label") or config.get("name") or "mcp"),
                server_url=str(server_url),
                headers=dict(config.get("headers") or {}),
                timeout=float(config.get("timeout") or 30),
            )
        )
    return clients
