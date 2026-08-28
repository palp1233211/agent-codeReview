"""KnowledgeGapStore 建表 / 入队 / 状态流转测试（mock pymysql，不连真实数据库）"""
import pytest
from unittest.mock import MagicMock, patch

from src.dify.intent import parse_envelope
from src.storage.knowledge_gap_store import KnowledgeGapStore

_ENVELOPE = '{"content":"","unknown":"处罚人是什么字段"}'


def _mock_connect():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.__enter__.return_value = conn
    return conn, cursor


def _store(mock_connect) -> tuple[KnowledgeGapStore, MagicMock]:
    conn, cursor = _mock_connect()
    mock_connect.return_value = conn
    store = KnowledgeGapStore(host="127.0.0.1", port=3306, user="u", password="p", database="db")
    cursor.reset_mock()  # 清掉 __init__ 建表那次调用
    return store, cursor


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_init_creates_table_if_not_exists(mock_connect):
    conn, cursor = _mock_connect()
    mock_connect.return_value = conn

    KnowledgeGapStore(host="127.0.0.1", port=3306, user="u", password="p", database="db")

    executed_sql = cursor.execute.call_args[0][0]
    assert "CREATE TABLE IF NOT EXISTS lark_bot_knowledge_gaps" in executed_sql


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_enqueue_inserts_normalized_intent_fields(mock_connect):
    store, cursor = _store(mock_connect)
    cursor.lastrowid = 12

    gap_id = store.enqueue(
        user_id="ou_1", chat_id="oc_1", message_id="om_1", envelope=parse_envelope(_ENVELOPE)
    )

    executed_sql, params = cursor.execute.call_args[0]
    assert "INSERT IGNORE INTO lark_bot_knowledge_gaps" in executed_sql
    assert gap_id == 12
    # 入队的是 unknown 字段的内容——那才是知识库没覆盖到的问题
    assert params[:4] == ("ou_1", "oc_1", "om_1", "处罚人是什么字段")
    # 可选列在解析阶段已归一化成空串，不会违反 NOT NULL 约束
    assert params[4:7] == ("", "", "")


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_enqueue_returns_none_when_message_already_recorded(mock_connect):
    store, cursor = _store(mock_connect)
    cursor.lastrowid = 0  # INSERT IGNORE 命中 uk_message_id，没有真正插入

    gap_id = store.enqueue(
        user_id="ou_1", chat_id="oc_1", message_id="om_1", envelope=parse_envelope(_ENVELOPE)
    )

    assert gap_id is None


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_try_claim_returns_true_when_row_transitioned(mock_connect):
    store, cursor = _store(mock_connect)
    cursor.rowcount = 1

    assert store.try_claim(12, from_status="pending", to_status="filling", operator_id="ou_9")

    executed_sql, params = cursor.execute.call_args[0]
    # CAS：必须带上 from_status 条件，否则并发下会重复抢占
    assert "WHERE id = %s AND status = %s" in executed_sql
    assert params == ("filling", "ou_9", 12, "pending")


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_try_claim_returns_false_when_status_already_changed(mock_connect):
    store, cursor = _store(mock_connect)
    cursor.rowcount = 0  # 已被别的线程抢走

    assert not store.try_claim(12, from_status="pending", to_status="filling")


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_mark_needs_human_records_reason(mock_connect):
    store, cursor = _store(mock_connect)

    store.mark_needs_human(12, fail_reason="代码里查不到该字段，问题本身有歧义")

    executed_sql, params = cursor.execute.call_args[0]
    assert "status = 'needs_human'" in executed_sql
    assert params == ("代码里查不到该字段，问题本身有歧义", 12)


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_mark_drafted_records_doc_path_and_hash(mock_connect):
    store, cursor = _store(mock_connect)

    store.mark_drafted(12, doc_path="menus/abnormalv2.md", draft_summary="新增 2 个块", content_sha256="a" * 64)

    executed_sql, params = cursor.execute.call_args[0]
    assert "status = 'drafted'" in executed_sql
    assert params == ("menus/abnormalv2.md", "新增 2 个块", "a" * 64, 12)


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_list_by_status_returns_gap_objects(mock_connect):
    store, cursor = _store(mock_connect)
    cursor.fetchall.return_value = [
        {
            "id": 12,
            "user_id": "ou_1",
            "chat_id": "oc_1",
            "original_query": "处罚人是什么字段",
            "task_name": "",
            "status": "pending",
            "doc_path": "",
            "draft_summary": None,
            "fail_reason": None,
            "content_sha256": "",
            "dify_document_id": "",
        }
    ]

    gaps = store.list_by_status("pending", limit=5)

    assert len(gaps) == 1
    assert gaps[0].id == 12
    assert gaps[0].original_query == "处罚人是什么字段"
    # draft_summary 为 NULL 时不应变成字符串 "None"
    assert gaps[0].draft_summary == ""


# ---------- 更新不到行时必须显式失败 ----------


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_mark_drafted_raises_when_row_missing(mock_connect):
    """踩过的坑：行被删/被并发改掉时 UPDATE 影响 0 行，代码却照常发"查证完成"通知，
    用户按提示去 approve 才发现根本没落库。必须在这里就炸出来。"""
    from src.storage.knowledge_gap_store import GapRowMissing

    store, cursor = _store(mock_connect)
    cursor.rowcount = 0

    with pytest.raises(GapRowMissing, match="12"):
        store.mark_drafted(12, doc_path="d.md", draft_summary="s", content_sha256="h")


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_mark_synced_raises_when_row_missing(mock_connect):
    from src.storage.knowledge_gap_store import GapRowMissing

    store, cursor = _store(mock_connect)
    cursor.rowcount = 0

    with pytest.raises(GapRowMissing):
        store.mark_synced(12, dify_document_id="d", dify_batch="b")


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_mark_needs_human_raises_when_row_missing(mock_connect):
    from src.storage.knowledge_gap_store import GapRowMissing

    store, cursor = _store(mock_connect)
    cursor.rowcount = 0

    with pytest.raises(GapRowMissing):
        store.mark_needs_human(12, fail_reason="x")


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_mark_succeeds_when_row_updated(mock_connect):
    store, cursor = _store(mock_connect)
    cursor.rowcount = 1

    store.mark_drafted(12, doc_path="d.md", draft_summary="s", content_sha256="h")


# ---------- 卡死记录回收 ----------


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_reclaim_stale_resets_filling_rows_to_pending(mock_connect):
    """进程在 fill 途中挂掉，行会永久停在 filling：既不会被重试（CAS 只认 pending），
    也不出现在待办里，静默卡死。启动时回收。"""
    store, cursor = _store(mock_connect)
    cursor.rowcount = 2

    assert store.reclaim_stale(older_than_minutes=30) == 2

    sql, params = cursor.execute.call_args[0]
    assert "SET status = 'pending'" in sql
    assert "status = 'filling'" in sql
    assert params == (30,)


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_reclaim_stale_returns_zero_when_nothing_stuck(mock_connect):
    store, cursor = _store(mock_connect)
    cursor.rowcount = 0

    assert store.reclaim_stale() == 0


# ---------- 删除 ----------


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_delete_returns_true_when_row_removed(mock_connect):
    store, cursor = _store(mock_connect)
    cursor.rowcount = 1

    assert store.delete(12) is True

    executed_sql, params = cursor.execute.call_args[0]
    assert "DELETE FROM lark_bot_knowledge_gaps" in executed_sql
    assert params == (12,)


@patch("src.storage.knowledge_gap_store.pymysql.connect")
def test_delete_returns_false_when_row_missing(mock_connect):
    store, cursor = _store(mock_connect)
    cursor.rowcount = 0

    assert store.delete(999) is False
