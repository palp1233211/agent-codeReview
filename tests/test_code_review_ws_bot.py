"""Code Review 专用飞书机器人路由测试。"""
import json
import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.lark.client import LarkClient
from src.lark.code_review_ws_bot import build_review_message_receive_handler


BOT_OPEN_ID = "ou_code_review_bot"
REVIEW_CHAT_ID = "oc_review"


class _SyncThread:
    """将后台执行转换成同步调用，方便断言任务参数。"""

    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _mention(open_id: str):
    return SimpleNamespace(key="@_user_1", id=SimpleNamespace(open_id=open_id))


def _event(
    *,
    chat_id=REVIEW_CHAT_ID,
    mentions=None,
    message_id="om_1",
    parent_id=None,
    sender_open_id="ou_sender",
    text="",
):
    message = SimpleNamespace(
        message_id=message_id,
        chat_id=chat_id,
        message_type="text",
        content=json.dumps({"text": text}),
        mentions=mentions or [],
        parent_id=parent_id,
    )
    sender = SimpleNamespace(sender_id=SimpleNamespace(open_id=sender_open_id))
    return SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))


def _handler(monkeypatch):
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    execute = MagicMock()
    monkeypatch.setattr("src.lark.code_review_ws_bot._run_review_task", execute)
    feedback = MagicMock()
    monkeypatch.setattr("src.lark.code_review_ws_bot._run_feedback_analysis", feedback)
    store = MagicMock()
    store.create_task.return_value = 42
    return (
        build_review_message_receive_handler(
            lark_client=MagicMock(),
            store=store,
            bot_open_id=BOT_OPEN_ID,
            review_chat_id=REVIEW_CHAT_ID,
            organization_id="org_1",
        ),
        store,
        execute,
        feedback,
    )


def test_code_review_client_uses_its_own_environment_prefix(monkeypatch):
    monkeypatch.setenv("LARK_CODE_REVIEW_APP_ID", "cli_review")
    monkeypatch.setenv("LARK_CODE_REVIEW_APP_SECRET", "review_secret")

    with patch.object(LarkClient, "__init__", return_value=None) as init:
        LarkClient.from_env("LARK_CODE_REVIEW_")

    init.assert_called_once_with(app_id="cli_review", app_secret="review_secret")


def test_only_fixed_group_and_code_review_mention_start_a_review(monkeypatch):
    handler, store, execute, feedback = _handler(monkeypatch)
    url = "https://codeup.aliyun.com/flashexpress/ard/be/ard-etl/change/1215"

    handler(_event(mentions=[_mention(BOT_OPEN_ID)], text=f"@_user_1 {url}"))

    store.create_task.assert_called_once()
    task = store.create_task.call_args.kwargs
    assert task["trigger_message_id"] == "om_1"
    assert task["chat_id"] == REVIEW_CHAT_ID
    assert task["mr_url"] == url
    assert task["local_id"] == "1215"
    assert execute.call_args.kwargs["task_id"] == 42
    assert execute.call_args.kwargs["organization_id"] == "org_1"
    feedback.assert_not_called()


def test_other_bot_or_other_group_cannot_trigger_code_review(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="src.lark.code_review_ws_bot")
    handler, store, execute, feedback = _handler(monkeypatch)
    url = "https://codeup.aliyun.com/flashexpress/ard/be/ard-etl/change/1215"

    handler(_event(mentions=[_mention("ou_dify_bot")], text=url))
    handler(_event(chat_id="oc_else", mentions=[_mention(BOT_OPEN_ID)], text=url, message_id="om_2"))

    store.create_task.assert_not_called()
    execute.assert_not_called()
    feedback.assert_not_called()
    assert "未 @当前机器人" in caplog.text
    assert "不在目标群" in caplog.text


def test_duplicate_event_does_not_create_a_second_task(monkeypatch):
    handler, store, execute, feedback = _handler(monkeypatch)
    url = "https://codeup.aliyun.com/flashexpress/ard/be/ard-etl/change/1215"
    event = _event(mentions=[_mention(BOT_OPEN_ID)], text=url)

    handler(event)
    handler(event)

    store.create_task.assert_called_once()
    execute.assert_called_once()
    feedback.assert_not_called()


def test_direct_quote_of_review_message_creates_feedback_without_mention(monkeypatch):
    handler, store, execute, feedback = _handler(monkeypatch)
    store.find_review_task_for_feedback.return_value = SimpleNamespace(
        id=7,
        review_text="原 Review：get_country_code() 返回值大小写会导致比较失败。",
        repository_id="repo",
        local_id="12",
        review_comment_content="## 🤖 AI 代码审查报告\n原评论",
    )
    store.create_feedback.return_value = 99

    handler(
        _event(
            message_id="om_feedback",
            parent_id="om_review_result",
            text="这里已经在上游过滤空值了，这条是误报。",
        )
    )

    store.find_review_task_for_feedback.assert_called_once_with(
        review_message_id="om_review_result",
        chat_id=REVIEW_CHAT_ID,
    )
    store.create_feedback.assert_called_once_with(
        review_task_id=7,
        feedback_message_id="om_feedback",
        parent_message_id="om_review_result",
        chat_id=REVIEW_CHAT_ID,
        sender_open_id="ou_sender",
        feedback_text="这里已经在上游过滤空值了，这条是误报。",
    )
    feedback.assert_called_once()
    assert feedback.call_args.kwargs["feedback_id"] == 99
    assert feedback.call_args.kwargs["feedback_message_id"] == "om_feedback"
    assert feedback.call_args.kwargs["review_text"] == "原 Review：get_country_code() 返回值大小写会导致比较失败。"
    assert feedback.call_args.kwargs["feedback_text"] == "这里已经在上游过滤空值了，这条是误报。"
    assert feedback.call_args.kwargs["organization_id"] == "org_1"
    assert feedback.call_args.kwargs["repository_id"] == "repo"
    assert feedback.call_args.kwargs["local_id"] == "12"
    assert feedback.call_args.kwargs["original_comment_content"] == "## 🤖 AI 代码审查报告\n原评论"
    assert feedback.call_args.kwargs["store"] is store
    assert feedback.call_args.kwargs["lark_client"] is not None
    execute.assert_not_called()


def test_quote_of_non_review_message_is_not_feedback(monkeypatch):
    handler, store, execute, feedback = _handler(monkeypatch)
    store.find_review_task_for_feedback.return_value = None

    handler(_event(parent_id="om_someone_else", text="这条我不同意"))

    store.create_feedback.assert_not_called()
    store.create_task.assert_not_called()
    execute.assert_not_called()
    feedback.assert_not_called()
