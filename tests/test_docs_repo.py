"""DocsRepo 测试：路由、源码锚点解析、块追加。用 tmp_path 造真实文件，不 mock 文件系统。"""
import pytest

from src.knowledge.docs_repo import DocsRepo

_INDEX_YAML = """
- topic: customer_complaints
  path: menus/customer_complaints.md
  aliases: [客户投诉, 客诉平台]
  keywords: [complaint, 投诉, 客诉]
- topic: abnormalv2
  path: menus/abnormalv2.md
  aliases: [异常订单]
  keywords: [abnormal, 异常]
"""

_DOC = """# 客户投诉平台知识库

---

## abnormal_message 有哪些关键字段

<!-- src: app/BLL/AbnormalCustomerComplaintBLL.php -->
<!-- updated: 2026-08-01 -->

存储所有处罚记录的主表。

---

## 投诉数据来源有哪些渠道

<!-- src: app/tasks/CustomerComplaintTask.php -->
<!-- src: app/BLL/AbnormalCustomerComplaintsBLL.php -->

从多个渠道自动采集。
"""


def _repo(tmp_path) -> DocsRepo:
    (tmp_path / "menus").mkdir()
    (tmp_path / "index.yaml").write_text(_INDEX_YAML, encoding="utf-8")
    (tmp_path / "menus" / "customer_complaints.md").write_text(_DOC, encoding="utf-8")
    return DocsRepo(tmp_path)


def test_route_matches_topic_exactly(tmp_path):
    entry = _repo(tmp_path).route(task_name="customer_complaints", keyword="", query="")

    assert entry is not None
    assert entry.path == "menus/customer_complaints.md"


def test_route_matches_chinese_alias_in_query(tmp_path):
    entry = _repo(tmp_path).route(task_name="", keyword="", query="客诉平台的处罚金额怎么算")

    assert entry is not None
    assert entry.topic == "customer_complaints"


def test_route_matches_keyword(tmp_path):
    entry = _repo(tmp_path).route(task_name="", keyword="异常", query="")

    assert entry is not None
    assert entry.topic == "abnormalv2"


def test_route_returns_none_when_nothing_matches(tmp_path):
    """匹配不上必须返回 None，让上层走"新建文档"分支，绝不能瞎塞进某篇。"""
    assert _repo(tmp_path).route(task_name="", keyword="", query="今天天气怎么样") is None


def test_route_prefers_entry_with_more_matches(tmp_path):
    entry = _repo(tmp_path).route(task_name="", keyword="投诉", query="客诉平台 complaint 字段")

    assert entry.topic == "customer_complaints"


def test_find_blocks_by_source_locates_referencing_blocks(tmp_path):
    blocks = _repo(tmp_path).find_blocks_by_source(["app/tasks/CustomerComplaintTask.php"])

    assert len(blocks) == 1
    assert blocks[0].heading == "投诉数据来源有哪些渠道"
    assert blocks[0].doc_path == "menus/customer_complaints.md"


def test_find_blocks_by_source_matches_multiple_anchors_in_one_block(tmp_path):
    blocks = _repo(tmp_path).find_blocks_by_source(
        ["app/BLL/AbnormalCustomerComplaintsBLL.php"]
    )

    assert len(blocks) == 1
    assert "app/tasks/CustomerComplaintTask.php" in blocks[0].sources


def test_find_blocks_by_source_ignores_unreferenced_files(tmp_path):
    assert _repo(tmp_path).find_blocks_by_source(["app/BLL/SomethingElse.php"]) == []


def test_append_blocks_adds_separator_and_changes_hash(tmp_path):
    repo = _repo(tmp_path)
    before = repo.doc_sha256("menus/customer_complaints.md")

    after = repo.append_blocks(
        "menus/customer_complaints.md",
        "## 处罚人是什么字段\n\n<!-- src: app/BLL/PunishBLL.php -->\n\n是 punish_user_id。",
    )

    content = repo.read_doc("menus/customer_complaints.md")
    assert content.count("\n---\n") == 3  # 原本 2 个分隔符，追加后 3 个
    assert content.rstrip().endswith("是 punish_user_id。")
    assert after != before


def test_append_blocks_rejects_path_outside_docs_root(tmp_path):
    """路径穿越防护：agent 产出的 doc_path 不可信。"""
    repo = _repo(tmp_path)

    with pytest.raises(ValueError, match="超出知识库目录"):
        repo.append_blocks("../../etc/passwd", "恶意内容")


def test_create_doc_registers_index_entry(tmp_path):
    repo = _repo(tmp_path)

    repo.create_doc(
        topic="parcel_loss",
        title="包裹丢失知识库",
        aliases=["丢件"],
        keywords=["loss", "丢失"],
    )

    # 新建后应能被路由到，否则下次同类问题又会新建一篇重复文档
    entry = DocsRepo(tmp_path).route(task_name="", keyword="丢件", query="")
    assert entry is not None
    assert entry.path == "menus/parcel_loss.md"


# ---------- 业务名（推送时写进每块的来源标注）----------


def test_label_prefers_explicit_display_name(tmp_path):
    (tmp_path / "index.yaml").write_text(
        "- topic: t\n  path: menus/t.md\n  display_name: 本网点处罚统计\n  aliases: [【菜单查询】本网点处罚统计]\n",
        encoding="utf-8",
    )
    entry = DocsRepo(tmp_path).entry_for_path("menus/t.md")

    assert entry.label() == "本网点处罚统计"


def test_label_falls_back_to_shortest_alias(tmp_path):
    """别名里通常同时有带【】的原名和去掉标记的版本，后者更适合做来源标注。"""
    (tmp_path / "index.yaml").write_text(
        "- topic: t\n  path: menus/t.md\n  aliases: [【菜单查询】本网点处罚统计, 本网点处罚统计]\n",
        encoding="utf-8",
    )
    entry = DocsRepo(tmp_path).entry_for_path("menus/t.md")

    assert entry.label() == "本网点处罚统计"


def test_label_falls_back_to_topic_when_no_aliases(tmp_path):
    (tmp_path / "index.yaml").write_text("- topic: parcel_loss\n  path: menus/p.md\n", encoding="utf-8")
    entry = DocsRepo(tmp_path).entry_for_path("menus/p.md")

    assert entry.label() == "parcel_loss"


def test_entry_for_path_returns_none_when_unknown(tmp_path):
    (tmp_path / "index.yaml").write_text("[]", encoding="utf-8")

    assert DocsRepo(tmp_path).entry_for_path("menus/nope.md") is None
