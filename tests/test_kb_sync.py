"""GapSyncer 测试：推送 Dify 的幂等性、索引轮询、失败不误报成功。"""
from unittest.mock import MagicMock

import pytest

from src.dify.dataset_client import DocumentRef
from src.knowledge.docs_repo import DocsRepo
from src.knowledge.sync import GapSyncer
from src.storage.kb_document_store import KbDocument
from src.storage.knowledge_gap_store import KnowledgeGap

_DOC_BODY = "# 客户投诉平台知识库\n\n---\n\n## 处罚人是什么字段\n\n<!-- src: a.php -->\n\n答案。\n"


def _repo(tmp_path) -> DocsRepo:
    (tmp_path / "menus").mkdir(exist_ok=True)  # 同一个用例里可能建两次
    (tmp_path / "index.yaml").write_text("[]", encoding="utf-8")
    (tmp_path / "menus" / "customer_complaints.md").write_text(_DOC_BODY, encoding="utf-8")
    return DocsRepo(tmp_path)


def _gap(sha: str = "") -> KnowledgeGap:
    return KnowledgeGap(
        id=12,
        user_id="ou_1",
        chat_id="oc_1",
        original_query="处罚人是什么字段",
        task_name="",
        status="syncing",
        doc_path="menus/customer_complaints.md",
        draft_summary="新增 1 个块",
        fail_reason="",
        content_sha256=sha,
        dify_document_id="",
    )


def _syncer(tmp_path, *, dataset_client, doc_store=None, gap_store=None, notify=None):
    return GapSyncer(
        gap_store=gap_store or MagicMock(),
        doc_store=doc_store or MagicMock(**{"get.return_value": None}),
        docs_repo=_repo(tmp_path),
        dataset_client=dataset_client,
        notify=notify or MagicMock(),
        sleep=lambda _seconds: None,  # 测试里不真等
    )


def _client(*, status_sequence=("completed",)) -> MagicMock:
    client = MagicMock()
    client.dataset_id = "ds-1"
    client.create_document_by_text.return_value = DocumentRef("doc-1", "b-1", "x")
    client.update_document_by_text.return_value = DocumentRef("doc-1", "b-1", "x")
    client.indexing_status.side_effect = list(status_sequence)
    client.verify_retrievable.return_value = True
    return client


def test_creates_document_when_not_mapped_yet(tmp_path):
    client = _client()
    store = MagicMock()

    _syncer(tmp_path, dataset_client=client, gap_store=store).sync(_gap(), chat_id="oc_1")

    client.create_document_by_text.assert_called_once()
    client.update_document_by_text.assert_not_called()

    pushed = client.create_document_by_text.call_args.kwargs["text"]
    assert "## 处罚人是什么字段" in pushed and "答案。" in pushed
    # 维护用的锚点是本地元数据，不该进 Dify 的检索内容
    assert "<!-- src:" not in pushed
    # 开头那个只有标题的块会被丢掉（摘要模型对着光标题会编内容），只剩 1 个实质块
    assert "# 客户投诉平台知识库" not in pushed
    assert "\n---\n" not in pushed
    store.mark_synced.assert_called_once()


def test_updates_document_when_already_mapped(tmp_path):
    client = _client()
    doc_store = MagicMock()
    doc_store.get.return_value = KbDocument(
        doc_path="menus/customer_complaints.md",
        dify_document_id="doc-existing",
        dify_dataset_id="ds-1",
        content_sha256="stale",
    )

    _syncer(tmp_path, dataset_client=client, doc_store=doc_store).sync(_gap(), chat_id="oc_1")

    # 必须走更新，重建会换掉 document_id，检索历史全断
    client.create_document_by_text.assert_not_called()
    assert client.update_document_by_text.call_args.kwargs["document_id"] == "doc-existing"


def test_skips_push_when_content_already_synced(tmp_path):
    """幂等：内容没变就别重复推，重复推会触发一次无谓的重新索引。"""
    repo = _repo(tmp_path)
    current_sha = repo.doc_sha256("menus/customer_complaints.md")
    client = _client()
    doc_store = MagicMock()
    doc_store.get.return_value = KbDocument(
        doc_path="menus/customer_complaints.md",
        dify_document_id="doc-existing",
        dify_dataset_id="ds-1",
        content_sha256=current_sha,
    )
    gap_store = MagicMock()

    _syncer(
        tmp_path, dataset_client=client, doc_store=doc_store, gap_store=gap_store
    ).sync(_gap(), chat_id="oc_1")

    client.update_document_by_text.assert_not_called()
    client.create_document_by_text.assert_not_called()
    gap_store.mark_synced.assert_called_once()


def test_waits_until_indexing_completes(tmp_path):
    client = _client(status_sequence=("waiting", "indexing", "completed"))
    store = MagicMock()

    _syncer(tmp_path, dataset_client=client, gap_store=store).sync(_gap(), chat_id="oc_1")

    assert client.indexing_status.call_count == 3
    store.mark_synced.assert_called_once()


def test_marks_failed_when_indexing_errors(tmp_path):
    """HTTP 200 只代表入队，索引失败必须如实报告，不能说'已同步'。"""
    client = _client(status_sequence=("error",))
    store = MagicMock()
    notify = MagicMock()

    _syncer(
        tmp_path, dataset_client=client, gap_store=store, notify=notify
    ).sync(_gap(), chat_id="oc_1")

    store.mark_synced.assert_not_called()
    store.mark_failed.assert_called_once()
    assert "索引" in notify.call_args[0][1]


def test_records_mapping_after_successful_sync(tmp_path):
    client = _client()
    doc_store = MagicMock()
    doc_store.get.return_value = None

    _syncer(tmp_path, dataset_client=client, doc_store=doc_store).sync(_gap(), chat_id="oc_1")

    kwargs = doc_store.upsert.call_args.kwargs
    assert kwargs["doc_path"] == "menus/customer_complaints.md"
    assert kwargs["dify_document_id"] == "doc-1"
    assert len(kwargs["content_sha256"]) == 64


def test_marks_failed_when_retrieval_verification_fails(tmp_path):
    """索引状态是 completed 不代表真能查到——Weaviate 只读/缓存未刷新时 Dify 照样报完成。
    这里必须靠实际检索验证，不能只信状态字段，否则用户会看到"已同步"但查不到。"""
    client = _client()
    client.verify_retrievable.return_value = False
    store = MagicMock()
    notify = MagicMock()

    _syncer(
        tmp_path, dataset_client=client, gap_store=store, notify=notify
    ).sync(_gap(), chat_id="oc_1")

    store.mark_synced.assert_not_called()
    store.mark_failed.assert_called_once()
    assert "查不到" in notify.call_args[0][1] or "验证" in notify.call_args[0][1]


def test_verification_uses_pushed_content_and_document_id(tmp_path):
    client = _client()
    client.verify_retrievable.return_value = True

    _syncer(tmp_path, dataset_client=client).sync(_gap(), chat_id="oc_1")

    args = client.verify_retrievable.call_args.args
    assert args[0] == "doc-1"
    assert "处罚人是什么字段" in args[1]


def test_records_document_id_without_matching_hash_when_verification_fails(tmp_path):
    """验证失败也要记住 document_id：否则下次 /kb sync 因为找不到映射会重新 create
    出一篇重复文档（而不是 update 同一篇），而故意不写真实内容哈希是为了不被幂等
    短路挡住——这样下次 /kb sync 才会真的再推一次，而不是误判"内容没变"直接跳过。"""
    client = _client()
    client.verify_retrievable.return_value = False
    doc_store = MagicMock()
    doc_store.get.return_value = None

    _syncer(tmp_path, dataset_client=client, doc_store=doc_store).sync(_gap(), chat_id="oc_1")

    kwargs = doc_store.upsert.call_args.kwargs
    assert kwargs["dify_document_id"] == "doc-1"
    assert kwargs["content_sha256"] == ""


def test_raises_when_doc_path_missing(tmp_path):
    client = _client()
    gap = KnowledgeGap(**{**_gap().__dict__, "doc_path": ""})

    with pytest.raises(ValueError, match="doc_path"):
        _syncer(tmp_path, dataset_client=client).sync(gap, chat_id="oc_1")


def test_gives_up_after_max_polls(tmp_path):
    """索引一直不完成也不能死等，飞书那边要有个交代。"""
    client = _client(status_sequence=["indexing"] * 50)
    store = MagicMock()
    notify = MagicMock()

    _syncer(
        tmp_path, dataset_client=client, gap_store=store, notify=notify
    ).sync(_gap(), chat_id="oc_1")

    store.mark_synced.assert_not_called()
    store.mark_failed.assert_called_once()
