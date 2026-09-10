"""Behavior tests for OpenAI Responses and Chat Completions agent loops."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.agents.runtime import (
    OpenAIAgentRuntime,
    RuntimeOptions,
    _extract_compare_scope,
    _compact_mcp_result,
    _mcp_scope_restriction,
    agent_tool,
)


@agent_tool(
    name="TestEcho",
    description="Echo a value for runtime state-machine tests.",
    input_schema={"value": {"type": "string"}},
)
async def _test_echo(value: str) -> dict[str, str]:
    return {"echo": value}


@agent_tool(
    name="TestBoom",
    description="Raise an error for runtime recovery tests.",
    input_schema={},
)
async def _test_boom() -> None:
    raise RuntimeError("boom")


def test_compact_mcp_result_marks_large_text():
    result = {"content": [{"type": "text", "text": "x" * 1000}]}

    compacted = _compact_mcp_result("mcp__yunxiao__get_file_blobs", result, max_chars=100)

    assert compacted["truncated"] is True
    assert compacted["original_chars"] > compacted["kept_chars"]
    assert "内容已截断" in compacted["content"][0]["text"]


def test_extract_compare_scope_indexes_added_lines():
    result = {
        "content": [{
            "type": "text",
            "text": '{"diffs":[{"newPath":"app/example.php","diff":"--- a/app/example.php\\n+++ b/app/example.php\\n@@ -10,2 +10,3 @@\\n old\\n+new_a\\n+new_b\\n"}]}',
        }]
    }

    assert _extract_compare_scope(result) == {
        "files": {"app/example.php": {"added_line_ranges": ["11-12"], "deleted": False}}
    }


def test_mcp_scope_blocks_unmodified_file_reads():
    assert _mcp_scope_restriction(
        "mcp__yunxiao__get_file_blobs",
        {"filePath": "app/old.php"},
        changed_files={"app/changed.php"},
        read_file_paths=set(),
        context_chars=0,
        context_limit=80000,
    ) == "文件 app/old.php 不在本次 MR diff 的变更文件集合中，禁止读取。"


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


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['responses', 'chat_completions'])
async def test_mr_guard_blocks_historical_comment_and_final_report(mode):
    invalid = ('## 🤖 AI 代码审查报告\n#### 1. [问题] `history.php:11`\n'
               '- 变更证据：new `new`\n### 📊 审查结论\nHigh 1')
    calls = [
        ('mcp__yunxiao__compare', {'straight': 'true'}),
        ('mcp__yunxiao__create_change_request_comment', {'content': invalid}),
    ]
    if mode == 'responses':
        outputs = []
        for i, (name, args) in enumerate(calls):
            payload = dict(type='function_call', call_id=str(i), name=name,
                           arguments=json.dumps(args))
            outputs.append(SimpleNamespace(output=[_ResponseItem(**payload, serialized=payload)], output_text=''))
        outputs.append(SimpleNamespace(output=[], output_text=invalid))
        create = Mock(side_effect=outputs)
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
    else:
        outputs = []
        for i, (name, args) in enumerate(calls):
            call = SimpleNamespace(id=str(i), function=SimpleNamespace(name=name, arguments=json.dumps(args)))
            outputs.append(SimpleNamespace(choices=[SimpleNamespace(message=_ChatMessage(content=None, tool_calls=[call]))]))
        outputs.append(SimpleNamespace(choices=[SimpleNamespace(message=_ChatMessage(content=invalid, tool_calls=[]))]))
        create = Mock(side_effect=outputs)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    runtime = OpenAIAgentRuntime(model='test-model')
    runtime.api_mode = mode
    runtime._client = client
    runtime._load_mcp_tools = AsyncMock(return_value={})
    runtime._call_tool = AsyncMock(return_value={'content': [{'type': 'text', 'text': json.dumps({
        'diffs': [{'newPath': 'a.php', 'diff': '@@ -10,2 +10,2 @@\n unchanged\n-old\n+new'}]
    })}]})
    messages = await runtime.run('review', RuntimeOptions(
        allowed_tools=[name for name, _ in calls], max_turns=3, enforce_mr_review=True,
    ))
    assert runtime._call_tool.await_count == 1
    assert runtime._call_tool.call_args.args[1]['straight'] == 'false'
    assert messages[-1]['subtype'] == 'review_scope_error'
    assert messages[-1]['is_error'] is True
    assert invalid not in str(messages)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['responses', 'chat_completions'])
async def test_mr_guard_allows_corrected_comment_after_local_rejection(mode):
    invalid = ('## 🤖 AI 代码审查报告\n#### 1. [问题] `a.php:12`\n'
               '- 变更证据：new `new`\n### 📊 审查结论\nHigh 1')
    valid = ('## 🤖 AI 代码审查报告\n#### 1. [问题] `a.php:11`\n'
             '- 变更证据：new `new`\n### 📊 审查结论\nHigh 1')
    calls = [
        ('mcp__yunxiao__compare', {'straight': 'true'}),
        ('mcp__yunxiao__create_change_request_comment', {'content': invalid}),
        ('mcp__yunxiao__create_change_request_comment', {'content': valid}),
    ]
    if mode == 'responses':
        outputs = []
        for i, (name, args) in enumerate(calls):
            payload = dict(type='function_call', call_id=str(i), name=name,
                           arguments=json.dumps(args))
            outputs.append(SimpleNamespace(output=[_ResponseItem(**payload, serialized=payload)], output_text=''))
        outputs.append(SimpleNamespace(output=[_message(valid)], output_text=valid))
        create = Mock(side_effect=outputs)
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
    else:
        outputs = []
        for i, (name, args) in enumerate(calls):
            call = SimpleNamespace(id=str(i), function=SimpleNamespace(name=name, arguments=json.dumps(args)))
            outputs.append(SimpleNamespace(choices=[SimpleNamespace(message=_ChatMessage(content=None, tool_calls=[call]))]))
        outputs.append(SimpleNamespace(choices=[SimpleNamespace(message=_ChatMessage(content=valid, tool_calls=[]))]))
        create = Mock(side_effect=outputs)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    runtime = OpenAIAgentRuntime(model='test-model')
    runtime.api_mode = mode
    runtime._client = client
    runtime._load_mcp_tools = AsyncMock(return_value={})
    compare_result = {'content': [{'type': 'text', 'text': json.dumps({
        'diffs': [{'newPath': 'a.php', 'diff': '@@ -10,2 +10,2 @@\n unchanged\n-old\n+new'}]
    })}]}
    runtime._call_tool = AsyncMock(side_effect=[compare_result, {'content': [{'type': 'text', 'text': '{"id": "123"}'}]}])

    messages = await runtime.run('review', RuntimeOptions(
        allowed_tools=[name for name, _ in calls], max_turns=4, enforce_mr_review=True,
    ))

    assert runtime._call_tool.await_count == 2
    assert runtime._call_tool.call_args_list[1].args[0] == 'mcp__yunxiao__create_change_request_comment'
    assert runtime._call_tool.call_args_list[1].args[1]['content'] == valid
    assert messages[-1]['subtype'] == 'success'
    assert messages[-1]['content'] == valid


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


@pytest.mark.asyncio
@pytest.mark.parametrize("api_mode", ["responses", "chat"])
async def test_openai_modes_report_empty_final_output_as_incomplete(api_mode: str):
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime.api_mode = api_mode
    if api_mode == "responses":
        runtime._client = SimpleNamespace(
            responses=SimpleNamespace(
                create=Mock(return_value=SimpleNamespace(output=[], output_text=""))
            )
        )
    else:
        runtime._client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=Mock(
                        return_value=SimpleNamespace(
                            choices=[SimpleNamespace(message=_ChatMessage(content=None, tool_calls=[]))]
                        )
                    )
                )
            )
        )

    messages = await runtime.run("review", RuntimeOptions(max_turns=1))

    assert messages[-1] == {
        "type": "result",
        "subtype": "incomplete",
        "is_error": True,
        "content": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("api_mode", ["responses", "chat"])
@pytest.mark.parametrize(
    ("tool_name", "expected_error"),
    [("UnknownTool", "未知工具"), ("TestBoom", "boom")],
)
async def test_openai_modes_mark_unrecovered_tool_errors(
    api_mode: str,
    tool_name: str,
    expected_error: str,
):
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime.api_mode = api_mode
    if api_mode == "responses":
        call = _function_call("call-error", "A")
        call.name = tool_name
        call.arguments = "{}"
        call.serialized["name"] = tool_name
        call.serialized["arguments"] = "{}"
        create = Mock(
            side_effect=[
                SimpleNamespace(output=[call], output_text=""),
                SimpleNamespace(output=[_message("recovered")], output_text="recovered"),
            ]
        )
        runtime._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    else:
        tool_call = _chat_tool_call("call-error", "A")
        tool_call.function.name = tool_name
        tool_call.function.arguments = "{}"
        create = Mock(
            side_effect=[
                SimpleNamespace(
                    choices=[SimpleNamespace(message=_ChatMessage(content=None, tool_calls=[tool_call]))]
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(message=_ChatMessage(content="recovered", tool_calls=[]))]
                ),
            ]
        )
        runtime._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

    messages = await runtime.run(
        "review",
        RuntimeOptions(allowed_tools=[tool_name], max_turns=2),
    )

    assert messages[-1]["subtype"] == "tool_error"
    assert messages[-1]["is_error"] is True
    request = create.call_args_list[1].kwargs
    transcript = request.get("input") or request.get("messages")
    assert expected_error in str(transcript)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_mode", ["responses", "chat"])
async def test_openai_modes_clear_tool_error_after_same_tool_succeeds(api_mode: str):
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime.api_mode = api_mode
    runtime._call_tool = AsyncMock(side_effect=[{"error": "temporary"}, {"ok": True}])  # noqa: SLF001
    if api_mode == "responses":
        create = Mock(
            side_effect=[
                SimpleNamespace(output=[_function_call("call-a", "A")], output_text=""),
                SimpleNamespace(output=[_function_call("call-b", "A")], output_text=""),
                SimpleNamespace(output=[_message("recovered")], output_text="recovered"),
            ]
        )
        runtime._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    else:
        create = Mock(
            side_effect=[
                SimpleNamespace(
                    choices=[SimpleNamespace(message=_ChatMessage(content=None, tool_calls=[_chat_tool_call("call-a", "A")]))]
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(message=_ChatMessage(content=None, tool_calls=[_chat_tool_call("call-b", "A")]))]
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(message=_ChatMessage(content="recovered", tool_calls=[]))]
                ),
            ]
        )
        runtime._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

    messages = await runtime.run(
        "review",
        RuntimeOptions(allowed_tools=["TestEcho"], max_turns=3),
    )

    assert messages[-1]["subtype"] == "success"
    assert runtime._call_tool.await_count == 2  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("api_mode", ["responses", "chat"])
async def test_openai_modes_do_not_clear_failure_for_different_tool_arguments(api_mode: str):
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime.api_mode = api_mode
    runtime._call_tool = AsyncMock(side_effect=[{"error": "A failed"}, {"ok": True}])  # noqa: SLF001
    if api_mode == "responses":
        runtime._client = SimpleNamespace(
            responses=SimpleNamespace(
                create=Mock(
                    side_effect=[
                        SimpleNamespace(output=[_function_call("call-a", "A")], output_text=""),
                        SimpleNamespace(output=[_function_call("call-b", "B")], output_text=""),
                        SimpleNamespace(output=[_message("partial")], output_text="partial"),
                    ]
                )
            )
        )
    else:
        runtime._client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=Mock(
                        side_effect=[
                            SimpleNamespace(
                                choices=[SimpleNamespace(message=_ChatMessage(content=None, tool_calls=[_chat_tool_call("call-a", "A")]))]
                            ),
                            SimpleNamespace(
                                choices=[SimpleNamespace(message=_ChatMessage(content=None, tool_calls=[_chat_tool_call("call-b", "B")]))]
                            ),
                            SimpleNamespace(
                                choices=[SimpleNamespace(message=_ChatMessage(content="partial", tool_calls=[]))]
                            ),
                        ]
                    )
                )
            )
        )

    messages = await runtime.run("review", RuntimeOptions(allowed_tools=["TestEcho"], max_turns=3))

    assert messages[-1]["subtype"] == "tool_error"
    assert "A failed" in str(messages[-1]["errors"])


@pytest.mark.asyncio
@pytest.mark.parametrize("api_mode", ["responses", "chat"])
async def test_openai_modes_treat_mcp_is_error_result_as_tool_error(api_mode: str):
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime.api_mode = api_mode
    runtime._call_tool = AsyncMock(  # noqa: SLF001
        return_value={"isError": True, "content": [{"type": "text", "text": "MCP denied"}]}
    )
    if api_mode == "responses":
        runtime._client = SimpleNamespace(
            responses=SimpleNamespace(
                create=Mock(
                    side_effect=[
                        SimpleNamespace(output=[_function_call("call-a", "A")], output_text=""),
                        SimpleNamespace(output=[_message("cannot review")], output_text="cannot review"),
                    ]
                )
            )
        )
    else:
        runtime._client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=Mock(
                        side_effect=[
                            SimpleNamespace(
                                choices=[SimpleNamespace(message=_ChatMessage(content=None, tool_calls=[_chat_tool_call("call-a", "A")]))]
                            ),
                            SimpleNamespace(
                                choices=[SimpleNamespace(message=_ChatMessage(content="cannot review", tool_calls=[]))]
                            ),
                        ]
                    )
                )
            )
        )

    messages = await runtime.run("review", RuntimeOptions(allowed_tools=["TestEcho"], max_turns=2))

    assert messages[-1]["subtype"] == "tool_error"
    assert "MCP denied" in str(messages[-1]["errors"])


@pytest.mark.asyncio
async def test_agent_tool_converts_subagent_exception_to_tool_error():
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime._call_subagent = AsyncMock(side_effect=RuntimeError("subagent crashed"))  # noqa: SLF001

    result = await runtime._call_tool(  # noqa: SLF001
        "Agent",
        {"subagent_type": "quality", "prompt": "review"},
        cwd=".",
        agents={"quality": SimpleNamespace()},
    )

    assert result == {"error": "子 Agent 执行失败: subagent crashed"}


@pytest.mark.asyncio
async def test_subagent_error_result_is_returned_as_tool_error():
    runtime = OpenAIAgentRuntime(model="test-model")
    runtime.run = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {"type": "assistant", "content": ["partial"]},
            {"type": "result", "subtype": "max_turns", "is_error": True},
        ]
    )

    result = await runtime._call_subagent(  # noqa: SLF001
        {"subagent_type": "quality", "prompt": "review"},
        cwd=".",
        hooks={},
        agents={"quality": SimpleNamespace(tools=())},
        max_turns=3,
    )

    assert result["subtype"] == "max_turns"
    assert "error" in result
