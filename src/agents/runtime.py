"""Provider-neutral agent runtimes for Claude Agent SDK and OpenAI SDK."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    hooks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    cwd: str | None = None
    max_turns: int = 20
    remote_mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    claude_mcp_servers: dict[str, Any] = field(default_factory=dict)
    include_default_file_tools: bool = True


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


def _safe_path(path: str, cwd: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate.resolve()


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
    path = _safe_path(file_path, root)
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
    matches = sorted(str(path.relative_to(root)) for path in root.glob(pattern) if path.is_file())
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
    target = _safe_path(path, root)
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
        cwd = options.cwd or os.getcwd()
        allowed = set(options.allowed_tools)
        tools = [
            definition.as_openai_tool()
            for name, definition in _TOOLS.items()
            if not allowed or name in allowed
        ]
        if options.agents and (not allowed or "Agent" in allowed):
            tools.append(_openai_agent_tool_schema())
        tools.extend(options.remote_mcp_servers)

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
        previous_response_id: str | None = None

        for _ in range(options.max_turns):
            response = await asyncio.to_thread(
                self.client.responses.create,
                model=self.model,
                input=input_items,
                tools=tools or None,
                previous_response_id=previous_response_id,
            )
            previous_response_id = response.id
            output = list(getattr(response, "output", []) or [])

            tool_outputs: list[dict[str, Any]] = []
            for item in output:
                item_type = getattr(item, "type", None)
                if item_type == "message":
                    text = _message_text(item)
                    if text:
                        messages.append({"type": "assistant", "content": [text]})
                elif item_type == "function_call":
                    tool_name = getattr(item, "name", "")
                    args = _loads_json(getattr(item, "arguments", "{}"))
                    messages.append({"type": "tool_use", "tool": tool_name, "input": args})
                    result = await self._call_tool(
                        tool_name,
                        args,
                        cwd=cwd,
                        hooks=options.hooks,
                        agents=options.agents,
                        max_turns=options.max_turns,
                    )
                    tool_outputs.append(
                        {
                            "type": "function_call_output",
                            "call_id": getattr(item, "call_id"),
                            "output": json.dumps(result, ensure_ascii=False),
                        }
                    )
                elif item_type in {"mcp_call", "mcp_approval_request"}:
                    messages.append({"type": item_type, "tool": getattr(item, "name", None)})

            if not tool_outputs:
                messages.append({"type": "result", "subtype": "success", "content": _response_text(response)})
                return messages

            input_items = tool_outputs

        messages.append({"type": "result", "subtype": "max_turns", "content": None})
        return messages

    async def _run_chat_completions(self, prompt: str, options: RuntimeOptions) -> list[dict[str, Any]]:
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
            if not allowed or name in allowed
        ]
        if options.agents and (not allowed or "Agent" in allowed):
            tools.append({"type": "function", "function": _openai_agent_function_schema()})

        chat_messages: list[dict[str, Any]] = []
        if options.agents:
            system_prompt = "\n\n---\n\n".join(
                f"## {name}\n{spec.description}\n\n{spec.prompt}\n可用工具: {', '.join(spec.tools)}"
                for name, spec in options.agents.items()
            )
            chat_messages.append({"role": "system", "content": system_prompt})
        chat_messages.append({"role": "user", "content": prompt})

        messages: list[dict[str, Any]] = []
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
                messages.append({"type": "result", "subtype": "success", "content": content})
                return messages

            chat_messages.append(choice.model_dump(exclude_none=True))
            for call in tool_calls:
                tool_name = call.function.name
                args = _loads_json(call.function.arguments)
                messages.append({"type": "tool_use", "tool": tool_name, "input": args})
                result = await self._call_tool(
                    tool_name,
                    args,
                    cwd=cwd,
                    hooks=options.hooks,
                    agents=options.agents,
                    max_turns=options.max_turns,
                )
                chat_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        messages.append({"type": "result", "subtype": "max_turns", "content": None})
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
    ) -> Any:
        if name == "Agent":
            return await self._call_subagent(args, cwd=cwd, hooks=hooks or {}, agents=agents or {}, max_turns=max_turns)

        definition = get_registered_tool(name)
        if definition is None:
            return {"error": f"未知工具: {name}"}

        denied = await _run_pre_hooks(name, args, hooks or {})
        if denied:
            return denied

        if "cwd" in inspect.signature(definition.func).parameters:
            args = {**args, "cwd": cwd}

        error: Exception | None = None
        try:
            result = definition.func(**args)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception as exc:
            error = exc
            return {"error": str(exc)}
        finally:
            await _run_post_hooks(name, locals().get("result", {}), error, hooks or {})

    async def _call_subagent(
        self,
        args: dict[str, Any],
        *,
        cwd: str,
        hooks: dict[str, list[dict[str, Any]]],
        agents: dict[str, AgentSpec],
        max_turns: int,
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
                hooks=hooks,
                cwd=cwd,
                max_turns=max(1, max_turns - 1),
            ),
        )
        summary = ""
        for message in reversed(messages):
            if message.get("type") == "assistant":
                summary = "\n".join(message.get("content", []))
                break
        return {"agent": agent_name, "summary": summary, "raw_messages": messages}


def _loads_json(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _openai_agent_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        **_openai_agent_function_schema(),
    }


def _openai_agent_function_schema() -> dict[str, Any]:
    return {
        "name": "Agent",
        "description": "Run a configured specialist subagent and return its findings.",
        "parameters": {
            "type": "object",
            "properties": {
                "subagent_type": {
                    "type": "string",
                    "description": "Configured subagent name, for example security-reviewer.",
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
        if pattern and pattern != tool_name:
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
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            create_sdk_mcp_server,
            query,
            tool,
        )

        messages: list[dict[str, Any]] = []
        agents = {
            name: AgentDefinition(
                description=spec.description,
                prompt=spec.prompt,
                tools=list(spec.tools),
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
            if name not in {"Read", "Grep", "Glob"} and (not options.allowed_tools or name in options.allowed_tools)
        ]
        mcp_servers = dict(options.claude_mcp_servers)
        if local_tools:
            mcp_servers["local-tools"] = create_sdk_mcp_server(
                name="local-tools",
                version="1.0.0",
                tools=local_tools,
            )

        claude_options = ClaudeAgentOptions(
            allowed_tools=options.allowed_tools,
            permission_mode=os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions"),
            hooks=options.hooks,
            agents=agents,
            mcp_servers=mcp_servers,
            cwd=options.cwd,
            max_turns=options.max_turns,
            env=clean_claude_env(),
            debug_stderr=_NullWriter(),
            stderr=_stderr_logger,
        )

        try:
            async for message in query(prompt=prompt, options=claude_options):
                if isinstance(message, AssistantMessage):
                    text_parts = []
                    tool_uses = []
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
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
