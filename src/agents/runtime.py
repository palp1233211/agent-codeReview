"""Provider-neutral agent runtimes for Claude Agent SDK and OpenAI SDK."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from .mcp_client import HttpMcpClient, McpTool, SseMcpClient, create_http_mcp_clients

ToolCallable = Callable[..., Any | Awaitable[Any]]


@dataclass
class AgentSpec:
    """Prompt and tool definition loaded from YAML."""

    description: str
    prompt: str
    tools: tuple[str, ...] = ()


@dataclass
class RuntimeOptions:
    allowed_tools: list[str] = field(default_factory=list)
    agents: dict[str, AgentSpec] = field(default_factory=dict)
    allowed_agents: list[str] = field(default_factory=list)
    hooks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    cwd: str | None = None
    max_turns: int = 20
    remote_mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    claude_mcp_servers: dict[str, Any] = field(default_factory=dict)
    include_default_file_tools: bool = True
    verbose: bool = False
    progress_prefix: str = "agent"
    disallowed_tools: list[str] = field(default_factory=list)


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    func: ToolCallable

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": self.input_schema,
                "additionalProperties": False,
            },
        }


_TOOLS: dict[str, ToolDefinition] = {}


def agent_tool(name: str, description: str, input_schema: dict[str, Any]):
    """Register a Python function as an agent callable tool."""

    def _decorator(func: ToolCallable) -> ToolCallable:
        _TOOLS[name] = ToolDefinition(
            name=name,
            description=description,
            input_schema=input_schema,
            func=func,
        )
        return func

    return _decorator


def get_registered_tool(name: str) -> ToolDefinition | None:
    return _TOOLS.get(name)


def _tool_is_allowed(
    name: str,
    allowed_tools: list[str] | tuple[str, ...],
    disallowed_tools: list[str] | tuple[str, ...],
) -> bool:
    if name in disallowed_tools:
        return False
    return not allowed_tools or name in allowed_tools


_CLAUDE_LOCAL_TOOL_PREFIX = "mcp__local-tools__"


def _claude_local_tool_policy_names(name: str) -> tuple[str, ...]:
    if name.startswith(_CLAUDE_LOCAL_TOOL_PREFIX):
        bare_name = name.removeprefix(_CLAUDE_LOCAL_TOOL_PREFIX)
        return (name, bare_name) if bare_name else (name,)
    if name in _TOOLS and name not in {"Read", "Grep", "Glob"}:
        return (name, f"{_CLAUDE_LOCAL_TOOL_PREFIX}{name}")
    return (name,)


def _tool_policy_allows_any_name(
    names: tuple[str, ...],
    allowed_tools: list[str] | tuple[str, ...],
    disallowed_tools: list[str] | tuple[str, ...],
) -> bool:
    if any(name in disallowed_tools for name in names):
        return False
    return not allowed_tools or any(name in allowed_tools for name in names)


def _progress(options: RuntimeOptions, message: str) -> None:
    _progress_from_values(options.progress_prefix, options.verbose, message)


def _progress_from_values(prefix: str, enabled: bool, message: str) -> None:
    if enabled:
        print(f"🔎 [{prefix}] {message}", flush=True)


def _summarize_tool_args(args: dict[str, Any]) -> str:
    if not args:
        return ""
    safe_keys = (
        "organizationId",
        "repositoryId",
        "localId",
        "from",
        "to",
        "filePath",
        "ref",
        "comment_type",
        "subagent_type",
    )
    summary = {
        key: args.get(key)
        for key in safe_keys
        if key in args and args.get(key) is not None
    }
    if "content" in args:
        summary["content_len"] = len(str(args.get("content") or ""))
    return json.dumps(summary or {"keys": sorted(args)}, ensure_ascii=False)


def _summarize_tool_result(result: Any) -> str:
    if isinstance(result, dict):
        if result.get("error"):
            return f"error={result.get('error')}"
        text = result.get("content", [{}])[0].get("text") if isinstance(result.get("content"), list) else None
        if text:
            return f"text_len={len(str(text))}"
        return f"keys={sorted(result)[:8]}"
    return f"type={type(result).__name__}"


def _tool_operation_key(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)}"


def _tool_error_message(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    if result.get("error"):
        return str(result["error"])
    if not (result.get("isError") or result.get("is_error")):
        return None
    content = result.get("content")
    if isinstance(content, list):
        text_parts = [
            str(item.get("text"))
            for item in content
            if isinstance(item, dict) and item.get("text")
        ]
        if text_parts:
            return "\n".join(text_parts)
    return "工具返回错误状态"


def _safe_path(path: str, cwd: Path) -> Path:
    root = cwd.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"路径越界: {path}") from exc
    return resolved


@agent_tool(
    name="Read",
    description="Read a text file from the current repository. Supports optional line range.",
    input_schema={
        "file_path": {"type": "string", "description": "File path to read"},
        "start_line": {"type": "integer", "description": "Optional 1-based start line"},
        "end_line": {"type": "integer", "description": "Optional 1-based end line"},
    },
)
async def read_file_tool(
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    root = Path(cwd or os.getcwd()).resolve()
    try:
        path = _safe_path(file_path, root)
    except ValueError as exc:
        return {"error": str(exc)}
    if not path.exists() or not path.is_file():
        return {"content": [{"type": "text", "text": f"文件不存在: {file_path}"}]}

    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    selected = lines
    if start_line:
        selected = selected[start_line - 1 :]
    if end_line:
        base = start_line - 1 if start_line else 0
        selected = selected[: max(0, end_line - base)]

    return {
        "content": [{"type": "text", "text": "\n".join(selected)}],
        "total_lines": len(lines),
    }


@agent_tool(
    name="Glob",
    description="Find files in the current repository using a glob pattern.",
    input_schema={
        "pattern": {"type": "string", "description": "Glob pattern, for example src/**/*.py"},
    },
)
async def glob_tool(pattern: str, cwd: str | None = None) -> dict[str, Any]:
    root = Path(cwd or os.getcwd()).resolve()
    pattern_path = Path(pattern)
    if pattern_path.is_absolute() or ".." in pattern_path.parts:
        return {"error": f"路径越界: {pattern}"}

    matches: list[str] = []
    for path in root.glob(pattern):
        try:
            resolved = _safe_path(str(path), root)
        except ValueError:
            continue
        if resolved.is_file():
            matches.append(str(path.relative_to(root)))
    matches.sort()
    return {"content": [{"type": "text", "text": "\n".join(matches[:500])}], "matches": matches}


@agent_tool(
    name="Grep",
    description="Search text in repository files using ripgrep.",
    input_schema={
        "pattern": {"type": "string", "description": "Text or regex to search"},
        "path": {"type": "string", "description": "Optional path to search under"},
    },
)
async def grep_tool(pattern: str, path: str = ".", cwd: str | None = None) -> dict[str, Any]:
    import subprocess

    root = Path(cwd or os.getcwd()).resolve()
    try:
        target = _safe_path(path, root)
    except ValueError as exc:
        return {"error": str(exc)}
    result = subprocess.run(
        ["rg", "--line-number", "--color", "never", pattern, str(target)],
        cwd=root,
        capture_output=True,
        text=True,
    )
    output = result.stdout[:20000] if result.stdout else "未找到匹配。"
    return {"content": [{"type": "text", "text": output}], "returncode": result.returncode}


class OpenAIAgentRuntime:
    """Small agent loop for OpenAI-compatible Responses clients."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ):
        self._api_key = api_key
        self._base_url = base_url
        self._client = None
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-5.4")
        self.api_mode = os.getenv("OPENAI_API_MODE", "responses").lower()

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self._api_key or os.getenv("OPENAI_API_KEY"),
                base_url=self._base_url or os.getenv("OPENAI_BASE_URL") or None,
            )
        return self._client

    async def run(self, prompt: str, options: RuntimeOptions) -> list[dict[str, Any]]:
        if self.api_mode in {"chat", "chat_completions", "chat-completions"}:
            return await self._run_chat_completions(prompt, options)
        return await self._run_responses(prompt, options)

    async def _run_responses(self, prompt: str, options: RuntimeOptions) -> list[dict[str, Any]]:
        mcp_clients: list[HttpMcpClient | SseMcpClient] = []
        primary_error: BaseException | None = None
        try:
            return await self._run_responses_inner(prompt, options, mcp_clients)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            await self._close_mcp_clients(
                mcp_clients,
                suppress_errors=primary_error is not None,
            )

    async def _run_responses_inner(
        self,
        prompt: str,
        options: RuntimeOptions,
        mcp_clients: list[HttpMcpClient | SseMcpClient],
    ) -> list[dict[str, Any]]:
        prompt = await _run_user_prompt_hooks(prompt, options.hooks)
        cwd = options.cwd or os.getcwd()
        allowed = set(options.allowed_tools)
        tools = [
            definition.as_openai_tool()
            for name, definition in _TOOLS.items()
            if _tool_is_allowed(name, options.allowed_tools, options.disallowed_tools)
        ]
        if options.agents and _tool_is_allowed("Agent", options.allowed_tools, options.disallowed_tools):
            tools.append(_openai_agent_tool_schema(options.allowed_agents or sorted(options.agents)))
        mcp_tools = await self._load_mcp_tools(
            options,
            allowed,
            disallowed=set(options.disallowed_tools),
            client_sink=mcp_clients,
        )
        tools.extend(tool_schema for tool_schema, _, _ in mcp_tools.values())
        _progress(
            options,
            f"OpenAI Responses 启动，tools={len(tools)}，mcp_tools={len(mcp_tools)}",
        )

        system_parts = []
        if options.agents:
            for name, spec in options.agents.items():
                system_parts.append(
                    f"## {name}\n{spec.description}\n\n{spec.prompt}\n可用工具: {', '.join(spec.tools)}"
                )
        system_prompt = "\n\n---\n\n".join(system_parts)
        input_items: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        if system_prompt:
            input_items.insert(0, {"role": "system", "content": system_prompt})

        messages: list[dict[str, Any]] = []
        unresolved_tool_errors: dict[str, str] = {}

        for _ in range(options.max_turns):
            response = await asyncio.to_thread(
                self.client.responses.create,
                model=self.model,
                input=input_items,
                tools=tools or None,
            )
            output = list(getattr(response, "output", []) or [])

            tool_outputs: list[dict[str, Any]] = []
            for item in output:
                item_type = getattr(item, "type", None)
                if item_type == "message":
                    text = _message_text(item)
                    if text:
                        messages.append({"type": "assistant", "content": [text]})
                elif item_type == "mcp_list_tools":
                    server_label = getattr(item, "server_label", None)
                    imported_tools = getattr(item, "tools", None) or []
                    _progress(
                        options,
                        f"MCP 工具导入: server={server_label} count={len(imported_tools)}",
                    )
                    messages.append(
                        {
                            "type": "mcp_list_tools",
                            "server_label": server_label,
                            "tools": imported_tools,
                        }
                    )
                elif item_type == "function_call":
                    tool_name = getattr(item, "name", "")
                    args = _loads_json(getattr(item, "arguments", "{}"))
                    _progress(options, f"调用工具: {tool_name} {_summarize_tool_args(args)}")
                    messages.append({"type": "tool_use", "tool": tool_name, "input": args})
                    result = await self._call_tool(
                        tool_name,
                        args,
                        cwd=cwd,
                        hooks=options.hooks,
                        agents=options.agents,
                        max_turns=options.max_turns,
                        verbose=options.verbose,
                        progress_prefix=options.progress_prefix,
                        mcp_tools=mcp_tools,
                        allowed_tools=options.allowed_tools,
                        disallowed_tools=options.disallowed_tools,
                    )
                    operation_key = _tool_operation_key(tool_name, args)
                    tool_error = _tool_error_message(result)
                    if tool_error:
                        unresolved_tool_errors[operation_key] = tool_error
                    else:
                        unresolved_tool_errors.pop(operation_key, None)
                    tool_outputs.append(
                        {
                            "type": "function_call_output",
                            "call_id": getattr(item, "call_id"),
                            "output": json.dumps(result, ensure_ascii=False),
                        }
                    )
                elif item_type in {"mcp_call", "mcp_approval_request"}:
                    tool_name = getattr(item, "name", None)
                    error = getattr(item, "error", None)
                    _progress(options, f"MCP 事件: type={item_type} tool={tool_name} error={error}")
                    messages.append({"type": item_type, "tool": tool_name, "error": error})

            if not tool_outputs:
                _progress(options, "OpenAI Responses 完成，未等待更多工具结果")
                final_text = _response_text(response)
                if unresolved_tool_errors:
                    messages.append(
                        {
                            "type": "result",
                            "subtype": "tool_error",
                            "is_error": True,
                            "content": final_text or None,
                            "errors": dict(unresolved_tool_errors),
                        }
                    )
                elif final_text:
                    messages.append({"type": "result", "subtype": "success", "content": final_text})
                else:
                    messages.append(
                        {
                            "type": "result",
                            "subtype": "incomplete",
                            "is_error": True,
                            "content": None,
                        }
                    )
                return messages

            # Some OpenAI-compatible providers do not retain function calls behind
            # previous_response_id. Re-send the response output with tool results so
            # the call_id is always present in the next request.
            serialized_output = [
                item.model_dump(exclude_none=True)
                if hasattr(item, "model_dump")
                else item
                for item in output
            ]
            input_items = [*input_items, *serialized_output, *tool_outputs]

        messages.append(
            {"type": "result", "subtype": "max_turns", "is_error": True, "content": None}
        )
        return messages

    async def _run_chat_completions(self, prompt: str, options: RuntimeOptions) -> list[dict[str, Any]]:
        mcp_clients: list[HttpMcpClient | SseMcpClient] = []
        primary_error: BaseException | None = None
        try:
            return await self._run_chat_completions_inner(prompt, options, mcp_clients)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            await self._close_mcp_clients(
                mcp_clients,
                suppress_errors=primary_error is not None,
            )

    async def _run_chat_completions_inner(
        self,
        prompt: str,
        options: RuntimeOptions,
        mcp_clients: list[HttpMcpClient | SseMcpClient],
    ) -> list[dict[str, Any]]:
        prompt = await _run_user_prompt_hooks(prompt, options.hooks)
        cwd = options.cwd or os.getcwd()
        allowed = set(options.allowed_tools)
        tools = [
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": {
                        "type": "object",
                        "properties": definition.input_schema,
                        "additionalProperties": False,
                    },
                },
            }
            for name, definition in _TOOLS.items()
            if _tool_is_allowed(name, options.allowed_tools, options.disallowed_tools)
        ]
        if options.agents and _tool_is_allowed("Agent", options.allowed_tools, options.disallowed_tools):
            tools.append({"type": "function", "function": _openai_agent_function_schema(options.allowed_agents or sorted(options.agents))})
        mcp_tools = await self._load_mcp_tools(
            options,
            allowed,
            chat_completions=True,
            disallowed=set(options.disallowed_tools),
            client_sink=mcp_clients,
        )
        tools.extend(tool_schema for tool_schema, _, _ in mcp_tools.values())
        _progress(
            options,
            f"OpenAI Chat Completions 启动，tools={len(tools)}，mcp_tools={len(mcp_tools)}",
        )

        chat_messages: list[dict[str, Any]] = []
        if options.agents:
            system_prompt = "\n\n---\n\n".join(
                f"## {name}\n{spec.description}\n\n{spec.prompt}\n可用工具: {', '.join(spec.tools)}"
                for name, spec in options.agents.items()
            )
            chat_messages.append({"role": "system", "content": system_prompt})
        chat_messages.append({"role": "user", "content": prompt})

        messages: list[dict[str, Any]] = []
        unresolved_tool_errors: dict[str, str] = {}
        for _ in range(options.max_turns):
            response = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=self.model,
                messages=chat_messages,
                tools=tools or None,
            )
            choice = response.choices[0].message
            content = choice.content or ""
            if content:
                messages.append({"type": "assistant", "content": [content]})

            tool_calls = choice.tool_calls or []
            if not tool_calls:
                _progress(options, "OpenAI Chat Completions 完成，未等待更多工具结果")
                if unresolved_tool_errors:
                    messages.append(
                        {
                            "type": "result",
                            "subtype": "tool_error",
                            "is_error": True,
                            "content": content or None,
                            "errors": dict(unresolved_tool_errors),
                        }
                    )
                elif content:
                    messages.append({"type": "result", "subtype": "success", "content": content})
                else:
                    messages.append(
                        {
                            "type": "result",
                            "subtype": "incomplete",
                            "is_error": True,
                            "content": None,
                        }
                    )
                return messages

            chat_messages.append(choice.model_dump(exclude_none=True))
            for call in tool_calls:
                tool_name = call.function.name
                args = _loads_json(call.function.arguments)
                _progress(options, f"调用工具: {tool_name} {_summarize_tool_args(args)}")
                messages.append({"type": "tool_use", "tool": tool_name, "input": args})
                result = await self._call_tool(
                    tool_name,
                    args,
                    cwd=cwd,
                    hooks=options.hooks,
                    agents=options.agents,
                    max_turns=options.max_turns,
                    verbose=options.verbose,
                    progress_prefix=options.progress_prefix,
                    mcp_tools=mcp_tools,
                    allowed_tools=options.allowed_tools,
                    disallowed_tools=options.disallowed_tools,
                )
                operation_key = _tool_operation_key(tool_name, args)
                tool_error = _tool_error_message(result)
                if tool_error:
                    unresolved_tool_errors[operation_key] = tool_error
                else:
                    unresolved_tool_errors.pop(operation_key, None)
                chat_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        messages.append(
            {"type": "result", "subtype": "max_turns", "is_error": True, "content": None}
        )
        return messages

    async def _call_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        cwd: str,
        hooks: dict[str, list[dict[str, Any]]] | None = None,
        agents: dict[str, AgentSpec] | None = None,
        max_turns: int = 20,
        verbose: bool = False,
        progress_prefix: str = "agent",
        mcp_tools: dict[
            str, tuple[dict[str, Any], HttpMcpClient | SseMcpClient, McpTool]
        ] | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
    ) -> Any:
        if not _tool_is_allowed(name, allowed_tools or [], disallowed_tools or []):
            return {"error": f"工具未获准执行: {name}"}
        mcp_entry = (mcp_tools or {}).get(name)
        definition = get_registered_tool(name)
        if name != "Agent" and not mcp_entry and definition is None:
            _progress_from_values(progress_prefix, verbose, f"未知工具: {name}")
            return {"error": f"未知工具: {name}"}

        denied = await _run_permission_hooks(name, args, hooks or {})
        if denied:
            return denied

        denied = await _run_pre_hooks(name, args, hooks or {})
        if denied:
            return denied

        error: Exception | None = None
        result: Any = {}
        if name == "Agent":
            _progress_from_values(progress_prefix, verbose, f"调用子 Agent: {_summarize_tool_args(args)}")
            try:
                result = await self._call_subagent(
                    args,
                    cwd=cwd,
                    hooks=hooks or {},
                    agents=agents or {},
                    max_turns=max_turns,
                    disallowed_tools=disallowed_tools or [],
                )
                return result
            except Exception as exc:
                error = exc
                _progress_from_values(progress_prefix, verbose, f"子 Agent 失败: error={exc}")
                return {"error": f"子 Agent 执行失败: {exc}"}
            finally:
                await _run_post_hooks(name, result, error, hooks or {})

        if mcp_entry:
            _, client, tool = mcp_entry
            try:
                result = await asyncio.to_thread(client.call_tool, tool.server_name, args)
                _progress_from_values(
                    progress_prefix,
                    verbose,
                    f"MCP 工具完成: {name} {_summarize_tool_result(result)}",
                )
                return result
            except Exception as exc:
                error = exc
                _progress_from_values(progress_prefix, verbose, f"MCP 工具失败: {name} error={exc}")
                return {"error": str(exc)}
            finally:
                await _run_post_hooks(name, result, error, hooks or {})

        if "cwd" in inspect.signature(definition.func).parameters:
            args = {**args, "cwd": cwd}

        try:
            result = definition.func(**args)
            if inspect.isawaitable(result):
                result = await result
            _progress_from_values(progress_prefix, verbose, f"工具完成: {name} {_summarize_tool_result(result)}")
            return result
        except Exception as exc:
            error = exc
            _progress_from_values(progress_prefix, verbose, f"工具失败: {name} error={exc}")
            return {"error": str(exc)}
        finally:
            await _run_post_hooks(name, result, error, hooks or {})

    async def _load_mcp_tools(
        self,
        options: RuntimeOptions,
        allowed: set[str],
        *,
        chat_completions: bool = False,
        disallowed: set[str] | None = None,
        client_sink: list[HttpMcpClient | SseMcpClient] | None = None,
    ) -> dict[
        str, tuple[dict[str, Any], HttpMcpClient | SseMcpClient, McpTool]
    ]:
        imported: dict[
            str, tuple[dict[str, Any], HttpMcpClient | SseMcpClient, McpTool]
        ] = {}
        for client in create_http_mcp_clients(options.remote_mcp_servers):
            if client_sink is not None:
                client_sink.append(client)
            tools = await asyncio.to_thread(client.list_tools)
            selected = [
                tool
                for tool in tools
                if (not allowed or tool.exposed_name in allowed)
                and tool.exposed_name not in (disallowed or set())
            ]
            _progress(
                options,
                f"MCP 工具导入: server={client.server_label} count={len(selected)}/{len(tools)}",
            )
            for tool in selected:
                function_schema = {
                    "name": tool.exposed_name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                }
                schema = (
                    {"type": "function", "function": function_schema}
                    if chat_completions
                    else {"type": "function", **function_schema}
                )
                imported[tool.exposed_name] = (schema, client, tool)
        return imported

    @staticmethod
    async def _close_mcp_clients(
        clients: list[HttpMcpClient | SseMcpClient],
        *,
        suppress_errors: bool,
    ) -> None:
        close_errors: list[Exception] = []
        for client in reversed(clients):
            try:
                await asyncio.to_thread(client.close)
            except Exception as exc:
                close_errors.append(exc)
                print(
                    f"⚠️ MCP client 关闭失败: server={client.server_label} error={exc}",
                    file=sys.stderr,
                    flush=True,
                )
        if close_errors and not suppress_errors:
            raise RuntimeError(f"MCP client 关闭失败: {close_errors[0]}") from close_errors[0]

    async def _call_subagent(
        self,
        args: dict[str, Any],
        *,
        cwd: str,
        hooks: dict[str, list[dict[str, Any]]],
        agents: dict[str, AgentSpec],
        max_turns: int,
        disallowed_tools: list[str] | None = None,
    ) -> dict[str, Any]:
        agent_name = (
            args.get("subagent_type")
            or args.get("agent_name")
            or args.get("name")
            or args.get("type")
        )
        prompt = str(args.get("prompt") or args.get("task") or args.get("description") or "")
        if not agent_name or agent_name not in agents:
            return {"error": f"未知 subagent: {agent_name}", "available_agents": sorted(agents)}

        spec = agents[agent_name]
        messages = await self.run(
            prompt,
                RuntimeOptions(
                    allowed_tools=list(spec.tools),
                    agents={agent_name: spec},
                    allowed_agents=[agent_name],
                    hooks=hooks,
                    cwd=cwd,
                    max_turns=max(1, max_turns - 1),
                    disallowed_tools=list(disallowed_tools or []),
                ),
            )
        summary = ""
        for message in reversed(messages):
            if message.get("type") == "assistant":
                summary = "\n".join(message.get("content", []))
                break
        result_message = next(
            (message for message in reversed(messages) if message.get("type") == "result"),
            None,
        )
        if not result_message or result_message.get("is_error"):
            subtype = result_message.get("subtype") if result_message else "incomplete"
            return {
                "error": f"子 Agent {agent_name} 未完成: {subtype}",
                "agent": agent_name,
                "subtype": subtype,
                "raw_messages": messages,
            }
        return {"agent": agent_name, "summary": summary, "raw_messages": messages}


def _loads_json(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _openai_agent_tool_schema(allowed_agents: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        **_openai_agent_function_schema(allowed_agents),
    }


def _openai_agent_function_schema(allowed_agents: list[str]) -> dict[str, Any]:
    return {
        "name": "Agent",
        "description": "Run a configured specialist subagent and return its findings.",
        "parameters": {
            "type": "object",
            "properties": {
                "subagent_type": {
                    "type": "string",
                    "description": "Configured subagent name, for example security-reviewer.",
                    "enum": allowed_agents,
                },
                "prompt": {
                    "type": "string",
                    "description": "Task prompt for the specialist subagent.",
                },
            },
            "required": ["subagent_type", "prompt"],
            "additionalProperties": False,
        },
    }


def _message_text(item: Any) -> str:
    chunks = []
    for content in getattr(item, "content", []) or []:
        text = getattr(content, "text", None)
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def _response_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return text
    chunks = [_message_text(item) for item in getattr(response, "output", []) or []]
    return "\n".join(chunk for chunk in chunks if chunk)


async def _run_user_prompt_hooks(
    prompt: str,
    hooks: dict[str, list[dict[str, Any]]],
) -> str:
    effective_prompt = prompt
    for hook in _matching_hooks("UserPromptSubmit", "", hooks):
        result = await hook({"prompt": effective_prompt}, None, None)
        output = result.get("hookSpecificOutput", {}) if isinstance(result, dict) else {}
        additional_context = output.get("additionalContext")
        if additional_context:
            effective_prompt = f"{effective_prompt}\n\n{additional_context}"
    return effective_prompt


async def _run_permission_hooks(
    tool_name: str,
    tool_input: dict[str, Any],
    hooks: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    for hook in _matching_hooks("PermissionRequest", tool_name, hooks):
        result = await hook({"tool_name": tool_name, "tool_input": tool_input}, None, None)
        output = result.get("hookSpecificOutput", {}) if isinstance(result, dict) else {}
        decision = output.get("decision")
        behavior = decision.get("behavior") if isinstance(decision, dict) else decision
        if behavior == "deny" or output.get("permissionDecision") == "deny":
            reason = (
                output.get("permissionDecisionReason")
                or (decision.get("message") if isinstance(decision, dict) else None)
                or "工具调用被拒绝"
            )
            return {"error": str(reason)}
    return None


async def _run_pre_hooks(
    tool_name: str,
    tool_input: dict[str, Any],
    hooks: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    for hook in _matching_hooks("PreToolUse", tool_name, hooks):
        result = await hook({"tool_name": tool_name, "tool_input": tool_input}, None, None)
        output = result.get("hookSpecificOutput", {}) if isinstance(result, dict) else {}
        if output.get("permissionDecision") == "deny":
            return {"error": output.get("permissionDecisionReason", "工具调用被拒绝")}
    return None


async def _run_post_hooks(
    tool_name: str,
    result: Any,
    error: Exception | None,
    hooks: dict[str, list[dict[str, Any]]],
) -> None:
    for hook in _matching_hooks("PostToolUse", tool_name, hooks):
        await hook(
            {"tool_name": tool_name, "tool_result": result, "error": error},
            None,
            None,
        )


def _matching_hooks(
    event_name: str,
    tool_name: str,
    hooks: dict[str, list[dict[str, Any]]],
) -> list[Callable[..., Awaitable[dict[str, Any]]]]:
    matched = []
    for matcher in hooks.get(event_name, []):
        pattern = matcher.get("matcher")
        if pattern and not fnmatchcase(tool_name, str(pattern)):
            continue
        matched.extend(matcher.get("hooks", []))
    return matched


class ClaudeAgentRuntime:
    """Compatibility runtime that preserves the original Claude Agent SDK behavior."""

    async def run(self, prompt: str, options: RuntimeOptions) -> list[dict[str, Any]]:
        from claude_agent_sdk import (
            AgentDefinition,
            AssistantMessage,
            ClaudeAgentOptions,
            PermissionResultAllow,
            PermissionResultDeny,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            create_sdk_mcp_server,
            query,
            tool,
        )

        messages: list[dict[str, Any]] = []
        effective_allowed_tools = [
            tool_name
            for tool_name in options.allowed_tools
            if _tool_policy_allows_any_name(
                _claude_local_tool_policy_names(tool_name),
                options.allowed_tools,
                options.disallowed_tools,
            )
        ]
        agents = {
            name: AgentDefinition(
                description=spec.description,
                prompt=spec.prompt,
                tools=[
                    tool_name
                    for tool_name in spec.tools
                    if _tool_policy_allows_any_name(
                        _claude_local_tool_policy_names(tool_name),
                        [],
                        options.disallowed_tools,
                    )
                ],
                disallowedTools=list(options.disallowed_tools) or None,
            )
            for name, spec in options.agents.items()
        }

        local_tools = [
            tool(
                name=definition.name,
                description=definition.description,
                input_schema=definition.input_schema,
            )(definition.func)
            for name, definition in _TOOLS.items()
            if name not in {"Read", "Grep", "Glob"}
            and _tool_is_allowed(name, options.allowed_tools, options.disallowed_tools)
        ]
        mcp_servers = dict(options.claude_mcp_servers)
        if local_tools:
            mcp_servers["local-tools"] = create_sdk_mcp_server(
                name="local-tools",
                version="1.0.0",
                tools=local_tools,
            )

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], _context: Any):
            policy_names = _claude_local_tool_policy_names(tool_name)
            if not _tool_policy_allows_any_name(
                policy_names,
                options.allowed_tools,
                options.disallowed_tools,
            ):
                _progress(
                    options,
                    f"拒绝未授权工具: {tool_name} {_summarize_tool_args(tool_input)}",
                )
                return PermissionResultDeny(
                    message=f"Tool {tool_name} is not allowed for this run.",
                    interrupt=False,
                )
            return PermissionResultAllow()

        _progress(
            options,
            f"Claude SDK 启动，allowed_tools={len(effective_allowed_tools)}，mcp_servers={', '.join(sorted(mcp_servers)) or 'none'}",
        )

        claude_options = ClaudeAgentOptions(
            tools=effective_allowed_tools or None,
            allowed_tools=effective_allowed_tools,
            disallowed_tools=options.disallowed_tools,
            permission_mode=os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions"),
            hooks=options.hooks,
            agents=agents,
            mcp_servers=mcp_servers,
            cwd=options.cwd,
            max_turns=options.max_turns,
            env=clean_claude_env(),
            debug_stderr=_NullWriter(),
            stderr=_stderr_logger,
            can_use_tool=can_use_tool,
            setting_sources=[],
        )

        async def prompt_stream():
            yield {
                "type": "user",
                "message": {"role": "user", "content": prompt},
                "parent_tool_use_id": None,
                "session_id": "default",
            }

        try:
            async for message in query(prompt=prompt_stream(), options=claude_options):
                if isinstance(message, AssistantMessage):
                    text_parts = []
                    tool_uses = []
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            _progress(options, f"调用工具: {block.name} {_summarize_tool_args(block.input)}")
                            tool_uses.append(
                                {
                                    "type": "tool_use",
                                    "tool": block.name,
                                    "input": block.input,
                                }
                            )

                    if text_parts:
                        messages.append({"type": "assistant", "content": text_parts})
                    messages.extend(tool_uses)
                elif isinstance(message, ResultMessage):
                    _progress(
                        options,
                        f"Claude SDK 完成，subtype={message.subtype}，is_error={getattr(message, 'is_error', False)}",
                    )
                    messages.append(
                        {
                            "type": "result",
                            "subtype": message.subtype,
                            "content": message.result if hasattr(message, "result") else None,
                            "is_error": getattr(message, "is_error", False),
                        }
                    )
        except Exception as exc:
            if messages:
                if not any(msg.get("type") == "result" for msg in messages):
                    messages.append(
                        {
                            "type": "result",
                            "subtype": "error",
                            "content": str(exc),
                            "is_error": True,
                        }
                    )
                return messages
            raise

        return messages


def create_agent_runtime(provider: str | None = None) -> OpenAIAgentRuntime | ClaudeAgentRuntime:
    selected = (provider or os.getenv("AGENT_PROVIDER") or os.getenv("AGENT_SDK") or "claude").lower()
    if selected in {"openai", "openai_sdk"}:
        return OpenAIAgentRuntime()
    if selected in {"claude", "claude_sdk", "anthropic"}:
        return ClaudeAgentRuntime()
    raise ValueError(f"不支持的 AGENT_PROVIDER: {selected}")


_CLAUDE_CODE_ENV_PREFIXES = ("CLAUDE_", "CLAUDECODE")


def clean_claude_env() -> dict[str, str]:
    """Strip Claude Code session vars before spawning child Claude CLI."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_CLAUDE_CODE_ENV_PREFIXES)
    }


def _stderr_logger(line: str) -> None:
    print(f"[claude-cli-stderr] {line}", file=sys.stderr, flush=True)


class _NullWriter:
    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None
