"""知识盲区流水线的装配：把各部件从环境变量拼成一个可用的 KbCommands。

单独放一个文件是为了让 ws_bot 保持"收消息 -> 转发 -> 回话"的单一职责，
不被一堆 from_env() 和依赖注入淹没。
"""
from __future__ import annotations

import logging
import os
from typing import Any

from ..dify.dataset_client import DifyDatasetClient
from ..storage.kb_document_store import KbDocumentStore
from .commands import KbCommands
from .docs_repo import DocsRepo
from .gap_agent import GapFiller
from .sync import GapSyncer

logger = logging.getLogger(__name__)


def build_kb_commands(*, lark_client: Any, gap_store: Any) -> KbCommands:
    """按环境变量装配 /kb 指令处理器。缺配置时抛 KeyError，由启动流程统一报错。"""

    def notify(chat_id: str, text: str) -> None:
        """后台任务回推进度到飞书。失败只记日志——通知发不出去不该让任务算失败。"""
        try:
            lark_client.send_text_message(chat_id, text)
        except RuntimeError:
            logger.exception("回推知识补全进度失败: chat_id=%s", chat_id)

    # 上次进程挂掉时正在跑的补全任务会永久停在 filling（CAS 只认 pending，
    # 没人会再碰它）。启动时打回 pending，让它能被重新领走。
    reclaimed = gap_store.reclaim_stale(
        older_than_minutes=int(os.environ.get("KB_STALE_MINUTES", "30"))
    )
    if reclaimed:
        logger.warning("回收了 %s 条卡在 filling 的知识补全任务，已打回 pending", reclaimed)

    docs_repo = DocsRepo.from_env()
    dataset_client = DifyDatasetClient.from_env()

    filler = GapFiller(
        gap_store=gap_store,
        docs_repo=docs_repo,
        notify=notify,
        repo_path=os.environ["FBI_REPO_PATH"],
    )
    syncer = GapSyncer(
        gap_store=gap_store,
        doc_store=KbDocumentStore.from_env(),
        docs_repo=docs_repo,
        dataset_client=dataset_client,
        notify=notify,
    )

    return KbCommands(
        gap_store=gap_store,
        fill_gap=filler.fill,
        sync_gap=syncer.sync,
        notify=notify,
        max_concurrent_fills=int(os.environ.get("KB_MAX_CONCURRENT_FILLS", "1")),
    )
