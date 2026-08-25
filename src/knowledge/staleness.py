"""过期知识检测：代码改了，哪些知识块需要重新核实。

依据是每块头上的源码锚点：

    ## 处罚金额怎么算
    <!-- src: app/BLL/PunishBLL.php -->

给一份 git diff，解析出改动的文件，再 grep 锚点定位受影响的块。**只重核这几块**，
其余不动——整篇重跑既慢又会把没动过的内容也搅一遍。

已知局限：没有锚点的块（例如从 Dify 接管进来的存量文档）覆盖不到。宁可漏报也不
误报——把没关系的块标成"受影响"，会让人失去对这个信号的信任。
"""
from __future__ import annotations

import re

from .docs_repo import BlockRef, DocsRepo

# diff --git a/path/to/File.php b/path/to/File.php
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
# 兼容 --name-only 那种纯路径列表
_LOOKS_LIKE_DIFF = "diff --git "


def parse_changed_files(diff_text: str) -> list[str]:
    """从 git diff（或 --name-only 输出）里解析改动的文件路径，按出现顺序去重。"""
    if not diff_text or not diff_text.strip():
        return []

    if _LOOKS_LIKE_DIFF in diff_text:
        paths: list[str] = []
        for old, new in _DIFF_GIT_RE.findall(diff_text):
            paths.extend(p for p in (old, new) if p and p != "dev/null")
    else:
        paths = [line.strip() for line in diff_text.splitlines() if line.strip()]

    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def find_stale_blocks(diff_text: str, docs_repo: DocsRepo) -> list[BlockRef]:
    """返回引用了本次改动文件的所有知识块。"""
    changed = parse_changed_files(diff_text)
    if not changed:
        return []
    return docs_repo.find_blocks_by_source(changed)
