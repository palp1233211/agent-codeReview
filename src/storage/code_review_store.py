"""Code Review 任务和结果的独立 MySQL 存储。"""
from __future__ import annotations

import os
import json
from dataclasses import dataclass
from typing import Any

import pymysql

_UNSET = object()

_CREATE_TASK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS lark_code_review_tasks (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键 ID',
    trigger_message_id VARCHAR(64) NOT NULL COMMENT '触发审查的飞书消息 ID；CLI 触发时为 cli:UUID',
    chat_id VARCHAR(64) NOT NULL COMMENT '接收审查结果的飞书群 ID',
    mr_url TEXT NOT NULL COMMENT 'Yunxiao MR 完整地址',
    repository_id VARCHAR(255) NOT NULL COMMENT 'Yunxiao 代码库 ID 或代码库路径',
    local_id VARCHAR(64) NOT NULL COMMENT 'MR 在代码库中的编号',
    merger_open_id VARCHAR(128) NOT NULL DEFAULT '' COMMENT 'MR 作者的飞书用户 ID；历史字段名为 merger_open_id',
    merger_name VARCHAR(255) NOT NULL DEFAULT '' COMMENT 'MR 作者姓名；历史字段名为 merger_name',
    status VARCHAR(32) NOT NULL DEFAULT 'queued' COMMENT '任务状态：queued、running、reviewed、sending、sent、failed',
    review_result LONGTEXT NULL COMMENT '完整审查结果，使用 JSON 字符串保存',
    review_text LONGTEXT NULL COMMENT '发送到飞书群的格式化审查文本',
    review_message_id VARCHAR(64) NULL COMMENT '审查结果对应的飞书消息 ID，用于关联引用回复',
    error_message TEXT NULL COMMENT '审查或飞书回传失败原因',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '任务创建时间',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '任务最后更新时间',
    PRIMARY KEY (id), UNIQUE KEY uk_trigger_message_id (trigger_message_id),
    UNIQUE KEY uk_review_message_id (review_message_id),
    KEY idx_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='飞书 Code Review 任务与结果'
"""

_CREATE_FEEDBACK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS lark_code_review_feedback (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键 ID',
    review_task_id BIGINT UNSIGNED NOT NULL COMMENT '关联 lark_code_review_tasks.id',
    feedback_message_id VARCHAR(64) NOT NULL COMMENT '用户反馈消息 ID，用于幂等去重',
    parent_message_id VARCHAR(64) NOT NULL COMMENT '被引用的 Review 结果消息 ID',
    chat_id VARCHAR(64) NOT NULL COMMENT '反馈所在的飞书群 ID',
    sender_open_id VARCHAR(128) NOT NULL DEFAULT '' COMMENT '反馈发送人的飞书 open_id',
    feedback_text LONGTEXT NOT NULL COMMENT '用户引用回复的原文',
    status VARCHAR(32) NOT NULL DEFAULT 'queued' COMMENT '分析状态：queued、analyzing、analyzed、failed',
    verdict VARCHAR(32) NULL COMMENT '审核结论：correct、false_positive、uncertain',
    reason_category VARCHAR(64) NULL COMMENT '误报原因分类；仅 false_positive 时填写',
    reason_summary TEXT NULL COMMENT 'AI 提炼的误报原因；仅 false_positive 时填写',
    confidence DECIMAL(4,3) NULL COMMENT 'AI 判断置信度，范围 0 到 1',
    analysis_raw LONGTEXT NULL COMMENT 'AI 原始 JSON 分析结果',
    error_message TEXT NULL COMMENT '反馈解析、AI 分析或群回复失败原因',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '反馈接收时间',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录最后更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_feedback_message_id (feedback_message_id),
    KEY idx_review_task_created (review_task_id, created_at),
    KEY idx_verdict_created (verdict, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='飞书 Code Review 用户反馈与误报分析'
"""


@dataclass(frozen=True)
class ReviewTaskForFeedback:
    id: int
    review_text: str
    repository_id: str
    local_id: str
    review_comment_content: str


def _feedback_review_text(review_result: Any, notification_text: Any) -> str:
    """Return the full MR review evidence, falling back for legacy task rows."""
    value = review_result
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = None
    if isinstance(value, dict):
        summary = str(value.get("summary") or "").strip()
        if summary:
            return summary
    return str(notification_text or "")


def _review_comment_content(review_result: Any) -> str:
    """Extract the exact original MR comment content saved with this review task."""
    value = review_result
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return ""
    if not isinstance(value, dict):
        return ""
    raw_messages = value.get("raw_messages")
    if isinstance(raw_messages, list):
        for message in reversed(raw_messages):
            if not isinstance(message, dict):
                continue
            if message.get("type") != "tool_use":
                continue
            if message.get("tool") != "mcp__yunxiao__create_change_request_comment":
                continue
            content = (message.get("input") or {}).get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    summary = value.get("summary")
    if isinstance(summary, str) and summary.lstrip().startswith("## 🤖 AI 代码审查报告"):
        return summary.strip()
    return ""

class CodeReviewStore:
    def __init__(self, host: str, port: int, user: str, password: str, database: str, charset: str = "utf8mb4"):
        self._connect_kwargs = dict(host=host, port=port, user=user, password=password, database=database, charset=charset, connect_timeout=5)
        self._ensure_table()

    @classmethod
    def from_env(cls) -> "CodeReviewStore":
        return cls(os.environ["DB_HOST"], int(os.environ.get("DB_PORT", "3306")), os.environ["DB_USERNAME"], os.environ["DB_PASSWORD"], os.environ["DB_DATABASE"], os.environ.get("DB_CHARSET", "utf8mb4"))

    def _connect(self):
        return pymysql.connect(autocommit=True, **self._connect_kwargs)

    def _ensure_table(self) -> None:
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(_CREATE_TASK_TABLE_SQL)
            cursor.execute(_CREATE_FEEDBACK_TABLE_SQL)

    def create_task(self, *, trigger_message_id: str, chat_id: str, mr_url: str, repository_id: str, local_id: str) -> int | None:
        sql = "INSERT INTO lark_code_review_tasks (trigger_message_id, chat_id, mr_url, repository_id, local_id) VALUES (%s, %s, %s, %s, %s)"
        with self._connect() as conn, conn.cursor() as cursor:
            try:
                cursor.execute(sql, (trigger_message_id, chat_id, mr_url, repository_id, local_id))
                return int(cursor.lastrowid)
            except pymysql.err.IntegrityError as exc:
                if exc.args and exc.args[0] == 1062:
                    return None
                raise

    def find_feishu_user_id_by_name(self, name: str) -> str:
        """从现有飞书用户表查询用户；无匹配或重名时返回空。"""
        sql = (
            "SELECT DISTINCT feishu_user_id FROM feishu_user "
            "WHERE name=%s AND feishu_user_id<>'' LIMIT 2"
        )
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(sql, (name,))
            rows = cursor.fetchall()
        return str(rows[0][0]) if len(rows) == 1 else ""

    def find_review_task_for_feedback(
        self,
        *,
        review_message_id: str,
        chat_id: str,
    ) -> ReviewTaskForFeedback | None:
        """按被引用的 Review 消息精确查任务，绝不根据文本猜测关联关系。"""
        sql = (
            "SELECT id, repository_id, local_id, review_result, review_text "
            "FROM lark_code_review_tasks "
            "WHERE review_message_id=%s AND chat_id=%s LIMIT 1"
        )
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(sql, (review_message_id, chat_id))
            row = cursor.fetchone()
        if not row:
            return None
        return ReviewTaskForFeedback(
            id=int(row[0]),
            repository_id=str(row[1]),
            local_id=str(row[2]),
            review_text=_feedback_review_text(row[3], row[4]),
            review_comment_content=_review_comment_content(row[3]),
        )

    def create_feedback(
        self,
        *,
        review_task_id: int,
        feedback_message_id: str,
        parent_message_id: str,
        chat_id: str,
        sender_open_id: str,
        feedback_text: str,
    ) -> int | None:
        """保存引用回复；仅重复事件返回 None，其他数据库错误必须上抛。"""
        sql = (
            "INSERT INTO lark_code_review_feedback "
            "(review_task_id, feedback_message_id, parent_message_id, chat_id, sender_open_id, feedback_text) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        )
        with self._connect() as conn, conn.cursor() as cursor:
            try:
                cursor.execute(
                    sql,
                    (
                        review_task_id,
                        feedback_message_id,
                        parent_message_id,
                        chat_id,
                        sender_open_id,
                        feedback_text,
                    ),
                )
                return int(cursor.lastrowid)
            except pymysql.err.IntegrityError as exc:
                if exc.args and exc.args[0] == 1062:
                    return None
                raise

    def update_feedback(
        self,
        feedback_id: int,
        *,
        status: str,
        verdict: str | object = _UNSET,
        reason_category: str | None | object = _UNSET,
        reason_summary: str | None | object = _UNSET,
        confidence: float | None | object = _UNSET,
        analysis_raw: Any = _UNSET,
        error_message: str | None | object = _UNSET,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        optional = {
            "verdict": verdict,
            "reason_category": reason_category,
            "reason_summary": reason_summary,
            "confidence": confidence,
            "analysis_raw": analysis_raw,
            "error_message": error_message,
        }
        values.update({key: value for key, value in optional.items() if value is not _UNSET})
        assignments = ", ".join(f"{key}=%s" for key in values)
        params = [
            json.dumps(value, ensure_ascii=False)
            if key == "analysis_raw" and value is not None
            else value
            for key, value in values.items()
        ]
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                f"UPDATE lark_code_review_feedback SET {assignments} WHERE id=%s",
                (*params, feedback_id),
            )

    def update_result(self, task_id: int, *, status: str, review_result: Any = _UNSET, review_text: str | object = _UNSET, review_message_id: str | None | object = _UNSET, error_message: str | object = _UNSET, merger_open_id: str | object = _UNSET, merger_name: str | object = _UNSET, mr_url: str | object = _UNSET) -> None:
        values = {"status": status}
        optional = {"review_result": review_result, "review_text": review_text, "review_message_id": review_message_id, "error_message": error_message, "merger_open_id": merger_open_id, "merger_name": merger_name, "mr_url": mr_url}
        values.update({key: value for key, value in optional.items() if value is not _UNSET})
        assignments = ", ".join(f"{key}=%s" for key in values)
        sql = f"UPDATE lark_code_review_tasks SET {assignments} WHERE id=%s"
        params = [json.dumps(value, ensure_ascii=False) if key == "review_result" else value for key, value in values.items()]
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(sql, (*params, task_id))
