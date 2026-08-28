"""DocumentImporter 测试：接管已有 Dify 文档。"""
from unittest.mock import MagicMock, Mock, patch

import pytest
import yaml

from src.dify.dataset_client import DifyDocument
from src.knowledge.docs_repo import DocsRepo
from src.knowledge.importer import (
    DocumentImporter,
    fetch_document_text,
    slugify_document_name,
)
from src.storage.kb_document_store import KbDocument


def test_slugify_strips_brackets_and_extension():
    assert slugify_document_name("【菜单查询】本网点处罚统计") == "菜单查询_本网点处罚统计"
    assert slugify_document_name("abnormal_message_知识库文档.md") == "abnormal_message_知识库文档"


def test_slugify_handles_empty_name():
    assert slugify_document_name("   ") == "untitled"


def test_slugify_neutralizes_path_separators():
    """文档名里的 / 会变成目录分隔符，把文件写到意料之外的位置。"""
    slug = slugify_document_name("【菜单功能】 询问任务/已签收未收到 列表")

    assert "/" not in slug and "\\" not in slug
    assert slug == "菜单功能_询问任务_已签收未收到_列表"


def test_slugify_rejects_parent_directory_traversal():
    assert ".." not in slugify_document_name("../../etc/passwd")


def _importer(tmp_path, *, doc_store=None, documents=None, text="第一段\n\n---\n\n第二段"):
    (tmp_path / "index.yaml").write_text("[]", encoding="utf-8")
    client = MagicMock()
    client.dataset_id = "ds-1"
    client.list_documents.return_value = documents or [
        DifyDocument(document_id="doc-1", name="【菜单查询】本网点处罚统计", indexing_status="completed")
    ]
    store = doc_store or MagicMock(**{"get.return_value": None})
    return DocumentImporter(
        docs_repo=DocsRepo(tmp_path),
        doc_store=store,
        dataset_client=client,
        fetch_text=lambda _doc_id: (text, 2),
    ), store


def test_writes_local_file_with_fetched_content(tmp_path):
    importer, _ = _importer(tmp_path)

    imported = importer.import_all()

    assert len(imported) == 1
    content = (tmp_path / "menus" / "菜单查询_本网点处罚统计.md").read_text(encoding="utf-8")
    assert "第一段" in content and "第二段" in content


def test_registers_index_entry_with_original_name_as_alias(tmp_path):
    importer, _ = _importer(tmp_path)

    importer.import_all()

    entries = yaml.safe_load((tmp_path / "index.yaml").read_text(encoding="utf-8"))
    assert entries[0]["topic"] == "菜单查询_本网点处罚统计"
    # 去掉【】前缀的主体也要进别名，用户提问一般不带方括号
    assert "本网点处罚统计" in entries[0]["aliases"]


def test_records_mapping_with_original_dify_name(tmp_path):
    importer, store = _importer(tmp_path)

    importer.import_all()

    kwargs = store.upsert.call_args.kwargs
    assert kwargs["dify_document_id"] == "doc-1"
    # 原名必须存下来，否则下次更新会把线上文档改名
    assert kwargs["dify_document_name"] == "【菜单查询】本网点处罚统计"
    assert len(kwargs["content_sha256"]) == 64


def test_skips_already_imported_documents(tmp_path):
    store = MagicMock()
    store.get.return_value = KbDocument(
        doc_path="menus/菜单查询_本网点处罚统计.md",
        dify_document_id="doc-1",
        dify_dataset_id="ds-1",
        content_sha256="x",
        dify_document_name="【菜单查询】本网点处罚统计",
    )
    importer, _ = _importer(tmp_path, doc_store=store)

    assert importer.import_all() == []
    store.upsert.assert_not_called()


def test_dry_run_touches_nothing(tmp_path):
    importer, store = _importer(tmp_path)

    imported = importer.import_all(dry_run=True)

    assert len(imported) == 1
    assert not (tmp_path / "menus").exists()
    store.upsert.assert_not_called()


def test_skips_document_with_no_readable_segments(tmp_path):
    importer, store = _importer(tmp_path, text="   ")

    assert importer.import_all() == []
    store.upsert.assert_not_called()


# ---------- fetch_document_text 的分页 ----------


def _segments_page(contents, *, page, has_more, total):
    resp = Mock()
    resp.status_code = 200
    resp.json.return_value = {
        "data": [
            {"position": (page - 1) * len(contents) + i + 1, "content": c}
            for i, c in enumerate(contents)
        ],
        "has_more": has_more,
        "page": page,
        "limit": len(contents),
        "total": total,
    }
    return resp


@patch("src.knowledge.importer.requests.get")
def test_fetches_all_pages_of_segments(mock_get):
    """segments 接口有分页上限，只取第一页会静默丢掉大半内容。"""
    mock_get.side_effect = [
        _segments_page(["第1段", "第2段"], page=1, has_more=True, total=3),
        _segments_page(["第3段"], page=2, has_more=False, total=3),
    ]

    text, count = fetch_document_text(
        base_url="http://localhost/v1", api_key="ds-x", dataset_id="d", document_id="doc"
    )

    assert count == 3
    assert "第1段" in text and "第3段" in text
    assert mock_get.call_count == 2


@patch("src.knowledge.importer.requests.get")
def test_stops_paging_when_no_more_data(mock_get):
    mock_get.return_value = _segments_page(["只有一段"], page=1, has_more=False, total=1)

    _text, count = fetch_document_text(
        base_url="http://localhost/v1", api_key="ds-x", dataset_id="d", document_id="doc"
    )

    assert count == 1
    assert mock_get.call_count == 1


@patch("src.knowledge.importer.requests.get")
def test_preserves_handwritten_segment_summaries(mock_get):
    """summary 是人工写的检索抓手，Dify 侧无法从正文重建，导入时丢了就永久没了。"""
    resp = Mock()
    resp.status_code = 200
    resp.json.return_value = {
        "data": [
            {"position": 1, "content": "【基本标识字段】...", "summary": "处罚关联信息 运单、pno"},
            {"position": 2, "content": "【处罚相关字段】...", "summary": None},
        ],
        "has_more": False,
    }
    mock_get.return_value = resp

    text, _count = fetch_document_text(
        base_url="http://localhost/v1", api_key="ds-x", dataset_id="d", document_id="doc"
    )

    assert "<!-- summary: 处罚关联信息 运单、pno -->" in text
    # 没有摘要的段不该冒出空注释
    assert text.count("<!-- summary:") == 1


@patch("src.knowledge.importer.requests.get")
def test_summary_comment_precedes_block_content(mock_get):
    resp = Mock()
    resp.status_code = 200
    resp.json.return_value = {
        "data": [{"position": 1, "content": "正文内容", "summary": "摘要文字"}],
        "has_more": False,
    }
    mock_get.return_value = resp

    text, _ = fetch_document_text(
        base_url="http://localhost/v1", api_key="ds-x", dataset_id="d", document_id="doc"
    )

    assert text.index("<!-- summary:") < text.index("正文内容")


@patch("src.knowledge.importer.requests.get")
def test_raises_when_segments_request_fails(mock_get):
    resp = Mock()
    resp.status_code = 404
    resp.json.return_value = {"code": "not_found", "message": "Document not found"}
    mock_get.return_value = resp

    with pytest.raises(RuntimeError, match="Document not found"):
        fetch_document_text(
            base_url="http://localhost/v1", api_key="ds-x", dataset_id="d", document_id="doc"
        )
