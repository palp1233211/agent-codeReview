"""Code Review 任务和结果的独立 MySQL 存储。"""
from __future__ import annotations

import os
import json
from typing import Any

import pymysql

_UNSET = object()

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS lark_code_review_tasks (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    trigger_message_id VARCHAR(64) NOT NULL,
    chat_id VARCHAR(64) NOT NULL,
    mr_url TEXT NOT NULL,
    repository_id VARCHAR(255) NOT NULL,
    local_id VARCHAR(64) NOT NULL,
    merger_open_id VARCHAR(128) NOT NULL DEFAULT '',
    merger_name VARCHAR(255) NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT 'queued',
    review_result LONGTEXT NULL,
    review_text LONGTEXT NULL,
    review_message_id VARCHAR(64) NULL,
    error_message TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id), UNIQUE KEY uk_trigger_message_id (trigger_message_id),
    UNIQUE KEY uk_review_message_id (review_message_id),
    KEY idx_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='飞书 Code Review 任务与结果'
"""


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
            cursor.execute(_CREATE_TABLE_SQL)

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
        """姓名唯一命中时返回飞书 user_id；无匹配或重名时返回空。"""
        sql = (
            "SELECT DISTINCT feishu_user_id FROM feishu_user "
            "WHERE name=%s AND feishu_user_id<>'' LIMIT 2"
        )
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(sql, (name,))
            rows = cursor.fetchall()
        return str(rows[0][0]) if len(rows) == 1 else ""

    def update_result(self, task_id: int, *, status: str, review_result: Any = _UNSET, review_text: str | object = _UNSET, review_message_id: str | None | object = _UNSET, error_message: str | object = _UNSET, merger_open_id: str | object = _UNSET, merger_name: str | object = _UNSET, mr_url: str | object = _UNSET) -> None:
        values = {"status": status}
        optional = {"review_result": review_result, "review_text": review_text, "review_message_id": review_message_id, "error_message": error_message, "merger_open_id": merger_open_id, "merger_name": merger_name, "mr_url": mr_url}
        values.update({key: value for key, value in optional.items() if value is not _UNSET})
        assignments = ", ".join(f"{key}=%s" for key in values)
        sql = f"UPDATE lark_code_review_tasks SET {assignments} WHERE id=%s"
        params = [json.dumps(value, ensure_ascii=False) if key == "review_result" else value for key, value in values.items()]
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(sql, (*params, task_id))
