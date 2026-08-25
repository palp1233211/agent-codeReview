"""MySQL 存储：本地知识文档 ↔ Dify document_id 的映射。

为什么放数据库而不是跟 index.yaml 放一起：document_id 是**随环境变化的同步状态**
（dev / prod 是两个不同的 dataset，同一篇文档 id 不同）。塞进 git 文件必然引起
合并冲突，还可能把测试环境的 id 带到生产。唯一键带上 dataset_id 就天然隔离了。

分工：index.yaml 管内容路由（人写、入 git），这张表管同步状态（机器写），
靠 doc_path 关联，互不重复。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

import pymysql
import pymysql.cursors

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kb_documents (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doc_key CHAR(64) NOT NULL
        COMMENT 'sha256(dataset_id + doc_path)。直接用两列做唯一键会超 InnoDB 767 字节索引上限',
    doc_path VARCHAR(512) NOT NULL COMMENT '本地 markdown 相对路径',
    dify_dataset_id VARCHAR(64) NOT NULL COMMENT '所属 Dify 知识库，区分 dev/prod',
    dify_document_id VARCHAR(64) NOT NULL COMMENT 'Dify document_id',
    dify_document_name VARCHAR(255) NOT NULL DEFAULT ''
        COMMENT 'Dify 侧的文档名。接管已有文档时必须沿用原名，否则更新会把人家改名',
    content_sha256 CHAR(64) NOT NULL DEFAULT '' COMMENT '最后同步成功的内容哈希，用于幂等',
    last_synced_at DATETIME NULL COMMENT '最后同步成功时间',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_doc_key (doc_key),
    KEY idx_dataset (dify_dataset_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='本地知识文档与 Dify 文档的映射'
"""

_UPSERT_SQL = """
INSERT INTO kb_documents
    (doc_key, doc_path, dify_dataset_id, dify_document_id, dify_document_name,
     content_sha256, last_synced_at)
VALUES (%s, %s, %s, %s, %s, %s, NOW())
ON DUPLICATE KEY UPDATE
    dify_document_id = VALUES(dify_document_id),
    dify_document_name = VALUES(dify_document_name),
    content_sha256 = VALUES(content_sha256),
    last_synced_at = NOW()
"""

_GET_SQL = """
SELECT doc_path, dify_dataset_id, dify_document_id, dify_document_name, content_sha256
FROM kb_documents
WHERE doc_key = %s
"""


def _doc_key(dataset_id: str, doc_path: str) -> str:
    """唯一键。用 \\n 分隔避免 ('a','b/c') 与 ('a/b','c') 撞成同一个 key。"""
    return hashlib.sha256(f"{dataset_id}\n{doc_path}".encode()).hexdigest()


@dataclass(frozen=True)
class KbDocument:
    doc_path: str
    dify_document_id: str
    dify_dataset_id: str
    content_sha256: str
    dify_document_name: str = ""


class KbDocumentStore:
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
    def from_env(cls) -> "KbDocumentStore":
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

    def get(self, *, dataset_id: str, doc_path: str) -> KbDocument | None:
        with self._connect(dict_rows=True) as conn, conn.cursor() as cursor:
            cursor.execute(_GET_SQL, (_doc_key(dataset_id, doc_path),))
            row = cursor.fetchone()

        if not row:
            return None
        return KbDocument(
            doc_path=row["doc_path"],
            dify_document_id=row["dify_document_id"],
            dify_dataset_id=row["dify_dataset_id"],
            content_sha256=row["content_sha256"] or "",
            dify_document_name=row.get("dify_document_name") or "",
        )

    def upsert(
        self,
        *,
        doc_path: str,
        dataset_id: str,
        dify_document_id: str,
        content_sha256: str,
        dify_document_name: str = "",
    ) -> None:
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                _UPSERT_SQL,
                (
                    _doc_key(dataset_id, doc_path),
                    doc_path,
                    dataset_id,
                    dify_document_id,
                    dify_document_name,
                    content_sha256,
                ),
            )
