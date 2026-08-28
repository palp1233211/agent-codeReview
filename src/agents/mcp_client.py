"""Minimal Streamable HTTP MCP client used by OpenAI-compatible runtimes."""
from __future__ import annotations

import itertools
import json
import os
import queue
import select
import subprocess
import threading
from urllib.parse import urljoin
from dataclasses import dataclass
from typing import Any

import requests


class McpClientError(RuntimeError):
    """Raised when an MCP server returns a transport or JSON-RPC error."""


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
    ) -> None:
        self.server_label = server_label
        self.server_url = server_url
        self.timeout = timeout
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
        response = self._session.post(self.server_url, json=payload, timeout=self.timeout)
        self._capture_session_id(response)
        response.raise_for_status()
        data = self._decode_response(response)
        error = data.get("error")
        if error:
            raise McpClientError(
                f"MCP {self.server_label} {method} 失败: "
                f"{error.get('message') or json.dumps(error, ensure_ascii=False)}"
            )
        result = data.get("result", {})
        return result if isinstance(result, dict) else {"content": result}

    def _notify(self, method: str) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        response = self._session.post(self.server_url, json=payload, timeout=self.timeout)
        self._capture_session_id(response)
        response.raise_for_status()

    def _capture_session_id(self, response: requests.Response) -> None:
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self._session.headers["Mcp-Session-Id"] = session_id

    @staticmethod
    def _decode_response(response: requests.Response) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            return response.json()

        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            value = line[5:].strip()
            if value and value != "[DONE]":
                payload = json.loads(value)
                if isinstance(payload, dict):
                    return payload
        raise McpClientError("MCP SSE 响应中没有 JSON-RPC 数据")

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
    ) -> None:
        self.server_label = server_label
        self.server_url = server_url
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(headers or {})
        self._stream_session = requests.Session()
        self._stream_session.headers.update(headers or {})
        self._request_ids = itertools.count(1)
        self._messages_url: str | None = None
        self._events: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._stream_response: requests.Response | None = None
        self._stream_thread: threading.Thread | None = None
        self._initialized = False

    def initialize(self) -> None:
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
        ready: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)

        def consume() -> None:
            try:
                response = self._stream_session.get(
                    self.server_url,
                    headers={"Accept": "text/event-stream"},
                    stream=True,
                    timeout=(self.timeout, None),
                )
                self._stream_response = response
                response.raise_for_status()
                event_name = "message"
                data_lines: list[str] = []
                ready_sent = False
                for raw_line in response.iter_lines(decode_unicode=False, delimiter=b"\n"):
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
            except BaseException as exc:  # transport failures must wake waiting callers
                if ready.empty():
                    ready.put(exc)
                self._events.put(exc)

        self._stream_thread = threading.Thread(target=consume, daemon=True)
        self._stream_thread.start()
        outcome = ready.get(timeout=self.timeout)
        if isinstance(outcome, BaseException):
            raise McpClientError(f"MCP {self.server_label} SSE 连接失败: {outcome}") from outcome

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._messages_url:
            raise McpClientError(f"MCP {self.server_label} SSE 消息端点不可用")
        request_id = next(self._request_ids)
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
            if event.get("id") != request_id:
                continue
            if event.get("error"):
                error = event["error"]
                raise McpClientError(
                    f"MCP {self.server_label} {method} 失败: "
                    f"{error.get('message') or json.dumps(error, ensure_ascii=False)}"
                )
            result = event.get("result", {})
            return result if isinstance(result, dict) else {"content": result}

    def _notify(self, method: str) -> None:
        if not self._messages_url:
            raise McpClientError(f"MCP {self.server_label} SSE 消息端点不可用")
        response = self._session.post(
            self._messages_url,
            json={"jsonrpc": "2.0", "method": method, "params": {}},
            timeout=self.timeout,
        )
        response.raise_for_status()

    def close(self) -> None:
        if self._stream_response is not None:
            self._stream_response.close()
        self._stream_session.close()
        self._session.close()
        self._initialized = False


class StdioMcpClient:
    """JSON-lines stdio MCP client for local MCP server processes."""

    def __init__(
        self,
        *,
        server_label: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.server_label = server_label
        self.command = command
        self.args = args or []
        self.env = {**os.environ, **(env or {})}
        self.timeout = timeout
        self._process: subprocess.Popen[str] | None = None
        self._request_ids = itertools.count(1)
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        self._ensure_process()
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

    def close(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
        self._initialized = False

    def _ensure_process(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self._process = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            bufsize=1,
            env=self.env,
        )

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._ensure_process()
        assert self._process is not None
        if self._process.stdin is None or self._process.stdout is None:
            raise McpClientError(f"MCP {self.server_label} stdio 管道不可用")
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._request_ids),
            "method": method,
            "params": params,
        }
        try:
            self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
            ready, _, _ = select.select([self._process.stdout], [], [], self.timeout)
            if not ready:
                raise McpClientError(f"MCP {self.server_label} {method} 超时")
            line = self._process.stdout.readline()
        except (BrokenPipeError, OSError) as exc:
            self.close()
            raise McpClientError(f"MCP {self.server_label} stdio 连接断开: {exc}") from exc
        if not line:
            code = self._process.poll()
            self.close()
            raise McpClientError(f"MCP {self.server_label} 进程退出: code={code}")
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise McpClientError(f"MCP {self.server_label} 返回无效 JSON: {line[:200]}") from exc
        if data.get("error"):
            error = data["error"]
            raise McpClientError(
                f"MCP {self.server_label} {method} 失败: "
                f"{error.get('message') or json.dumps(error, ensure_ascii=False)}"
            )
        result = data.get("result", {})
        return result if isinstance(result, dict) else {"content": result}

    def _notify(self, method: str) -> None:
        if self._process is None or self._process.stdin is None:
            raise McpClientError(f"MCP {self.server_label} stdio 管道不可用")
        self._process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self._process.stdin.flush()


def create_mcp_clients(
    configs: list[dict[str, Any]],
) -> list[HttpMcpClient | SseMcpClient | StdioMcpClient]:
    """Build HTTP or stdio MCP clients from runtime configuration."""
    clients = []
    for config in configs:
        if config.get("transport") == "stdio":
            command = config.get("command")
            if not command:
                continue
            clients.append(
                StdioMcpClient(
                    server_label=str(config.get("server_label") or "mcp"),
                    command=str(command),
                    args=[str(item) for item in config.get("args") or []],
                    env={str(k): str(v) for k, v in (config.get("env") or {}).items()},
                    timeout=float(config.get("timeout") or 30),
                )
            )
            continue
        server_url = config.get("server_url") or config.get("url")
        if not server_url:
            continue
        if config.get("transport") == "sse" or str(server_url).rstrip("/").endswith("/sse"):
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


create_http_mcp_clients = create_mcp_clients
