"""_process_message 分发测试：/kb 指令必须在转发 Dify 之前被拦截。"""
from unittest.mock import MagicMock

from src.lark.ws_bot import _process_message


def _call(kb_commands, dify_client=None, lark_client=None):
    _process_message(
        lark_client or MagicMock(),
        dify_client or MagicMock(),
        MagicMock(),  # conversation_store
        MagicMock(),  # gap_store
        kb_commands,
        "ou_1",
        "oc_1",
        "om_1",
        "/kb list",
    )


def test_command_is_intercepted_before_reaching_dify():
    kb = MagicMock()
    kb.handle.return_value = "队列是空的"
    dify = MagicMock()
    lark = MagicMock()

    _call(kb, dify_client=dify, lark_client=lark)

    dify.chat.assert_not_called()
    lark.send_text_message.assert_called_once_with("oc_1", "队列是空的")


def test_non_command_falls_through_to_dify():
    kb = MagicMock()
    kb.handle.return_value = None  # 不是指令
    dify = MagicMock()
    dify.chat.return_value = MagicMock(answer="普通回答", conversation_id="c-1")

    _call(kb, dify_client=dify)

    dify.chat.assert_called_once()


def test_works_when_kb_pipeline_is_disabled():
    """知识补全没配置时（kb_commands=None），问答机器人必须照常工作。"""
    dify = MagicMock()
    dify.chat.return_value = MagicMock(answer="普通回答", conversation_id="c-1")

    _call(None, dify_client=dify)

    dify.chat.assert_called_once()


def test_lark_failure_on_command_reply_does_not_reach_dify():
    kb = MagicMock()
    kb.handle.return_value = "队列是空的"
    lark = MagicMock()
    lark.send_text_message.side_effect = RuntimeError("飞书接口挂了")
    dify = MagicMock()

    _call(kb, dify_client=dify, lark_client=lark)

    # 回复发失败也不能退回去问 Dify，那会答非所问
    dify.chat.assert_not_called()
