"""同步时的文档命名：接管已有 Dify 文档时绝不能把人家改名。"""
from unittest.mock import MagicMock

from src.dify.dataset_client import DocumentRef
from src.knowledge.docs_repo import DocsRepo
from src.knowledge.sync import GapSyncer
from src.storage.kb_document_store import KbDocument
from src.storage.knowledge_gap_store import KnowledgeGap


def _repo(tmp_path) -> DocsRepo:
    (tmp_path / "menus").mkdir(exist_ok=True)
    (tmp_path / "index.yaml").write_text("[]", encoding="utf-8")
    (tmp_path / "menus" / "benwangdian_punish.md").write_text("# 内容\n", encoding="utf-8")
    return DocsRepo(tmp_path)


def _gap() -> KnowledgeGap:
    return KnowledgeGap(
        id=12, user_id="ou_1", chat_id="oc_1", original_query="q", task_name="",
        status="syncing", doc_path="menus/benwangdian_punish.md", draft_summary="",
        fail_reason="", content_sha256="", dify_document_id="",
    )


def _syncer(tmp_path, dataset_client, doc_store):
    return GapSyncer(
        gap_store=MagicMock(),
        doc_store=doc_store,
        docs_repo=_repo(tmp_path),
        dataset_client=dataset_client,
        notify=MagicMock(),
        sleep=lambda _s: None,
    )


def _client() -> MagicMock:
    client = MagicMock()
    client.dataset_id = "ds-1"
    client.update_document_by_text.return_value = DocumentRef("doc-1", "b-1", "x")
    client.create_document_by_text.return_value = DocumentRef("doc-1", "b-1", "x")
    client.indexing_status.return_value = "completed"
    client.verify_retrievable.return_value = True
    return client


def test_preserves_existing_dify_document_name(tmp_path):
    """接管的文档在 Dify 里叫「【菜单查询】本网点处罚统计」，更新后必须还叫这个。"""
    client = _client()
    doc_store = MagicMock()
    doc_store.get.return_value = KbDocument(
        doc_path="menus/benwangdian_punish.md",
        dify_document_id="doc-existing",
        dify_dataset_id="ds-1",
        content_sha256="stale",
        dify_document_name="【菜单查询】本网点处罚统计",
    )

    _syncer(tmp_path, client, doc_store).sync(_gap(), chat_id="oc_1")

    assert client.update_document_by_text.call_args.kwargs["name"] == "【菜单查询】本网点处罚统计"


def test_falls_back_to_filename_when_no_recorded_name(tmp_path):
    """我们自己新建的文档没有历史名字，用文件名即可。"""
    client = _client()
    doc_store = MagicMock()
    doc_store.get.return_value = None

    _syncer(tmp_path, client, doc_store).sync(_gap(), chat_id="oc_1")

    assert client.create_document_by_text.call_args.kwargs["name"] == "benwangdian_punish"


def test_records_name_in_mapping_after_sync(tmp_path):
    client = _client()
    doc_store = MagicMock()
    doc_store.get.return_value = None

    _syncer(tmp_path, client, doc_store).sync(_gap(), chat_id="oc_1")

    assert doc_store.upsert.call_args.kwargs["dify_document_name"] == "benwangdian_punish"
