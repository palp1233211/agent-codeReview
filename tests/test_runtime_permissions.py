"""Hook matcher and tool-policy tests for provider-neutral runtimes."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.agents.mcp_client import McpTool
from src.agents.runtime import (
    AgentSpec,
    ClaudeAgentRuntime,
    OpenAIAgentRuntime,
    RuntimeOptions,
    glob_tool,
    grep_tool,
    _matching_hooks,
    _tool_is_allowed,
    agent_tool,
    read_file_tool,
)


_POLICY_CALLS: list[str] = []


@agent_tool("PolicyProbe", "Record policy-test execution.", {})
async def _policy_probe():
    _POLICY_CALLS.append("called")
    return {"ok": True}


@pytest.mark.parametrize(
    ("allowed", "disallowed", "name", "expected"),
    [
        ([], [], "PolicyProbe", True),
        ([], ["PolicyProbe"], "PolicyProbe", False),
        (["PolicyProbe"], [], "PolicyProbe", True),
        (["Other"], [], "PolicyProbe", False),
        (["PolicyProbe"], ["PolicyProbe"], "PolicyProbe", False),
    ],
)
def test_tool_policy_contract(allowed, disallowed, name, expected):
    assert _tool_is_allowed(name, allowed, disallowed) is expected


def test_hook_matcher_supports_exact_wildcard_and_case_sensitive_patterns():
    exact = AsyncMock()
    wildcard = AsyncMock()
    global_hook = AsyncMock()
    hooks = {
        "PreToolUse": [
            {"matcher": "Read", "hooks": [exact]},
            {"matcher": "mcp__yunxiao__*", "hooks": [wildcard]},
            {"hooks": [global_hook]},
        ]
    }

    assert _matching_hooks("PreToolUse", "Read", hooks) == [exact, global_hook]
    assert _matching_hooks("PreToolUse", "mcp__yunxiao__compare", hooks) == [wildcard, global_hook]
    assert _matching_hooks("PreToolUse", "MCP__YUNXIAO__compare", hooks) == [global_hook]


@pytest.mark.asyncio
async def test_mcp_wildcard_pre_hook_can_deny_execution():
    deny = AsyncMock(
        return_value={
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "blocked",
            }
        }
    )
    client = Mock()
    tool = McpTool(
        exposed_name="mcp__yunxiao__compare",
        server_name="compare",
        description="Compare",
        input_schema={"type": "object", "properties": {}},
    )
    runtime = OpenAIAgentRuntime(model="test")

    result = await runtime._call_tool(  # noqa: SLF001
        tool.exposed_name,
        {},
        cwd=".",
        hooks={"PreToolUse": [{"matcher": "mcp__yunxiao__*", "hooks": [deny]}]},
        mcp_tools={tool.exposed_name: ({}, client, tool)},
    )

    assert result == {"error": "blocked"}
    client.call_tool.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("api_mode", ["responses", "chat"])
async def test_openai_modes_hide_disallowed_tools_from_schema(api_mode: str):
    runtime = OpenAIAgentRuntime(model="test")
    runtime.api_mode = api_mode
    if api_mode == "responses":
        create = Mock(return_value=SimpleNamespace(output=[], output_text=""))
        runtime._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    else:
        message = SimpleNamespace(content=None, tool_calls=[])
        create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=message)]))
        runtime._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    await runtime.run(
        "test",
        RuntimeOptions(disallowed_tools=["PolicyProbe"], max_turns=1),
    )

    schemas = create.call_args.kwargs["tools"] or []
    names = [
        schema.get("name") or schema.get("function", {}).get("name")
        for schema in schemas
    ]
    assert "PolicyProbe" not in names


@pytest.mark.asyncio
async def test_execution_guard_rejects_hallucinated_disallowed_tool():
    _POLICY_CALLS.clear()
    runtime = OpenAIAgentRuntime(model="test")

    result = await runtime._call_tool(  # noqa: SLF001
        "PolicyProbe",
        {},
        cwd=".",
        allowed_tools=["PolicyProbe"],
        disallowed_tools=["PolicyProbe"],
    )

    assert result == {"error": "工具未获准执行: PolicyProbe"}
    assert _POLICY_CALLS == []


@pytest.mark.asyncio
async def test_mcp_import_applies_disallowed_tools(monkeypatch):
    client = Mock()
    client.server_label = "yunxiao"
    denied = McpTool("mcp__yunxiao__denied", "denied", "Denied", {})
    allowed = McpTool("mcp__yunxiao__allowed", "allowed", "Allowed", {})
    client.list_tools.return_value = [denied, allowed]
    monkeypatch.setattr("src.agents.runtime.create_http_mcp_clients", lambda _configs: [client])
    runtime = OpenAIAgentRuntime(model="test")

    imported = await runtime._load_mcp_tools(  # noqa: SLF001
        RuntimeOptions(remote_mcp_servers=[{"server_url": "https://example.test/mcp"}]),
        set(),
        disallowed={denied.exposed_name},
    )

    assert set(imported) == {allowed.exposed_name}


@pytest.mark.asyncio
async def test_subagent_inherits_parent_disallowed_tools():
    runtime = OpenAIAgentRuntime(model="test")
    runtime.run = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {"type": "assistant", "content": ["done"]},
            {"type": "result", "subtype": "success"},
        ]
    )
    spec = AgentSpec(description="Quality", prompt="Review", tools=("Read", "PolicyProbe"))

    await runtime._call_subagent(  # noqa: SLF001
        {"subagent_type": "quality", "prompt": "review"},
        cwd=".",
        hooks={},
        agents={"quality": spec},
        max_turns=3,
        disallowed_tools=["PolicyProbe"],
    )

    nested_options = runtime.run.await_args.args[1]  # type: ignore[attr-defined]
    assert nested_options.disallowed_tools == ["PolicyProbe"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disallowed", "expected_agent_tools"),
    [
        ([], ["Read", "PolicyProbe"]),
        (["PolicyProbe"], ["Read"]),
    ],
)
async def test_claude_options_filter_agent_tools_and_pass_disallowed(
    monkeypatch,
    disallowed,
    expected_agent_tools,
):
    captured = {}

    async def fake_query(*, prompt, options):
        captured["prompt"] = prompt
        captured["options"] = options
        if False:
            yield None

    monkeypatch.setattr("claude_agent_sdk.query", fake_query)
    runtime = ClaudeAgentRuntime()

    await runtime.run(
        "test",
        RuntimeOptions(
            allowed_tools=["Agent"],
            disallowed_tools=disallowed,
            agents={
                "quality": AgentSpec(
                    description="Quality",
                    prompt="Review",
                    tools=("Read", "PolicyProbe"),
                )
            },
        ),
    )

    options = captured["options"]
    agent = options.agents["quality"]
    assert agent.tools == expected_agent_tools
    assert agent.disallowedTools == (disallowed or None)
    assert "PolicyProbe" not in options.tools
    assert options.allowed_tools == ["Agent"]
    assert options.disallowed_tools == disallowed


@pytest.mark.asyncio
async def test_claude_local_tools_permission_normalizes_mcp_tool_names(monkeypatch):
    captured = {}

    async def fake_query(*, prompt, options):
        captured["options"] = options
        if False:
            yield None

    monkeypatch.setattr("claude_agent_sdk.query", fake_query)
    runtime = ClaudeAgentRuntime()

    await runtime.run(
        "test",
        RuntimeOptions(allowed_tools=["PolicyProbe"], disallowed_tools=[]),
    )

    allow_result = await captured["options"].can_use_tool(
        "mcp__local-tools__PolicyProbe",
        {},
        None,
    )
    assert type(allow_result).__name__ == "PermissionResultAllow"

    await runtime.run(
        "test",
        RuntimeOptions(allowed_tools=[], disallowed_tools=["PolicyProbe"]),
    )

    deny_result = await captured["options"].can_use_tool(
        "mcp__local-tools__PolicyProbe",
        {},
        None,
    )
    assert type(deny_result).__name__ == "PermissionResultDeny"


@pytest.mark.asyncio
async def test_read_rejects_absolute_path_outside_cwd(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    result = await read_file_tool(str(outside), cwd=str(root))

    assert result == {"error": f"路径越界: {outside}"}


@pytest.mark.asyncio
async def test_read_rejects_parent_traversal_outside_cwd(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    result = await read_file_tool("../secret.txt", cwd=str(root))

    assert result == {"error": "路径越界: ../secret.txt"}


@pytest.mark.asyncio
async def test_read_rejects_symlink_target_outside_cwd(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "linked-secret.txt").symlink_to(outside)

    result = await read_file_tool("linked-secret.txt", cwd=str(root))

    assert result == {"error": "路径越界: linked-secret.txt"}


@pytest.mark.asyncio
async def test_grep_rejects_absolute_path_outside_cwd(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("needle", encoding="utf-8")

    result = await grep_tool("needle", str(outside), cwd=str(root))

    assert result == {"error": f"路径越界: {outside}"}


@pytest.mark.asyncio
async def test_grep_rejects_parent_traversal_outside_cwd(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("needle", encoding="utf-8")

    result = await grep_tool("needle", "../secret.txt", cwd=str(root))

    assert result == {"error": "路径越界: ../secret.txt"}


@pytest.mark.asyncio
async def test_grep_rejects_symlink_target_outside_cwd(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("needle", encoding="utf-8")
    (root / "linked-secret.txt").symlink_to(outside)

    result = await grep_tool("needle", "linked-secret.txt", cwd=str(root))

    assert result == {"error": "路径越界: linked-secret.txt"}


@pytest.mark.asyncio
async def test_glob_rejects_absolute_and_parent_traversal_patterns(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    absolute_result = await glob_tool(str(tmp_path / "*.txt"), cwd=str(root))
    traversal_result = await glob_tool("../*.txt", cwd=str(root))

    assert absolute_result == {"error": f"路径越界: {tmp_path / '*.txt'}"}
    assert traversal_result == {"error": "路径越界: ../*.txt"}


@pytest.mark.asyncio
async def test_glob_filters_symlink_targets_outside_cwd(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "visible.txt").write_text("visible", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "linked-outside").symlink_to(outside)

    result = await glob_tool("**/*.txt", cwd=str(root))

    assert result["matches"] == ["visible.txt"]
