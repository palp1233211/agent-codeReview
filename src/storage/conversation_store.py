"""MySQL 存储：记录每一轮飞书用户与 Dify 的问答。"""
from __future__ import annotations

import os

import pymysql

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS lark_bot_conversations (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id VARCHAR(64) NOT NULL COMMENT '飞书发送者 open_id',
    chat_id VARCHAR(64) NOT NULL COMMENT '飞书会话 chat_id',
    message_id VARCHAR(64) NOT NULL COMMENT '飞书消息 message_id，用于去重',
    conversation_id VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'Dify conversation_id',
    question TEXT NOT NULL COMMENT '用户提问（已去除 mention、清洗过空白）',
    answer TEXT NOT NULL COMMENT 'Dify 回复（已去除 <think> 思考过程）',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_message_id (message_id),
    KEY idx_user_id (user_id),
    KEY idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='飞书机器人对话记录'
"""

_INSERT_SQL = """
INSERT IGNORE INTO lark_bot_conversations
    (user_id, chat_id, message_id, conversation_id, question, answer)
VALUES (%s, %s, %s, %s, %s, %s)
"""


class ConversationStore:
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
    def from_env(cls) -> "ConversationStore":
        return cls(
            host=os.environ["DB_HOST"],
            port=int(os.environ.get("DB_PORT", "3306")),
            user=os.environ["DB_USERNAME"],
            password=os.environ["DB_PASSWORD"],
            database=os.environ["DB_DATABASE"],
            charset=os.environ.get("DB_CHARSET", "utf8mb4"),
        )

    def _connect(self) -> pymysql.connections.Connection:
        return pymysql.connect(autocommit=True, **self._connect_kwargs)

    def _ensure_table(self) -> None:
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(_CREATE_TABLE_SQL)

    def log(
        self,
        *,
        user_id: str,
        chat_id: str,
        message_id: str,
        conversation_id: str,
        question: str,
        answer: str,
    ) -> None:
        """记录一轮问答；同一 message_id 重复写入会被 INSERT IGNORE 静默去重。"""
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                _INSERT_SQL,
                (user_id, chat_id, message_id, conversation_id, question, answer),
            )
