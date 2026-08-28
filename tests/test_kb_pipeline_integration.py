"""端到端串联测试：用真实的 DocsRepo / KbCommands / GapFiller / GapSyncer，
只把「跑 Claude agent」和「Dify HTTP」换成假的。

单元测试各管一段，这个用例负责证明它们接得上——尤其是 fill 产出的 doc_path
能被 sync 正确读到。
"""
import json
from unittest.mock import MagicMock

from src.dify.dataset_client import DocumentRef
from src.knowledge.commands import KbCommands
from src.knowledge.docs_repo import DocsRepo
from src.knowledge.gap_agent import GapFiller
from src.knowledge.sync import GapSyncer
from src.storage.knowledge_gap_store import KnowledgeGap

_AGENT_OUTPUT = json.dumps(
    {
        "found": True,
        "topic": "customer_complaints",
        "title": "客户投诉平台知识库",
        "aliases": ["客诉平台"],
        "keywords": ["投诉"],
        "blocks": (
            "## 处罚人是什么字段（punish_user_id）\n\n"
            "<!-- src: app/BLL/PunishBLL.php -->\n\n"
            "处罚人字段是 `punish_user_id`，存发起处罚操作的用户工号。"
        ),
        "summary": "新增 1 个块：处罚人字段",
    },
    ensure_ascii=False,
)


class _FakeGapStore:
    """够用的内存版队列，避免整个链路被 MagicMock 的返回值糊住。"""

    def __init__(self, gap: KnowledgeGap):
        self._gaps = {gap.id: gap}

    def get(self, gap_id):
        return self._gaps.get(gap_id)

    def try_claim(self, gap_id, *, from_status, to_status, operator_id=""):
        gap = self._gaps[gap_id]
        if gap.status != from_status:
            return False
        self._gaps[gap_id] = KnowledgeGap(**{**gap.__dict__, "status": to_status})
        return True

    def mark_drafted(self, gap_id, *, doc_path, draft_summary, content_sha256):
        gap = self._gaps[gap_id]
        self._gaps[gap_id] = KnowledgeGap(
            **{
                **gap.__dict__,
                "status": "drafted",
                "doc_path": doc_path,
                "draft_summary": draft_summary,
                "content_sha256": content_sha256,
            }
        )

    def mark_synced(self, gap_id, *, dify_document_id, dify_batch):
        gap = self._gaps[gap_id]
        self._gaps[gap_id] = KnowledgeGap(
            **{**gap.__dict__, "status": "synced", "dify_document_id": dify_document_id}
        )

    def mark_needs_human(self, gap_id, *, fail_reason):
        gap = self._gaps[gap_id]
        self._gaps[gap_id] = KnowledgeGap(
            **{**gap.__dict__, "status": "needs_human", "fail_reason": fail_reason}
        )

    def mark_failed(self, gap_id, *, fail_reason):
        self.mark_needs_human(gap_id, fail_reason=fail_reason)

    def list_by_status(self, status, limit=20):
        return [g for g in self._gaps.values() if g.status == status]


def test_fill_then_approve_reaches_dify(tmp_path):
    (tmp_path / "menus").mkdir()
    (tmp_path / "index.yaml").write_text("[]", encoding="utf-8")
    docs_repo = DocsRepo(tmp_path)

    gap = KnowledgeGap(
        id=12, user_id="ou_1", chat_id="oc_1", original_query="处罚人是什么字段",
        task_name="customer_complaints", status="pending", doc_path="",
        draft_summary="", fail_reason="", content_sha256="", dify_document_id="",
    )
    gap_store = _FakeGapStore(gap)
    sent: list[str] = []

    dataset_client = MagicMock()
    dataset_client.dataset_id = "ds-1"
    dataset_client.create_document_by_text.return_value = DocumentRef("doc-1", "b-1", "x")
    dataset_client.indexing_status.return_value = "completed"

    doc_store = MagicMock()
    doc_store.get.return_value = None

    commands = KbCommands(
        gap_store=gap_store,
        fill_gap=GapFiller(
            gap_store=gap_store,
            docs_repo=docs_repo,
            notify=lambda _chat, text: sent.append(text),
            repo_path="/fake/fbi",
            run_agent=lambda _prompt: _AGENT_OUTPUT,
        ).fill,
        sync_gap=GapSyncer(
            gap_store=gap_store,
            doc_store=doc_store,
            docs_repo=docs_repo,
            dataset_client=dataset_client,
            notify=lambda _chat, text: sent.append(text),
            sleep=lambda _s: None,
        ).sync,
        notify=lambda _chat, text: sent.append(text),
        executor=lambda fn: fn(),
    )

    # 1) 队列里能看到这条
    assert "#12" in commands.handle("/kb list", chat_id="oc_1", user_id="ou_9")

    # 2) 补全：agent 查证 -> 新建文档 -> 落盘 -> 待确认
    commands.handle("/kb fill 12", chat_id="oc_1", user_id="ou_9")
    assert gap_store.get(12).status == "drafted"
    assert gap_store.get(12).doc_path == "menus/customer_complaints.md"
    assert "punish_user_id" in docs_repo.read_doc("menus/customer_complaints.md")
    # 此时还没推 Dify，必须等人工确认
    dataset_client.create_document_by_text.assert_not_called()

    # 3) 确认后才推送
    commands.handle("/kb approve 12", chat_id="oc_1", user_id="ou_9")
    assert gap_store.get(12).status == "synced"
    pushed = dataset_client.create_document_by_text.call_args.kwargs
    assert "punish_user_id" in pushed["text"]
    assert pushed["name"] == "customer_complaints"

    assert any("/kb approve 12" in msg for msg in sent)


def test_fill_failure_never_writes_to_docs(tmp_path):
    (tmp_path / "menus").mkdir()
    (tmp_path / "index.yaml").write_text("[]", encoding="utf-8")
    docs_repo = DocsRepo(tmp_path)

    gap = KnowledgeGap(
        id=13, user_id="ou_1", chat_id="oc_1", original_query="今天天气怎么样",
        task_name="", status="pending", doc_path="", draft_summary="",
        fail_reason="", content_sha256="", dify_document_id="",
    )
    gap_store = _FakeGapStore(gap)

    commands = KbCommands(
        gap_store=gap_store,
        fill_gap=GapFiller(
            gap_store=gap_store,
            docs_repo=docs_repo,
            notify=lambda _c, _t: None,
            repo_path="/fake/fbi",
            run_agent=lambda _p: json.dumps(
                {"found": False, "reason": "这个问题跟代码库无关"}, ensure_ascii=False
            ),
        ).fill,
        sync_gap=MagicMock(),
        notify=lambda _c, _t: None,
        executor=lambda fn: fn(),
    )

    commands.handle("/kb fill 13", chat_id="oc_1", user_id="ou_9")

    assert gap_store.get(13).status == "needs_human"
    assert docs_repo.iter_docs() == []  # 一个文件都不该被建出来
