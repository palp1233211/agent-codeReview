"""render_answer 测试：不泄露原始 JSON，且 DB 故障不能挡住给用户回话。"""
import json
from unittest.mock import MagicMock

import pymysql

from src.knowledge.gap_recorder import render_answer


def _env(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


def _ctx(gap_store=None):
    return {
        "user_id": "ou_1",
        "chat_id": "oc_1",
        "message_id": "om_1",
        "gap_store": gap_store or MagicMock(),
    }


def test_plain_answer_passes_through_untouched():
    store = MagicMock()

    answer = render_answer("客诉平台的处罚大类是 21。", **_ctx(store))

    assert answer == "客诉平台的处罚大类是 21。"
    store.enqueue.assert_not_called()


def test_full_answer_sends_content_without_enqueuing():
    store = MagicMock()

    answer = render_answer(_env(content="处罚大类是 21。", unknown=""), **_ctx(store))

    assert answer == "处罚大类是 21。"
    store.enqueue.assert_not_called()


def test_pure_gap_is_enqueued_and_never_leaks_json():
    store = MagicMock()
    store.enqueue.return_value = 12

    answer = render_answer(_env(content="", unknown="客诉的处罚类别号"), **_ctx(store))

    store.enqueue.assert_called_once()
    assert store.enqueue.call_args.kwargs["message_id"] == "om_1"
    assert "{" not in answer and "unknown" not in answer
    assert "12" in answer  # 带上编号，方便接着发 /kb fill 12


def test_partial_answer_sends_content_and_records_the_rest():
    """答上来一半时，答案必须照发——不能因为有缺口就把已知的部分也吞掉。"""
    store = MagicMock()
    store.enqueue.return_value = 12

    answer = render_answer(
        _env(content="处罚大类是 21。", unknown="审核状态 state 有哪些取值"), **_ctx(store)
    )

    assert "处罚大类是 21。" in answer
    assert "审核状态 state 有哪些取值" in answer
    assert "/kb fill 12" in answer
    store.enqueue.assert_called_once()


def test_partial_answer_enqueues_only_the_unknown_part():
    store = MagicMock()
    store.enqueue.return_value = 12

    render_answer(_env(content="已知部分", unknown="未知部分"), **_ctx(store))

    envelope = store.enqueue.call_args.kwargs["envelope"]
    assert envelope.gap_query == "未知部分"


def test_empty_envelope_falls_back_without_leaking():
    store = MagicMock()

    answer = render_answer(_env(content="", unknown=""), **_ctx(store))

    store.enqueue.assert_not_called()
    assert "{" not in answer and answer.strip()


def test_legacy_intent_envelope_still_enqueues():
    store = MagicMock()
    store.enqueue.return_value = 12

    answer = render_answer(
        _env(intent="unknown", original_query="处罚人是什么字段"), **_ctx(store)
    )

    store.enqueue.assert_called_once()
    assert "{" not in answer


def test_legacy_known_intent_is_not_enqueued_but_still_not_leaked():
    store = MagicMock()

    answer = render_answer(_env(intent="punish_query", original_query="x"), **_ctx(store))

    store.enqueue.assert_not_called()
    assert "punish_query" not in answer and "{" not in answer


def test_duplicate_message_still_gets_friendly_reply():
    store = MagicMock()
    store.enqueue.return_value = None  # uk_message_id 命中，之前已记过

    answer = render_answer(_env(content="", unknown="某问题"), **_ctx(store))

    assert "{" not in answer and answer.strip()


def test_db_failure_does_not_block_reply():
    store = MagicMock()
    store.enqueue.side_effect = pymysql.MySQLError("connection refused")

    answer = render_answer(_env(content="", unknown="某问题"), **_ctx(store))

    assert "{" not in answer and answer.strip()


def test_blank_plain_answer_falls_back_instead_of_sending_empty_text():
    """Dify 偶发返回空 answer（非信封）时不能原样透传，飞书拒绝空文本消息（230001）。"""
    store = MagicMock()

    answer = render_answer("", **_ctx(store))

    assert answer.strip()
    store.enqueue.assert_not_called()


def test_db_failure_still_delivers_known_part_of_partial_answer():
    """入队挂了也不能丢掉已经答出来的内容。"""
    store = MagicMock()
    store.enqueue.side_effect = pymysql.MySQLError("connection refused")

    answer = render_answer(_env(content="处罚大类是 21。", unknown="其他"), **_ctx(store))

    assert "处罚大类是 21。" in answer
