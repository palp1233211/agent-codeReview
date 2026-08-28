"""过期知识检测：代码一改，哪些知识块需要重新核实。"""
from src.knowledge.docs_repo import DocsRepo
from src.knowledge.staleness import find_stale_blocks, parse_changed_files

_DIFF = """diff --git a/app/BLL/PunishBLL.php b/app/BLL/PunishBLL.php
index 1a2b3c4..5d6e7f8 100644
--- a/app/BLL/PunishBLL.php
+++ b/app/BLL/PunishBLL.php
@@ -10,7 +10,7 @@ class PunishBLL
-        $money = 100;
+        $money = 200;
diff --git a/app/tasks/OtherTask.php b/app/tasks/OtherTask.php
index aaa..bbb 100644
--- a/app/tasks/OtherTask.php
+++ b/app/tasks/OtherTask.php
@@ -1 +1 @@
-old
+new
"""

_INDEX = """
- topic: punish
  path: menus/punish.md
  display_name: 处罚统计
"""

_DOC = """# 处罚知识库

---

## 处罚金额怎么算

<!-- src: app/BLL/PunishBLL.php -->

金额来自配置表。

---

## 处罚人是什么字段

<!-- src: app/BLL/UserBLL.php -->

是 punish_user_id。
"""


def _repo(tmp_path) -> DocsRepo:
    (tmp_path / "menus").mkdir()
    (tmp_path / "index.yaml").write_text(_INDEX, encoding="utf-8")
    (tmp_path / "menus" / "punish.md").write_text(_DOC, encoding="utf-8")
    return DocsRepo(tmp_path)


# ---------- parse_changed_files ----------


def test_parses_paths_from_git_diff():
    assert parse_changed_files(_DIFF) == [
        "app/BLL/PunishBLL.php",
        "app/tasks/OtherTask.php",
    ]


def test_parses_plain_name_only_listing():
    """也接受 git diff --name-only 的输出，省得非要带完整 diff。"""
    text = "app/BLL/PunishBLL.php\napp/tasks/OtherTask.php\n"

    assert parse_changed_files(text) == [
        "app/BLL/PunishBLL.php",
        "app/tasks/OtherTask.php",
    ]


def test_ignores_dev_null_for_deleted_files():
    diff = (
        "diff --git a/app/BLL/Gone.php b/app/BLL/Gone.php\n"
        "deleted file mode 100644\n--- a/app/BLL/Gone.php\n+++ /dev/null\n"
    )

    assert parse_changed_files(diff) == ["app/BLL/Gone.php"]


def test_handles_renames_reporting_both_paths():
    diff = (
        "diff --git a/app/BLL/Old.php b/app/BLL/New.php\n"
        "similarity index 95%\nrename from app/BLL/Old.php\nrename to app/BLL/New.php\n"
    )

    changed = parse_changed_files(diff)
    assert "app/BLL/Old.php" in changed and "app/BLL/New.php" in changed


def test_deduplicates_paths():
    assert parse_changed_files("a.php\na.php\n") == ["a.php"]


def test_returns_empty_for_blank_input():
    assert parse_changed_files("") == []
    assert parse_changed_files("   \n  \n") == []


# ---------- find_stale_blocks ----------


def test_finds_only_blocks_referencing_changed_files(tmp_path):
    stale = find_stale_blocks(_DIFF, _repo(tmp_path))

    assert len(stale) == 1
    assert stale[0].heading == "处罚金额怎么算"
    assert stale[0].doc_path == "menus/punish.md"


def test_returns_empty_when_no_block_references_changed_code(tmp_path):
    diff = "diff --git a/app/BLL/Unrelated.php b/app/BLL/Unrelated.php\n"

    assert find_stale_blocks(diff, _repo(tmp_path)) == []


def test_returns_empty_for_empty_diff(tmp_path):
    assert find_stale_blocks("", _repo(tmp_path)) == []


def test_block_without_anchor_is_never_flagged(tmp_path):
    """没有锚点的块（比如接管进来的存量文档）无法被过期检测覆盖，
    这是已知局限——但绝不能因此误报成"受影响"。"""
    (tmp_path / "menus").mkdir()
    (tmp_path / "index.yaml").write_text(_INDEX, encoding="utf-8")
    (tmp_path / "menus" / "punish.md").write_text(
        "## 无锚点的块\n\n内容。", encoding="utf-8"
    )

    assert find_stale_blocks(_DIFF, DocsRepo(tmp_path)) == []
