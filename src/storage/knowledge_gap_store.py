"""MySQL 存储：知识库答不上来的问题队列，以及它的补全状态流转。

状态机（只允许单向前进，回退靠人工改库）：

    pending ──try_claim──> filling ──> drafted ──approve──> approved ──> syncing ──> synced
                              └────────────────────────────> needs_human / failed

`try_claim` 是 CAS 更新（带 from_status 条件），并发下只有一个线程能抢到，
且这个保护跨进程/跨容器有效——纯内存锁做不到这点。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pymysql
import pymysql.cursors

from ..dify.intent import DifyEnvelope

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS lark_bot_knowledge_gaps (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id VARCHAR(64) NOT NULL COMMENT '飞书提问者 open_id',
    chat_id VARCHAR(64) NOT NULL COMMENT '飞书会话 chat_id，用于回推进度',
    message_id VARCHAR(64) NOT NULL COMMENT '飞书消息 message_id，用于去重',
    original_query TEXT NOT NULL COMMENT '用户原始提问',
    keyword VARCHAR(255) NOT NULL DEFAULT '' COMMENT 'Chatflow 提取的关键词',
    task_name VARCHAR(128) NOT NULL DEFAULT '' COMMENT '任务/菜单名',
    country VARCHAR(32) NOT NULL DEFAULT '' COMMENT '国家',
    intent_json TEXT NOT NULL COMMENT 'Chatflow 返回的原始 intent JSON',
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        COMMENT '状态: pending/filling/drafted/approved/syncing/synced/needs_human/failed',
    doc_path VARCHAR(512) NOT NULL DEFAULT '' COMMENT '命中的本地 markdown 相对路径',
    draft_summary TEXT NULL COMMENT '待审批摘要（推送飞书用）',
    content_sha256 CHAR(64) NOT NULL DEFAULT '' COMMENT '写入时文档内容哈希，用于幂等同步',
    dify_document_id VARCHAR(64) NOT NULL DEFAULT '' COMMENT '同步后的 Dify document_id',
    dify_batch VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'Dify 索引 batch，用于查询索引状态',
    fail_reason TEXT NULL COMMENT 'needs_human / failed 的原因',
    operator_id VARCHAR(64) NOT NULL DEFAULT '' COMMENT '执行 /kb fill|approve 的 open_id',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_message_id (message_id),
    KEY idx_status_created (status, created_at),
    KEY idx_task_name (task_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='飞书机器人知识盲区待补充队列'
"""

_INSERT_SQL = """
INSERT IGNORE INTO lark_bot_knowledge_gaps
    (user_id, chat_id, message_id, original_query, keyword, task_name, country, intent_json)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""

_CLAIM_SQL = """
UPDATE lark_bot_knowledge_gaps
SET status = %s, operator_id = %s
WHERE id = %s AND status = %s
"""

_MARK_DRAFTED_SQL = """
UPDATE lark_bot_knowledge_gaps
SET status = 'drafted', doc_path = %s, draft_summary = %s, content_sha256 = %s
WHERE id = %s
"""

_MARK_NEEDS_HUMAN_SQL = """
UPDATE lark_bot_knowledge_gaps
SET status = 'needs_human', fail_reason = %s
WHERE id = %s
"""

_MARK_FAILED_SQL = """
UPDATE lark_bot_knowledge_gaps
SET status = 'failed', fail_reason = %s
WHERE id = %s
"""

_MARK_SYNCED_SQL = """
UPDATE lark_bot_knowledge_gaps
SET status = 'synced', dify_document_id = %s, dify_batch = %s
WHERE id = %s
"""

# 进程在 fill 途中挂掉（重启 / OOM / claude CLI 崩溃）时，行会永久停在 filling：
# CAS 抢占只认 pending，所以不会被重试；/kb list 也不展示 filling，于是静默卡死。
_RECLAIM_STALE_SQL = """
UPDATE lark_bot_knowledge_gaps
SET status = 'pending', operator_id = ''
WHERE status = 'filling' AND updated_at < DATE_SUB(NOW(), INTERVAL %s MINUTE)
"""

_DELETE_SQL = "DELETE FROM lark_bot_knowledge_gaps WHERE id = %s"


class GapRowMissing(RuntimeError):
    """状态更新没有命中任何行——行被删掉，或已被并发改成别的状态。

    必须显式抛出：静默放过会让调用方以为落库成功，照常发出「查证完成」的通知，
    用户按提示去 approve 时才发现根本没这条记录。
    """

_SELECT_COLUMNS = """
    id, user_id, chat_id, original_query, task_name, status,
    doc_path, draft_summary, fail_reason, content_sha256, dify_document_id
"""

_LIST_BY_STATUS_SQL = f"""
SELECT {_SELECT_COLUMNS}
FROM lark_bot_knowledge_gaps
WHERE status = %s
ORDER BY created_at ASC
LIMIT %s
"""

_GET_SQL = f"""
SELECT {_SELECT_COLUMNS}
FROM lark_bot_knowledge_gaps
WHERE id = %s
"""


@dataclass(frozen=True)
class KnowledgeGap:
    id: int
    user_id: str
    chat_id: str
    original_query: str
    task_name: str
    status: str
    doc_path: str
    draft_summary: str
    fail_reason: str
    content_sha256: str
    dify_document_id: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "KnowledgeGap":
        """NULL 列统一转空串，避免下游把 None 拼进飞书文案变成 'None'。"""
        return cls(
            id=int(row["id"]),
            user_id=row["user_id"] or "",
            chat_id=row["chat_id"] or "",
            original_query=row["original_query"] or "",
            task_name=row["task_name"] or "",
            status=row["status"] or "",
            doc_path=row["doc_path"] or "",
            draft_summary=row["draft_summary"] or "",
            fail_reason=row["fail_reason"] or "",
            content_sha256=row["content_sha256"] or "",
            dify_document_id=row["dify_document_id"] or "",
        )


class KnowledgeGapStore:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        charset: str = "utf8mb4",
    ):
        self._connect_kwargs = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "database": database,
            "charset": charset,
            "connect_timeout": 5,
        }
        self._ensure_table()

    @classmethod
    def from_env(cls) -> "KnowledgeGapStore":
        return cls(
            host=os.environ["DB_HOST"],
            port=int(os.environ.get("DB_PORT", "3306")),
            user=os.environ["DB_USERNAME"],
            password=os.environ["DB_PASSWORD"],
            database=os.environ["DB_DATABASE"],
            charset=os.environ.get("DB_CHARSET", "utf8mb4"),
        )

    def _connect(self, dict_rows: bool = False) -> pymysql.connections.Connection:
        kwargs = dict(self._connect_kwargs)
        if dict_rows:
            kwargs["cursorclass"] = pymysql.cursors.DictCursor
        return pymysql.connect(autocommit=True, **kwargs)

    def _ensure_table(self) -> None:
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(_CREATE_TABLE_SQL)

    def enqueue(
        self,
        *,
        user_id: str,
        chat_id: str,
        message_id: str,
        envelope: DifyEnvelope,
    ) -> int | None:
        """把一条未命中问题入队；同一 message_id 重复投递返回 None（已记录过）。"""
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                _INSERT_SQL,
                (
                    user_id,
                    chat_id,
                    message_id,
                    envelope.gap_query,
                    "",
                    "",
                    "",
                    envelope.raw_json,
                ),
            )
            # INSERT IGNORE 命中唯一键时不插入，lastrowid 为 0
            return cursor.lastrowid or None

    def try_claim(
        self,
        gap_id: int,
        *,
        from_status: str,
        to_status: str,
        operator_id: str = "",
    ) -> bool:
        """CAS 抢占：只有当前状态等于 from_status 才能推进，返回是否抢到。"""
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(_CLAIM_SQL, (to_status, operator_id, gap_id, from_status))
            return cursor.rowcount == 1

    def mark_drafted(
        self,
        gap_id: int,
        *,
        doc_path: str,
        draft_summary: str,
        content_sha256: str,
    ) -> None:
        self._execute(_MARK_DRAFTED_SQL, (doc_path, draft_summary, content_sha256, gap_id))

    def mark_needs_human(self, gap_id: int, *, fail_reason: str) -> None:
        """查不出答案时调用。绝不允许降级写入臆测内容污染知识库。"""
        self._execute(_MARK_NEEDS_HUMAN_SQL, (fail_reason, gap_id))

    def mark_failed(self, gap_id: int, *, fail_reason: str) -> None:
        self._execute(_MARK_FAILED_SQL, (fail_reason, gap_id))

    def mark_synced(self, gap_id: int, *, dify_document_id: str, dify_batch: str) -> None:
        self._execute(_MARK_SYNCED_SQL, (dify_document_id, dify_batch, gap_id))

    def list_by_status(self, status: str, limit: int = 20) -> list[KnowledgeGap]:
        with self._connect(dict_rows=True) as conn, conn.cursor() as cursor:
            cursor.execute(_LIST_BY_STATUS_SQL, (status, limit))
            return [KnowledgeGap.from_row(row) for row in cursor.fetchall()]

    def get(self, gap_id: int) -> KnowledgeGap | None:
        with self._connect(dict_rows=True) as conn, conn.cursor() as cursor:
            cursor.execute(_GET_SQL, (gap_id,))
            row = cursor.fetchone()
            return KnowledgeGap.from_row(row) if row else None

    def delete(self, gap_id: int) -> bool:
        """从队列中彻底删除一条记录，返回是否真的删到了行。

        只删本地队列行；若该条已经 synced，Dify 知识库里的文档不受影响——
        撤回 Dify 内容走 approve/sync 相反的操作，不是这里的职责。
        """
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(_DELETE_SQL, (gap_id,))
            return cursor.rowcount == 1

    def reclaim_stale(self, older_than_minutes: int = 30) -> int:
        """把卡在 filling 太久的记录打回 pending，返回回收条数。

        在服务启动时调用：上次进程挂掉时正在跑的补全任务，否则永远没人管。
        """
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(_RECLAIM_STALE_SQL, (older_than_minutes,))
            return cursor.rowcount

    def _execute(self, sql: str, params: tuple) -> None:
        """执行状态更新；没命中任何行就抛 GapRowMissing。"""
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(sql, params)
            if cursor.rowcount == 0:
                raise GapRowMissing(
                    f"知识盲区 #{params[-1]} 的状态更新没有命中任何行"
                    "（记录可能已被删除，或已被并发改成其他状态）"
                )
