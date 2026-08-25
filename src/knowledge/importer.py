"""接管 Dify 里已存在的文档：拉回本地 + 登记索引 + 建立映射。

为什么必须先拉回本地：同步是**全量覆盖**（update-by-text）。如果只建映射不拉内容，
下次补充知识时会拿本地那份（不完整的）内容去覆盖线上，把原有内容抹掉。

Dify 的 service API 没有"取文档原文"的接口，只能按 segment 取。这里把 segment
用 `---` 拼回去——恰好也是我们的分块约定，所以拼出来的本地文件与线上分块边界一致。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import requests
import yaml

from .docs_repo import DocsRepo

logger = logging.getLogger(__name__)

_SEPARATOR = "\n\n---\n\n"
_PAGE_SIZE = 100
_MAX_PAGES = 100  # 兜底，防止 has_more 永远为 true 时死循环
# 括号、空白等噪声统一折成下划线
_NAME_NOISE_RE = re.compile(r"[【】\[\]()（）\s]+")
# 路径分隔符和 .. 必须干掉：文档名来自 Dify，带 / 会让文件写到意外的目录里
_PATH_UNSAFE_RE = re.compile(r"[/\\:*?\"<>|]+")
_DOTS_RE = re.compile(r"\.{2,}")
_SLUG_STRIP_RE = re.compile(r"\.md$", re.IGNORECASE)


@dataclass(frozen=True)
class ImportedDoc:
    doc_path: str
    topic: str
    dify_document_id: str
    dify_document_name: str
    segments: int


def slugify_document_name(name: str) -> str:
    """把 Dify 文档名转成可安全用作文件名的 topic。中文保留，路径相关字符全部中和。"""
    cleaned = _SLUG_STRIP_RE.sub("", name)
    cleaned = _PATH_UNSAFE_RE.sub("_", cleaned)
    cleaned = _DOTS_RE.sub("_", cleaned)
    cleaned = _NAME_NOISE_RE.sub("_", cleaned)
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("_.")
    return cleaned or "untitled"


def fetch_document_text(
    *,
    base_url: str,
    api_key: str,
    dataset_id: str,
    document_id: str,
    timeout: int = 60,
    page_size: int = _PAGE_SIZE,
) -> tuple[str, int]:
    """按 segment 取回文档全文，返回 (markdown, 段数)。

    **必须翻页**：segments 接口默认一页只给 20 段，只取第一页会静默丢掉大半内容，
    而且丢得毫无征兆——文件照样生成，只是短了一截。
    """
    url = f"{base_url.rstrip('/')}/datasets/{dataset_id}/documents/{document_id}/segments"
    headers = {"Authorization": f"Bearer {api_key}"}

    segments: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = requests.get(
            url,
            headers=headers,
            params={"page": page, "limit": page_size},
            timeout=timeout,
        )
        data = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(
                f"取文档分段失败 [{data.get('code')}]: {data.get('message')}"
            )

        segments.extend(data.get("data", []))

        if not data.get("has_more"):
            break
        page += 1
        if page > _MAX_PAGES:
            logger.warning("分段翻页超过 %s 页，提前停止: document_id=%s", _MAX_PAGES, document_id)
            break

    segments.sort(key=lambda s: s.get("position", 0))
    return _SEPARATOR.join(_render_segment(s) for s in segments if s.get("content")), len(
        segments
    )


def _render_segment(segment: dict[str, Any]) -> str:
    """把一个 Dify 分段还原成本地 markdown 块。

    `summary`（Dify 1.12 的 Summary Index）常常是人工逐块写的检索抓手，无法从正文
    重建。这里落成 `<!-- summary: ... -->` 注释保存下来，与 `<!-- src: -->` 同一套约定。
    """
    content = str(segment.get("content") or "").strip()
    summary = str(segment.get("summary") or "").strip()

    if not summary:
        return content
    # 摘要放正文前面，与 src 锚点的位置约定一致
    return f"<!-- summary: {summary} -->\n\n{content}"


class DocumentImporter:
    def __init__(
        self,
        *,
        docs_repo: DocsRepo,
        doc_store: Any,
        dataset_client: Any,
        fetch_text: Any = None,
    ):
        self._docs_repo = docs_repo
        self._doc_store = doc_store
        self._dataset_client = dataset_client
        self._fetch_text = fetch_text

    def import_all(self, *, dry_run: bool = False) -> list[ImportedDoc]:
        """把知识库里所有文档接管过来。已登记过映射的会跳过。"""
        dataset_id = self._dataset_client.dataset_id
        imported: list[ImportedDoc] = []

        for document in self._dataset_client.list_documents():
            topic = slugify_document_name(document.name)
            doc_path = f"menus/{topic}.md"

            if self._doc_store.get(dataset_id=dataset_id, doc_path=doc_path):
                logger.info("已接管过，跳过: %s", document.name)
                continue

            text, segment_count = self._fetch_text(document.document_id)
            if not text.strip():
                logger.warning("文档没有可读分段，跳过: %s", document.name)
                continue

            record = ImportedDoc(
                doc_path=doc_path,
                topic=topic,
                dify_document_id=document.document_id,
                dify_document_name=document.name,
                segments=segment_count,
            )
            imported.append(record)

            if dry_run:
                continue

            self._write_local(record, text)
            self._doc_store.upsert(
                doc_path=doc_path,
                dataset_id=dataset_id,
                dify_document_id=document.document_id,
                dify_document_name=document.name,
                content_sha256=self._docs_repo.doc_sha256(doc_path),
            )

        return imported

    def _write_local(self, record: ImportedDoc, text: str) -> None:
        """写本地文件并登记索引；索引里带上原始文档名作为别名，方便路由命中。"""
        target = self._docs_repo.root / record.doc_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.rstrip() + "\n", encoding="utf-8")

        index_file = self._docs_repo.root / "index.yaml"
        entries = (
            yaml.safe_load(index_file.read_text(encoding="utf-8"))
            if index_file.exists()
            else []
        ) or []

        if any(item.get("topic") == record.topic for item in entries):
            return

        aliases = _aliases_for(record.dify_document_name)
        entries.append(
            {
                "topic": record.topic,
                "path": record.doc_path,
                # 推送时写进每块的业务名，取去掉【】标记后的干净版本
                "display_name": aliases[-1],
                "aliases": aliases,
                "keywords": [],
            }
        )
        index_file.write_text(
            yaml.safe_dump(entries, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


def _aliases_for(document_name: str) -> list[str]:
    """从文档名派生别名：原名，以及去掉【xx】前缀后的主体。"""
    base = _SLUG_STRIP_RE.sub("", document_name).strip()
    aliases = [base]

    without_tag = re.sub(r"^【[^】]*】\s*", "", base).strip()
    if without_tag and without_tag != base:
        aliases.append(without_tag)

    return aliases
