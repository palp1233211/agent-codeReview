"""ConversationStore 建表 / 写入逻辑测试（mock pymysql，不连真实数据库）"""
from unittest.mock import MagicMock, patch

from src.storage.conversation_store import ConversationStore


def _mock_connect():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.__enter__.return_value = conn
    return conn, cursor


@patch("src.storage.conversation_store.pymysql.connect")
def test_init_creates_table_if_not_exists(mock_connect):
    conn, cursor = _mock_connect()
    mock_connect.return_value = conn

    ConversationStore(host="127.0.0.1", port=3306, user="u", password="p", database="db")

    executed_sql = cursor.execute.call_args[0][0]
    assert "CREATE TABLE IF NOT EXISTS lark_bot_conversations" in executed_sql


@patch("src.storage.conversation_store.pymysql.connect")
def test_log_inserts_row_with_expected_params(mock_connect):
    conn, cursor = _mock_connect()
    mock_connect.return_value = conn

    store = ConversationStore(host="127.0.0.1", port=3306, user="u", password="p", database="db")
    cursor.reset_mock()  # 清掉 __init__ 建表那次调用

    store.log(
        user_id="ou_1",
        chat_id="oc_1",
        message_id="om_1",
        conversation_id="conv-1",
        question="你好",
        answer="你好呀",
    )

    executed_sql, params = cursor.execute.call_args[0]
    assert "INSERT IGNORE INTO lark_bot_conversations" in executed_sql
    assert params == ("ou_1", "oc_1", "om_1", "conv-1", "你好", "你好呀")
