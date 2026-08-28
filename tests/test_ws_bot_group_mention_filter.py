"""群聊只有 @机器人 才处理，私聊不受此限制；测试 on_message_receive 的过滤逻辑。"""
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.lark.ws_bot import _bot_is_mentioned, build_message_receive_handler

BOT_OPEN_ID = "ou_bot"


class _SyncThread:
    """把 threading.Thread(...).start() 变成同步调用，方便断言。"""

    def __init__(self, target, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def _mention(open_id: str):
    return SimpleNamespace(key="@_user_1", id=SimpleNamespace(open_id=open_id), name="bot")


def _message(chat_type: str, mentions=None, text: str = "hello"):
    return SimpleNamespace(
        message_id="om_1",
        chat_id="oc_1",
        chat_type=chat_type,
        message_type="text",
        content=json.dumps({"text": text}),
        mentions=mentions,
    )


def _data(message):
    sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_sender"))
    return SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))


def _build_handler(monkeypatch, bot_open_id=BOT_OPEN_ID):
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    process_mock = MagicMock()
    monkeypatch.setattr("src.lark.ws_bot._process_message", process_mock)
    handler = build_message_receive_handler(
        MagicMock(),  # lark_client
        MagicMock(),  # dify_client
        MagicMock(),  # conversation_store
        MagicMock(),  # gap_store
        None,  # kb_commands
        bot_open_id,
    )
    return handler, process_mock


def test_bot_is_mentioned_matches_open_id():
    assert _bot_is_mentioned([_mention(BOT_OPEN_ID)], BOT_OPEN_ID) is True


def test_bot_is_mentioned_ignores_other_members():
    assert _bot_is_mentioned([_mention("ou_someone_else")], BOT_OPEN_ID) is False


def test_bot_is_mentioned_false_when_bot_open_id_unknown():
    assert _bot_is_mentioned([_mention(BOT_OPEN_ID)], None) is False


def test_group_message_without_mention_is_skipped(monkeypatch):
    handler, process_mock = _build_handler(monkeypatch)

    handler(_data(_message("group", mentions=None)))

    process_mock.assert_not_called()


def test_group_message_mentioning_someone_else_is_skipped(monkeypatch):
    handler, process_mock = _build_handler(monkeypatch)

    handler(_data(_message("group", mentions=[_mention("ou_someone_else")])))

    process_mock.assert_not_called()


def test_group_message_mentioning_bot_is_processed(monkeypatch):
    handler, process_mock = _build_handler(monkeypatch)

    handler(_data(_message("group", mentions=[_mention(BOT_OPEN_ID)])))

    process_mock.assert_called_once()


def test_p2p_message_without_mention_is_still_processed(monkeypatch):
    handler, process_mock = _build_handler(monkeypatch)

    handler(_data(_message("p2p", mentions=None)))

    process_mock.assert_called_once()


def test_group_message_is_skipped_when_bot_open_id_unavailable(monkeypatch):
    """启动时没拿到 bot_open_id（比如接口调用失败）时，群聊一律不处理，只保留私聊。"""
    handler, process_mock = _build_handler(monkeypatch, bot_open_id=None)

    handler(_data(_message("group", mentions=[_mention(BOT_OPEN_ID)])))

    process_mock.assert_not_called()
