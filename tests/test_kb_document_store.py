"""KbDocumentStore 测试。

重点守住唯一键：直接把 (dify_dataset_id, doc_path) 做联合唯一键会超 InnoDB
767 字节索引上限（VARCHAR(512) × utf8mb4 = 2048 字节），线上建表直接失败。
"""
from unittest.mock import MagicMock, patch

from src.storage.kb_document_store import KbDocumentStore, _doc_key


def _mock_connect():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.__enter__.return_value = conn
    return conn, cursor


def _store(mock_connect):
    conn, cursor = _mock_connect()
    mock_connect.return_value = conn
    store = KbDocumentStore(host="h", port=3306, user="u", password="p", database="db")
    cursor.reset_mock()
    return store, cursor


@patch("src.storage.kb_document_store.pymysql.connect")
def test_unique_key_uses_hash_not_raw_columns(mock_connect):
    conn, cursor = _mock_connect()
    mock_connect.return_value = conn

    KbDocumentStore(host="h", port=3306, user="u", password="p", database="db")

    ddl = cursor.execute.call_args[0][0]
    assert "UNIQUE KEY uk_doc_key (doc_key)" in ddl
    # 这两列联合做唯一键会超 767 字节，必须不出现
    assert "UNIQUE KEY uk_dataset_doc" not in ddl


def test_doc_key_separates_fields_to_avoid_collision():
    """('a', 'b/c') 和 ('a/b', 'c') 拼起来都是 'ab/c'，必须靠分隔符区分。"""
    assert _doc_key("a", "b/c") != _doc_key("a/b", "c")


def test_doc_key_is_stable():
    assert _doc_key("ds-1", "menus/x.md") == _doc_key("ds-1", "menus/x.md")
    assert len(_doc_key("ds-1", "menus/x.md")) == 64


@patch("src.storage.kb_document_store.pymysql.connect")
def test_get_looks_up_by_hash(mock_connect):
    store, cursor = _store(mock_connect)
    cursor.fetchone.return_value = None

    store.get(dataset_id="ds-1", doc_path="menus/x.md")

    _sql, params = cursor.execute.call_args[0]
    assert params == (_doc_key("ds-1", "menus/x.md"),)


@patch("src.storage.kb_document_store.pymysql.connect")
def test_upsert_writes_hash_and_document_name(mock_connect):
    store, cursor = _store(mock_connect)

    store.upsert(
        doc_path="menus/x.md",
        dataset_id="ds-1",
        dify_document_id="doc-1",
        content_sha256="a" * 64,
        dify_document_name="【菜单查询】本网点处罚统计",
    )

    _sql, params = cursor.execute.call_args[0]
    assert params[0] == _doc_key("ds-1", "menus/x.md")
    assert params[4] == "【菜单查询】本网点处罚统计"


@patch("src.storage.kb_document_store.pymysql.connect")
def test_get_returns_document_name(mock_connect):
    store, cursor = _store(mock_connect)
    cursor.fetchone.return_value = {
        "doc_path": "menus/x.md",
        "dify_dataset_id": "ds-1",
        "dify_document_id": "doc-1",
        "dify_document_name": "【菜单查询】本网点处罚统计",
        "content_sha256": "a" * 64,
    }

    document = store.get(dataset_id="ds-1", doc_path="menus/x.md")

    assert document.dify_document_name == "【菜单查询】本网点处罚统计"
