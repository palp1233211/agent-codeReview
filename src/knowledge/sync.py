"""把本地知识文档推送到 Dify 知识库。

只在用户 `/kb approve` 之后才会走到这里——写本地不需要确认（有 git 可回滚），
但推线上知识库必须人工过目。

三个容易踩错的地方：
1. **永远走 update，不删了重建**。重建会换掉 document_id，检索历史和外部引用全断。
2. **HTTP 200 不等于索引完成**。Dify 的索引是异步的，必须轮询 indexing-status，
   否则会给用户报「已同步」而实际文档是 error 状态。
3. **indexing-status=completed 也不等于真能查到**。实测碰到过 Weaviate 磁盘写满
   进入只读模式、以及新向量要等 Weaviate 重启才刷新内存缓存，这两种情况下 Dify
   都照样把文档标记成 completed。所以 completed 之后还要用推送内容本身做一次真实
   检索（`verify_retrievable`），查不到就如实报失败，而不是相信状态字段。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from ..dify.dataset_client import DifyDatasetClient
from .docs_repo import DocsRepo
from .render import render_for_dify

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 2
_MAX_POLLS = 30  # 约 1 分钟，超过就认输并告知用户，不死等


class GapSyncer:
    def __init__(
        self,
        *,
        gap_store: Any,
        doc_store: Any,
        docs_repo: DocsRepo,
        dataset_client: DifyDatasetClient,
        notify: Callable[[str, str], None],
        sleep: Callable[[float], None] = time.sleep,
        max_polls: int = _MAX_POLLS,
    ):
        self._gap_store = gap_store
        self._doc_store = doc_store
        self._docs_repo = docs_repo
        self._dataset_client = dataset_client
        self._notify = notify
        self._sleep = sleep
        self._max_polls = max_polls

    def sync(self, gap: Any, *, chat_id: str) -> None:
        if not gap.doc_path:
            raise ValueError(f"#{gap.id} 没有 doc_path，无法同步（应该先跑 /kb fill）")

        dataset_id = self._dataset_client.dataset_id
        # 推给 Dify 的是剥掉维护注释、补上业务来源的正文；哈希仍按原始文件算，
        # 这样本地改了注释以外的任何内容都会触发重推
        entry = self._docs_repo.entry_for_path(gap.doc_path)
        content = render_for_dify(
            self._docs_repo.read_doc(gap.doc_path),
            source_label=entry.label() if entry else "",
        )
        content_sha256 = self._docs_repo.doc_sha256(gap.doc_path)
        mapping = self._doc_store.get(dataset_id=dataset_id, doc_path=gap.doc_path)

        # 幂等：内容跟上次同步的一致就别重复推，重复推会白白触发一次重新索引
        if mapping and mapping.content_sha256 == content_sha256:
            logger.info("内容未变，跳过推送: gap_id=%s doc=%s", gap.id, gap.doc_path)
            self._finish(gap, chat_id=chat_id, document_id=mapping.dify_document_id, batch="")
            return

        # 接管已有 Dify 文档时沿用原名，否则一次更新就把人家文档改名了
        name = (mapping.dify_document_name if mapping else "") or self._document_name(
            gap.doc_path
        )
        if mapping:
            ref = self._dataset_client.update_document_by_text(
                document_id=mapping.dify_document_id, name=name, text=content
            )
        else:
            ref = self._dataset_client.create_document_by_text(name=name, text=content)

        status = self._wait_for_indexing(ref.batch)
        if status != "completed":
            reason = f"Dify 索引未成功完成（最终状态：{status}）"
            logger.warning("同步失败: gap_id=%s %s", gap.id, reason)
            self._gap_store.mark_failed(gap.id, fail_reason=reason)
            self._notify(
                chat_id,
                f"#{gap.id} 推送到 Dify 后索引没能完成（状态：{status}）。\n"
                f"文档已经传上去了，但可能不可检索。修好后发 `/kb sync {gap.id}` 重试。",
            )
            return

        if not self._dataset_client.verify_retrievable(ref.document_id, content):
            reason = "Dify 索引状态显示已完成，但检索验证没有找到这篇文档，实际可能查不到"
            logger.warning("同步验证失败: gap_id=%s document_id=%s", gap.id, ref.document_id)
            self._gap_store.mark_failed(gap.id, fail_reason=reason)
            # 记住 document_id 但不写真实内容哈希：下次 /kb sync 才会对同一篇文档走
            # update 重试，而不是因为查不到映射重新 create 出一篇重复文档，也不会
            # 被"内容没变"的幂等短路挡住重推
            self._doc_store.upsert(
                doc_path=gap.doc_path,
                dataset_id=dataset_id,
                dify_document_id=ref.document_id,
                content_sha256="",
                dify_document_name=name,
            )
            self._notify(
                chat_id,
                f"#{gap.id} 已推送到 Dify（document_id={ref.document_id}），但检索验证没通过，"
                f"内容可能查不到。等一会发 `/kb sync {gap.id}` 重试；如果还是不行，"
                f"需要有人去检查 Dify/向量库状态（比如磁盘空间、Weaviate 是否只读）。",
            )
            return

        self._doc_store.upsert(
            doc_path=gap.doc_path,
            dataset_id=dataset_id,
            dify_document_id=ref.document_id,
            content_sha256=content_sha256,
            dify_document_name=name,
        )
        self._finish(gap, chat_id=chat_id, document_id=ref.document_id, batch=ref.batch)

    # ---------- 内部 ----------

    def _wait_for_indexing(self, batch: str) -> str:
        if not batch:
            return "completed"

        for _ in range(self._max_polls):
            status = self._dataset_client.indexing_status(batch)
            if status in ("completed", "error"):
                return status
            self._sleep(_POLL_INTERVAL_SECONDS)

        return "timeout"

    def _finish(self, gap: Any, *, chat_id: str, document_id: str, batch: str) -> None:
        self._gap_store.mark_synced(
            gap.id, dify_document_id=document_id, dify_batch=batch
        )
        logger.info("已同步到 Dify: gap_id=%s document_id=%s", gap.id, document_id)
        self._notify(
            chat_id,
            f"#{gap.id} 已同步到 Dify 知识库，现在可以直接问这个问题试试。",
        )

    @staticmethod
    def _document_name(doc_path: str) -> str:
        """用文件名作为 Dify 文档名，保证一个菜单 = 一个 Document 的对应关系直观。"""
        return doc_path.rsplit("/", 1)[-1].removesuffix(".md")
