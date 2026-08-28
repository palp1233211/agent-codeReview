"""Behavior tests for OpenAI Responses and Chat Completions agent loops."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.agents.runtime import OpenAIAgentRuntime, RuntimeOptions, agent_tool


@agent_tool(
    name="TestEcho",
    description="Echo a value for runtime state-machine tests.",
    input_schema={"value": {"type": "string"}},
)
async def _test_echo(value: str) -> dict[str, str]:
    return {"echo": value}


class _ResponseItem(SimpleNamespace):
    def model_dump(self, exclude_none: bool = True) -> dict:
        del exclude_none
        return dict(self.serialized)


class _ChatMessage(SimpleNamespace):
    def model_dump(self, exclude_none: bool = True) -> dict:
        del exclude_none
        payload = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in self.tool_calls
            ]
        return payload


def _function_call(call_id: str, value: str) -> _ResponseItem:
    serialized = {
        "type": "function_call",
        "call_id": call_id,
        "name": "TestEcho",
        "arguments": f'{{"value":"{value}"}}',
    }
    return _ResponseItem(**serialized, serialized=serialized)


def _message(text: str) -> _ResponseItem:
    content = [SimpleNamespace(text=text)]
    return _ResponseItem(
        type="message",
        content=content,
        serialized={
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    )


def _chat_tool_call(call_id: str, value: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name="TestEcho", arguments=f'{{"value":"{value}"}}'),
    )


@pytest.mark.asyncio
async def test_responses_preserves_transcript_across_two_tool_rounds():
    create = Mock(
        side_effect=[
            SimpleNamespace(output=[_function_call("call-a", "A")], output_text=""),
            SimpleNamespace(output=[_function_call("call-b", "B")], output_text=""),
            SimpleNamespace(output=[_message("done")], output_text="done"),
        ]
    )
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime.api_mode = "responses"
    runtime._client = SimpleNamespace(responses=SimpleNamespace(create=create))

    messages = await runtime.run(
        "review",
        RuntimeOptions(allowed_tools=["TestEcho"], max_turns=3),
    )

    assert messages[-1] == {"type": "result", "subtype": "success", "content": "done"}
    assert [message["tool"] for message in messages if message["type"] == "tool_use"] == [
        "TestEcho",
        "TestEcho",
    ]
    second_input = create.call_args_list[1].kwargs["input"]
    third_input = create.call_args_list[2].kwargs["input"]
    assert second_input[-1] == {
        "type": "function_call_output",
        "call_id": "call-a",
        "output": '{"echo": "A"}',
    }
    assert [
        (item["type"], item["call_id"])
        for item in third_input
        if isinstance(item, dict) and item.get("call_id")
    ] == [
        ("function_call", "call-a"),
        ("function_call_output", "call-a"),
        ("function_call", "call-b"),
        ("function_call_output", "call-b"),
    ]
    assert third_input[-1]["output"] == '{"echo": "B"}'


@pytest.mark.asyncio
async def test_chat_completions_preserves_history_across_two_tool_rounds():
    create = Mock(
        side_effect=[
            SimpleNamespace(
                choices=[SimpleNamespace(message=_ChatMessage(content=None, tool_calls=[_chat_tool_call("call-a", "A")]))]
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(message=_ChatMessage(content=None, tool_calls=[_chat_tool_call("call-b", "B")]))]
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(message=_ChatMessage(content="done", tool_calls=[]))]
            ),
        ]
    )
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime.api_mode = "chat"
    runtime._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    messages = await runtime.run(
        "review",
        RuntimeOptions(allowed_tools=["TestEcho"], max_turns=3),
    )

    assert messages[-1] == {"type": "result", "subtype": "success", "content": "done"}
    history = create.call_args_list[2].kwargs["messages"]
    assert [item["role"] for item in history] == ["user", "assistant", "tool", "assistant", "tool"]
    assert [item["tool_call_id"] for item in history if item["role"] == "tool"] == ["call-a", "call-b"]
    assert history[-1]["content"] == '{"echo": "B"}'


@pytest.mark.asyncio
@pytest.mark.parametrize("api_mode", ["responses", "chat"])
async def test_openai_modes_report_max_turns_as_error(api_mode: str):
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime.api_mode = api_mode
    if api_mode == "responses":
        create = Mock(return_value=SimpleNamespace(output=[_function_call("call-a", "A")], output_text=""))
        runtime._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    else:
        create = Mock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=_ChatMessage(content=None, tool_calls=[_chat_tool_call("call-a", "A")]))]
            )
        )
        runtime._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    messages = await runtime.run(
        "review",
        RuntimeOptions(allowed_tools=["TestEcho"], max_turns=1),
    )

    assert messages[-1] == {
        "type": "result",
        "subtype": "max_turns",
        "is_error": True,
        "content": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("api_mode", ["responses", "chat"])
async def test_openai_modes_close_mcp_clients_after_success(monkeypatch, api_mode: str):
    mcp_client = Mock()
    mcp_client.server_label = "test-mcp"
    mcp_client.list_tools.return_value = []
    monkeypatch.setattr(
        "src.agents.runtime.create_http_mcp_clients",
        lambda _configs: [mcp_client],
    )
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime.api_mode = api_mode
    if api_mode == "responses":
        runtime._client = SimpleNamespace(
            responses=SimpleNamespace(
                create=Mock(return_value=SimpleNamespace(output=[_message("done")], output_text="done"))
            )
        )
    else:
        runtime._client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=Mock(
                        return_value=SimpleNamespace(
                            choices=[SimpleNamespace(message=_ChatMessage(content="done", tool_calls=[]))]
                        )
                    )
                )
            )
        )

    await runtime.run(
        "review",
        RuntimeOptions(remote_mcp_servers=[{"server_url": "https://example.test/mcp"}]),
    )

    mcp_client.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_openai_runtime_closes_mcp_client_when_tool_loading_fails(monkeypatch):
    mcp_client = Mock()
    mcp_client.server_label = "broken-mcp"
    mcp_client.list_tools.side_effect = RuntimeError("tools/list failed")
    monkeypatch.setattr(
        "src.agents.runtime.create_http_mcp_clients",
        lambda _configs: [mcp_client],
    )
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime.api_mode = "responses"

    with pytest.raises(RuntimeError, match="tools/list failed"):
        await runtime.run(
            "review",
            RuntimeOptions(remote_mcp_servers=[{"server_url": "https://example.test/mcp"}]),
        )

    mcp_client.close.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("api_mode", ["responses", "chat"])
async def test_openai_modes_close_mcp_clients_after_max_turns(monkeypatch, api_mode: str):
    mcp_client = Mock()
    mcp_client.server_label = "test-mcp"
    mcp_client.list_tools.return_value = []
    monkeypatch.setattr(
        "src.agents.runtime.create_http_mcp_clients",
        lambda _configs: [mcp_client],
    )
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime.api_mode = api_mode
    if api_mode == "responses":
        runtime._client = SimpleNamespace(
            responses=SimpleNamespace(
                create=Mock(return_value=SimpleNamespace(output=[_function_call("call-a", "A")], output_text=""))
            )
        )
    else:
        runtime._client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=Mock(
                        return_value=SimpleNamespace(
                            choices=[SimpleNamespace(message=_ChatMessage(content=None, tool_calls=[_chat_tool_call("call-a", "A")]))]
                        )
                    )
                )
            )
        )

    messages = await runtime.run(
        "review",
        RuntimeOptions(
            allowed_tools=["TestEcho"],
            max_turns=1,
            remote_mcp_servers=[{"server_url": "https://example.test/mcp"}],
        ),
    )

    assert messages[-1]["subtype"] == "max_turns"
    mcp_client.close.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("api_mode", ["responses", "chat"])
async def test_openai_modes_close_mcp_clients_after_model_error(monkeypatch, api_mode: str):
    mcp_client = Mock()
    mcp_client.server_label = "test-mcp"
    mcp_client.list_tools.return_value = []
    monkeypatch.setattr(
        "src.agents.runtime.create_http_mcp_clients",
        lambda _configs: [mcp_client],
    )
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime.api_mode = api_mode
    failing_create = Mock(side_effect=RuntimeError("model failed"))
    if api_mode == "responses":
        runtime._client = SimpleNamespace(responses=SimpleNamespace(create=failing_create))
    else:
        runtime._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=failing_create))
        )

    with pytest.raises(RuntimeError, match="model failed"):
        await runtime.run(
            "review",
            RuntimeOptions(remote_mcp_servers=[{"server_url": "https://example.test/mcp"}]),
        )

    mcp_client.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_openai_runtime_reports_close_failure_after_success(monkeypatch):
    mcp_client = Mock()
    mcp_client.server_label = "broken-close"
    mcp_client.list_tools.return_value = []
    mcp_client.close.side_effect = RuntimeError("close failed")
    monkeypatch.setattr(
        "src.agents.runtime.create_http_mcp_clients",
        lambda _configs: [mcp_client],
    )
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime.api_mode = "responses"
    runtime._client = SimpleNamespace(
        responses=SimpleNamespace(
            create=Mock(return_value=SimpleNamespace(output=[_message("done")], output_text="done"))
        )
    )

    with pytest.raises(RuntimeError, match="MCP client 关闭失败: close failed"):
        await runtime.run(
            "review",
            RuntimeOptions(remote_mcp_servers=[{"server_url": "https://example.test/mcp"}]),
        )


@pytest.mark.asyncio
async def test_openai_runtime_preserves_primary_error_when_close_also_fails(monkeypatch):
    mcp_client = Mock()
    mcp_client.server_label = "double-failure"
    mcp_client.list_tools.return_value = []
    mcp_client.close.side_effect = RuntimeError("close failed")
    monkeypatch.setattr(
        "src.agents.runtime.create_http_mcp_clients",
        lambda _configs: [mcp_client],
    )
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime.api_mode = "responses"
    runtime._client = SimpleNamespace(
        responses=SimpleNamespace(create=Mock(side_effect=ValueError("primary failed")))
    )

    with pytest.raises(ValueError, match="primary failed"):
        await runtime.run(
            "review",
            RuntimeOptions(remote_mcp_servers=[{"server_url": "https://example.test/mcp"}]),
        )
