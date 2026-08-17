"""DifyClient 会话管理与错误处理测试"""
from unittest.mock import Mock, patch

import pytest

from src.dify.client import DifyClient, strip_think_tags


def _mock_response(status_code: int, payload: dict) -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = payload
    return resp


@patch("src.dify.client.requests.post")
def test_chat_returns_answer_and_starts_conversation(mock_post):
    mock_post.return_value = _mock_response(
        200, {"answer": "hello back", "conversation_id": "conv-1"}
    )
    client = DifyClient(base_url="http://localhost/v1", api_key="app-test")

    reply = client.chat("user-1", "hello")

    assert reply.answer == "hello back"
    assert reply.conversation_id == "conv-1"
    sent_body = mock_post.call_args.kwargs["json"]
    assert sent_body["conversation_id"] == ""
    assert sent_body["user"] == "user-1"


@patch("src.dify.client.requests.post")
def test_chat_reuses_conversation_id_for_same_user(mock_post):
    mock_post.return_value = _mock_response(
        200, {"answer": "turn 1", "conversation_id": "conv-1"}
    )
    client = DifyClient(base_url="http://localhost/v1", api_key="app-test")
    client.chat("user-1", "first message")

    mock_post.return_value = _mock_response(200, {"answer": "turn 2"})
    second_reply = client.chat("user-1", "second message")

    second_call_body = mock_post.call_args.kwargs["json"]
    assert second_call_body["conversation_id"] == "conv-1"
    # 第二轮响应没带 conversation_id，reply 里应该沿用第一轮的
    assert second_reply.conversation_id == "conv-1"


@patch("src.dify.client.requests.post")
def test_chat_raises_runtime_error_on_non_200(mock_post):
    mock_post.return_value = _mock_response(
        400, {"code": "invalid_param", "message": "Workflow not published"}
    )
    client = DifyClient(base_url="http://localhost/v1", api_key="app-test")

    with pytest.raises(RuntimeError, match="Workflow not published"):
        client.chat("user-1", "hello")


@patch("src.dify.client.requests.post")
def test_chat_strips_think_tags_from_answer(mock_post):
    mock_post.return_value = _mock_response(
        200,
        {
            "answer": "<think>先分析一下用户意图...\n再想想</think>这是最终回复",
            "conversation_id": "conv-1",
        },
    )
    client = DifyClient(base_url="http://localhost/v1", api_key="app-test")

    assert client.chat("user-1", "hello").answer == "这是最终回复"


def test_strip_think_tags_removes_multiple_blocks():
    text = "<think>a</think>正文一<think>b</think>正文二"
    assert strip_think_tags(text) == "正文一正文二"


def test_strip_think_tags_returns_input_unchanged_when_no_tags():
    assert strip_think_tags("普通回答，没有思考标记") == "普通回答，没有思考标记"
