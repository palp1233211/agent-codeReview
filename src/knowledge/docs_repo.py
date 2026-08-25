"""本地知识文档仓库：路由、读写、源码锚点检索。

目录结构：

    {KNOWLEDGE_DOCS_DIR}/
        index.yaml            # 主题 -> 文件 的路由索引（入 git，人可读可改）
        menus/{topic}.md      # 一个菜单一个文件 = 一个 Dify Document

文档按 `---` 分块，每块一个可独立回答的知识点，块内用 HTML 注释挂源码锚点：

    ## 处罚人是什么字段
    <!-- src: app/BLL/PunishBLL.php -->
    <!-- updated: 2026-08-18 -->

锚点让"代码改了要更新哪些块"变成一次 grep，而不是全文重读。

索引里只放路由信息，**不放 dify_document_id**——那是随环境变化的同步状态，
放进 git 文件必然引起合并冲突和跨环境串号，它归 MySQL 管。
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_SRC_ANCHOR_RE = re.compile(r"<!--\s*src:\s*(.+?)\s*-->", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_SEPARATOR = "\n---\n"

_INDEX_FILENAME = "index.yaml"
_DOC_SUBDIR = "menus"


@dataclass(frozen=True)
class IndexEntry:
    topic: str
    path: str
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    display_name: str = ""

    def match_terms(self) -> set[str]:
        """所有可用于匹配的词，统一小写。"""
        return {t.lower() for t in (self.topic, *self.aliases, *self.keywords) if t}

    def label(self) -> str:
        """推送时写进每块的业务名。

        优先用显式的 display_name；没有就退到最短的别名——别名里通常既有原始的
        「【菜单查询】本网点处罚统计」也有去掉标记的「本网点处罚统计」，后者更干净。
        """
        if self.display_name:
            return self.display_name
        if self.aliases:
            return min(self.aliases, key=len)
        return self.topic


@dataclass(frozen=True)
class BlockRef:
    doc_path: str
    heading: str
    index: int
    sources: tuple[str, ...] = field(default=())


class DocsRepo:
    def __init__(self, root: Path | str):
        self._root = Path(root).resolve()
        # 两次 fill 打同一个文件会互相覆盖，按文件加锁
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()

    @classmethod
    def from_env(cls) -> "DocsRepo":
        return cls(os.environ["KNOWLEDGE_DOCS_DIR"])

    @property
    def root(self) -> Path:
        return self._root

    # ---------- 路径安全 ----------

    def _resolve(self, doc_path: str) -> Path:
        """把相对路径解析成绝对路径，并拒绝逃出知识库根目录的路径。

        doc_path 可能来自 agent 输出，不可信，必须校验。
        """
        target = (self._root / doc_path).resolve()
        if target != self._root and self._root not in target.parents:
            raise ValueError(f"文档路径超出知识库目录: {doc_path}")
        return target

    # ---------- 索引与路由 ----------

    def load_index(self) -> list[IndexEntry]:
        index_file = self._root / _INDEX_FILENAME
        if not index_file.exists():
            return []

        raw = yaml.safe_load(index_file.read_text(encoding="utf-8")) or []
        return [
            IndexEntry(
                topic=item["topic"],
                path=item["path"],
                aliases=tuple(item.get("aliases") or ()),
                keywords=tuple(item.get("keywords") or ()),
                display_name=item.get("display_name") or "",
            )
            for item in raw
        ]

    def entry_for_path(self, doc_path: str) -> IndexEntry | None:
        """按文件路径反查索引项，同步时取业务名用。"""
        return next((e for e in self.load_index() if e.path == doc_path), None)

    def route(self, *, task_name: str, keyword: str, query: str) -> IndexEntry | None:
        """按 task_name / keyword / 原始问题匹配已有文档，命中最多的胜出。

        匹配不上返回 None——上层据此走"新建文档"分支，而不是硬塞进某篇不相关的。
        """
        haystack = " ".join(filter(None, (task_name, keyword, query))).lower()
        if not haystack.strip():
            return None

        best: IndexEntry | None = None
        best_score = 0
        for entry in self.load_index():
            score = sum(1 for term in entry.match_terms() if term in haystack)
            if score > best_score:
                best, best_score = entry, score

        return best

    def create_doc(
        self,
        *,
        topic: str,
        title: str,
        aliases: list[str] | None = None,
        keywords: list[str] | None = None,
    ) -> str:
        """新建一篇空文档并登记到索引，返回相对路径。

        必须同时写索引，否则下次同类问题路由不到，会又建一篇重复的。
        """
        doc_path = f"{_DOC_SUBDIR}/{topic}.md"
        target = self._resolve(doc_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        if not target.exists():
            target.write_text(f"# {title}\n", encoding="utf-8")

        index_file = self._root / _INDEX_FILENAME
        entries = yaml.safe_load(index_file.read_text(encoding="utf-8")) if index_file.exists() else []
        entries = entries or []

        if not any(item.get("topic") == topic for item in entries):
            entries.append(
                {
                    "topic": topic,
                    "path": doc_path,
                    "aliases": aliases or [],
                    "keywords": keywords or [],
                }
            )
            index_file.write_text(
                yaml.safe_dump(entries, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

        return doc_path

    # ---------- 读写 ----------

    def read_doc(self, doc_path: str) -> str:
        return self._resolve(doc_path).read_text(encoding="utf-8")

    def doc_sha256(self, doc_path: str) -> str:
        """内容哈希，用于判断本地内容是否已同步到 Dify（幂等重试的依据）。"""
        target = self._resolve(doc_path)
        if not target.exists():
            return ""
        return hashlib.sha256(target.read_bytes()).hexdigest()

    def _lock_for(self, doc_path: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks[doc_path]

    def append_blocks(self, doc_path: str, blocks: str) -> str:
        """把新知识块追加到文档末尾，返回追加后的内容哈希。

        整个读-改-写在文件级锁内完成，避免两个 fill 并发追加时互相覆盖。
        """
        target = self._resolve(doc_path)

        with self._lock_for(doc_path):
            target.parent.mkdir(parents=True, exist_ok=True)
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            merged = existing.rstrip() + _SEPARATOR + "\n" + blocks.strip() + "\n"
            target.write_text(merged, encoding="utf-8")

        return self.doc_sha256(doc_path)

    # ---------- 源码锚点 ----------

    def iter_docs(self) -> list[str]:
        docs_dir = self._root / _DOC_SUBDIR
        if not docs_dir.exists():
            return []
        return sorted(
            str(p.relative_to(self._root)) for p in docs_dir.rglob("*.md")
        )

    def find_blocks_by_source(self, source_paths: list[str]) -> list[BlockRef]:
        """找出引用了给定源码文件的所有知识块（git diff 驱动的过期检测入口）。"""
        wanted = {p.strip() for p in source_paths if p.strip()}
        if not wanted:
            return []

        found: list[BlockRef] = []
        for doc_path in self.iter_docs():
            blocks = self.read_doc(doc_path).split(_SEPARATOR)
            for idx, block in enumerate(blocks):
                sources = tuple(_SRC_ANCHOR_RE.findall(block))
                if not wanted.intersection(sources):
                    continue

                heading = _HEADING_RE.search(block)
                found.append(
                    BlockRef(
                        doc_path=doc_path,
                        heading=heading.group(1) if heading else "",
                        index=idx,
                        sources=sources,
                    )
                )

        return found
