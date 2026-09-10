"""Code Review 独立存储表测试（mock pymysql，不连接真实数据库）。"""
from unittest.mock import MagicMock, patch

from src.storage.code_review_store import CodeReviewStore


def _mock_connect():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.__enter__.return_value = conn
    return conn, cursor


@patch("src.storage.code_review_store.pymysql.connect")
def test_init_only_creates_code_review_task_and_feedback_tables(mock_connect):
    conn, cursor = _mock_connect()
    mock_connect.return_value = conn

    CodeReviewStore(
        host="127.0.0.1",
        port=3306,
        user="u",
        password="p",
        database="db",
    )

    executed_sql = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("CREATE TABLE IF NOT EXISTS lark_code_review_tasks" in sql for sql in executed_sql)
    assert any("CREATE TABLE IF NOT EXISTS lark_code_review_feedback" in sql for sql in executed_sql)
    assert all("CREATE TABLE IF NOT EXISTS lark_code_review_users" not in sql for sql in executed_sql)
    assert all("CREATE TABLE IF NOT EXISTS feishu_user" not in sql for sql in executed_sql)
    assert all("lark_bot_conversations" not in sql for sql in executed_sql)


@patch("src.storage.code_review_store.pymysql.connect")
def test_user_lookup_reads_existing_feishu_user_table(mock_connect):
    conn, cursor = _mock_connect()
    mock_connect.return_value = conn
    store = CodeReviewStore(
        host="127.0.0.1",
        port=3306,
        user="u",
        password="p",
        database="db",
    )
    cursor.reset_mock()
    cursor.fetchall.return_value = (("ou_author",),)

    assert store.find_feishu_user_id_by_name("刘文超") == "ou_author"

    sql, params = cursor.execute.call_args.args
    assert "FROM feishu_user " in sql
    assert "FROM lark_bot_conversations" not in sql
    assert params == ("刘文超",)


@patch("src.storage.code_review_store.pymysql.connect")
def test_duplicate_names_do_not_select_the_wrong_user(mock_connect):
    conn, cursor = _mock_connect()
    mock_connect.return_value = conn
    store = CodeReviewStore(
        host="127.0.0.1",
        port=3306,
        user="u",
        password="p",
        database="db",
    )
    cursor.fetchall.return_value = (("ou_one",), ("ou_two",))

    assert store.find_feishu_user_id_by_name("同名用户") == ""


@patch("src.storage.code_review_store.pymysql.connect")
def test_feedback_lookup_uses_the_quoted_review_message_id(mock_connect):
    conn, cursor = _mock_connect()
    mock_connect.return_value = conn
    store = CodeReviewStore(
        host="127.0.0.1", port=3306, user="u", password="p", database="db"
    )
    cursor.fetchone.return_value = (
        7,
        "repo",
        "12",
        '{"summary":"完整 MR 审查：get_country_code() 返回值大小写存在问题",'
        '"raw_messages":[{"type":"tool_use",'
        '"tool":"mcp__yunxiao__create_change_request_comment",'
        '"input":{"content":"## 🤖 AI 代码审查报告\\n完整评论"}}]}',
        "飞书群通知摘要",
    )

    task = store.find_review_task_for_feedback(
        review_message_id="om_review", chat_id="oc_review"
    )

    assert task is not None
    assert task.id == 7
    assert task.repository_id == "repo"
    assert task.local_id == "12"
    assert task.review_text == "完整 MR 审查：get_country_code() 返回值大小写存在问题"
    assert task.review_comment_content == "## 🤖 AI 代码审查报告\n完整评论"
    sql, params = cursor.execute.call_args.args
    assert "review_result" in sql
    assert "FROM lark_code_review_tasks" in sql
    assert "review_message_id=%s" in sql
    assert params == ("om_review", "oc_review")


@patch("src.storage.code_review_store.pymysql.connect")
def test_feedback_lookup_falls_back_to_notification_text_for_legacy_task(mock_connect):
    conn, cursor = _mock_connect()
    mock_connect.return_value = conn
    store = CodeReviewStore(
        host="127.0.0.1", port=3306, user="u", password="p", database="db"
    )
    cursor.fetchone.return_value = (7, "repo", "12", None, "旧任务的群通知摘要")

    task = store.find_review_task_for_feedback(
        review_message_id="om_review", chat_id="oc_review"
    )

    assert task is not None
    assert task.review_text == "旧任务的群通知摘要"
    assert task.review_comment_content == ""


@patch("src.storage.code_review_store.pymysql.connect")
def test_feedback_insert_preserves_feedback_message_id_for_idempotency(mock_connect):
    conn, cursor = _mock_connect()
    mock_connect.return_value = conn
    cursor.lastrowid = 8
    store = CodeReviewStore(
        host="127.0.0.1", port=3306, user="u", password="p", database="db"
    )
    cursor.reset_mock()

    feedback_id = store.create_feedback(
        review_task_id=7,
        feedback_message_id="om_feedback",
        parent_message_id="om_review",
        chat_id="oc_review",
        sender_open_id="ou_user",
        feedback_text="这是误报",
    )

    assert feedback_id == 8
    sql, params = cursor.execute.call_args.args
    assert "INSERT INTO lark_code_review_feedback" in sql
    assert params[1] == "om_feedback"
