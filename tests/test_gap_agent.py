"""GapFiller 测试：agent 产出的内容必须先过校验才能落盘。"""
import json
from unittest.mock import MagicMock

from src.knowledge.docs_repo import DocsRepo
from src.knowledge.gap_agent import GapFiller
from src.storage.knowledge_gap_store import KnowledgeGap

_INDEX_YAML = """
- topic: customer_complaints
  path: menus/customer_complaints.md
  aliases: [客诉平台]
  keywords: [投诉]
"""

_GOOD_BLOCKS = (
    "## 处罚人是什么字段（punish_user_id）\n\n"
    "<!-- src: app/BLL/PunishBLL.php -->\n\n"
    "处罚人字段是 `punish_user_id`，存的是发起处罚操作的用户工号。"
)


def _gap() -> KnowledgeGap:
    return KnowledgeGap(
        id=12,
        user_id="ou_1",
        chat_id="oc_1",
        original_query="处罚人是什么字段",
        task_name="customer_complaints",
        status="filling",
        doc_path="",
        draft_summary="",
        fail_reason="",
        content_sha256="",
        dify_document_id="",
    )


def _repo(tmp_path) -> DocsRepo:
    (tmp_path / "menus").mkdir()
    (tmp_path / "index.yaml").write_text(_INDEX_YAML, encoding="utf-8")
    (tmp_path / "menus" / "customer_complaints.md").write_text(
        "# 客户投诉平台知识库\n", encoding="utf-8"
    )
    return DocsRepo(tmp_path)


def _filler(tmp_path, agent_output: str, gap_store=None):
    return GapFiller(
        gap_store=gap_store or MagicMock(),
        docs_repo=_repo(tmp_path),
        notify=MagicMock(),
        repo_path="/fake/fbi",
        run_agent=lambda prompt: agent_output,
    )


def test_appends_blocks_and_marks_drafted(tmp_path):
    store = MagicMock()
    output = json.dumps(
        {"found": True, "topic": "customer_complaints", "blocks": _GOOD_BLOCKS,
         "summary": "新增 1 个块：处罚人字段"},
        ensure_ascii=False,
    )
    filler = _filler(tmp_path, output, gap_store=store)

    filler.fill(_gap(), chat_id="oc_1")

    store.mark_drafted.assert_called_once()
    kwargs = store.mark_drafted.call_args.kwargs
    assert kwargs["doc_path"] == "menus/customer_complaints.md"
    assert len(kwargs["content_sha256"]) == 64
    assert "punish_user_id" in filler._docs_repo.read_doc("menus/customer_complaints.md")


def test_marks_needs_human_when_agent_finds_nothing(tmp_path):
    store = MagicMock()
    output = json.dumps(
        {"found": False, "reason": "代码里没有这个字段，问题本身有歧义"}, ensure_ascii=False
    )
    filler = _filler(tmp_path, output, gap_store=store)

    filler.fill(_gap(), chat_id="oc_1")

    store.mark_needs_human.assert_called_once()
    assert "歧义" in store.mark_needs_human.call_args.kwargs["fail_reason"]
    store.mark_drafted.assert_not_called()
    # 查不出就绝不写入，文档必须原样
    assert filler._docs_repo.read_doc("menus/customer_complaints.md").strip() == "# 客户投诉平台知识库"


def test_rejects_blocks_without_source_anchor(tmp_path):
    """没有 <!-- src: --> 锚点的块日后无法做过期检测，必须拒收。"""
    store = MagicMock()
    output = json.dumps(
        {"found": True, "topic": "customer_complaints",
         "blocks": "## 处罚人是什么字段\n\n是 punish_user_id。", "summary": "x"},
        ensure_ascii=False,
    )
    filler = _filler(tmp_path, output, gap_store=store)

    filler.fill(_gap(), chat_id="oc_1")

    store.mark_drafted.assert_not_called()
    store.mark_needs_human.assert_called_once()
    assert "锚点" in store.mark_needs_human.call_args.kwargs["fail_reason"]


def test_rejects_oversized_block(tmp_path):
    """超限的块会被 Dify 强制二次切分导致表格丢表头，写入前就要拦住。"""
    store = MagicMock()
    huge = "## 标题\n\n<!-- src: app/BLL/X.php -->\n\n" + ("中文内容" * 5000)
    output = json.dumps(
        {"found": True, "topic": "customer_complaints", "blocks": huge, "summary": "x"},
        ensure_ascii=False,
    )
    filler = _filler(tmp_path, output, gap_store=store)

    filler.fill(_gap(), chat_id="oc_1")

    store.mark_drafted.assert_not_called()
    assert "过长" in store.mark_needs_human.call_args.kwargs["fail_reason"]


def test_handles_unparseable_agent_output(tmp_path):
    store = MagicMock()
    filler = _filler(tmp_path, "我找了一圈，大概是 punish_user_id 吧", gap_store=store)

    filler.fill(_gap(), chat_id="oc_1")

    store.mark_drafted.assert_not_called()
    store.mark_needs_human.assert_called_once()


def test_creates_new_doc_when_topic_is_unknown(tmp_path):
    store = MagicMock()
    blocks = _GOOD_BLOCKS.replace("处罚人", "丢件赔付")
    output = json.dumps(
        {"found": True, "topic": "parcel_loss", "title": "包裹丢失知识库",
         "aliases": ["丢件"], "keywords": ["loss"], "blocks": blocks, "summary": "新建"},
        ensure_ascii=False,
    )
    gap = KnowledgeGap(**{**_gap().__dict__, "task_name": "parcel_loss"})
    filler = _filler(tmp_path, output, gap_store=store)

    filler.fill(gap, chat_id="oc_1")

    assert store.mark_drafted.call_args.kwargs["doc_path"] == "menus/parcel_loss.md"
    # 新建的文档必须同时进索引，否则下次同类问题会再建一篇重复的
    assert filler._docs_repo.route(task_name="", keyword="丢件", query="") is not None


def test_parses_agent_output_wrapped_in_markdown_fence(tmp_path):
    store = MagicMock()
    payload = json.dumps(
        {"found": True, "topic": "customer_complaints", "blocks": _GOOD_BLOCKS, "summary": "x"},
        ensure_ascii=False,
    )
    filler = _filler(tmp_path, f"分析完成：\n```json\n{payload}\n```\n", gap_store=store)

    filler.fill(_gap(), chat_id="oc_1")

    store.mark_drafted.assert_called_once()


def test_hint_is_folded_into_prompt(tmp_path):
    """FBI_REPO_PATH 下可能挂了多个项目子目录，agent 猜不出该进哪个时靠这段线索定位。"""
    store = MagicMock()
    output = json.dumps(
        {"found": True, "topic": "customer_complaints", "blocks": _GOOD_BLOCKS, "summary": "x"},
        ensure_ascii=False,
    )
    captured_prompts = []
    filler = GapFiller(
        gap_store=store,
        docs_repo=_repo(tmp_path),
        notify=MagicMock(),
        repo_path="/fake/root",
        run_agent=lambda prompt: captured_prompts.append(prompt) or output,
    )

    filler.fill(_gap(), chat_id="oc_1", hint="fbi 项目 PunishBLL::getList")

    assert "fbi 项目 PunishBLL::getList" in captured_prompts[0]


def test_no_hint_leaves_prompt_unchanged(tmp_path):
    store = MagicMock()
    output = json.dumps(
        {"found": True, "topic": "customer_complaints", "blocks": _GOOD_BLOCKS, "summary": "x"},
        ensure_ascii=False,
    )
    captured_prompts = []
    filler = GapFiller(
        gap_store=store,
        docs_repo=_repo(tmp_path),
        notify=MagicMock(),
        repo_path="/fake/root",
        run_agent=lambda prompt: captured_prompts.append(prompt) or output,
    )

    filler.fill(_gap(), chat_id="oc_1")

    assert "人工补充线索" not in captured_prompts[0]


def test_notifies_chat_on_success(tmp_path):
    notify = MagicMock()
    output = json.dumps(
        {"found": True, "topic": "customer_complaints", "blocks": _GOOD_BLOCKS,
         "summary": "新增 1 个块"},
        ensure_ascii=False,
    )
    filler = GapFiller(
        gap_store=MagicMock(),
        docs_repo=_repo(tmp_path),
        notify=notify,
        repo_path="/fake/fbi",
        run_agent=lambda prompt: output,
    )

    filler.fill(_gap(), chat_id="oc_1")

    text = notify.call_args[0][1]
    # 通知里要给出下一步指令，否则用户不知道怎么推 Dify
    assert "/kb approve 12" in text
