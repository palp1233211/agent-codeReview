"""Dify 知识库（dataset）API 客户端。

与 client.py 的区别：那个走 `/chat-messages`，用 `app-` 前缀的**应用**密钥；
这里走 `/datasets/*`，用 `dataset-`/`ds-` 前缀的**知识库**密钥（在知识库页面的
Service API 面板单独创建）。两者不通用，拿应用密钥打这些接口会静默 401，
所以构造时直接拦掉。

路由的单复数不对称是 Dify 的真实设计，不是笔误：
  创建 POST /datasets/{id}/document/create-by-text     ← document 单数
  更新 POST /datasets/{id}/documents/{doc}/update-by-text ← documents 复数
（下划线写法 create_by_text 等在 Dify 侧已标记 Deprecated，不要用。）
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests

# Dify 侧校验 max_tokens >= 50，本地先拦一道，省得跑到线上才 500
_MIN_MAX_TOKENS = 50
_DEFAULT_MAX_TOKENS = 1000
_DEFAULT_TIMEOUT = 60

# Dify 用 doc_language 填进摘要提示词的 {language} 占位符。不传的话默认 English，
# 中文文档会被生成英文摘要——中文提问对英文摘要，跨语种匹配明显更差。
_DEFAULT_DOC_LANGUAGE = "Chinese"

# 验证检索用的截断长度和候选数。只是想确认"向量存不存在"，不是真的模拟用户提问，
# 截一段正文原文去查即可，不需要很长。
_VERIFY_QUERY_CHARS = 200
_VERIFY_TOP_K = 10

# 分块分隔符。前后的换行不能省——裸 `---` 会匹配到 markdown 表格分隔行 `|---|---|`，
# 把表格从中间切碎（实测：2 块含表格的文档被切成 7 块，3 块内容只有一个 `|`）。
_SEPARATOR = "\n---\n"


@dataclass(frozen=True)
class DocumentRef:
    document_id: str
    batch: str
    name: str


@dataclass(frozen=True)
class DifyDocument:
    document_id: str
    name: str
    indexing_status: str


class DifyDatasetClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        dataset_id: str,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        timeout: int = _DEFAULT_TIMEOUT,
        doc_language: str = _DEFAULT_DOC_LANGUAGE,
    ):
        if api_key.startswith("app-"):
            raise ValueError(
                "DIFY_DATASET_API_KEY 看起来是应用密钥（app- 前缀）。"
                "知识库接口需要在「知识库 → Service API」单独创建的密钥。"
            )
        if max_tokens < _MIN_MAX_TOKENS:
            raise ValueError(f"max_tokens 不能小于 {_MIN_MAX_TOKENS}（Dify 侧限制）")

        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._dataset_id = dataset_id
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._doc_language = doc_language

    @classmethod
    def from_env(cls) -> "DifyDatasetClient":
        return cls(
            base_url=os.environ.get("DIFY_BASE_URL", "http://localhost/v1"),
            api_key=os.environ["DIFY_DATASET_API_KEY"],
            dataset_id=os.environ["DIFY_DATASET_ID"],
            max_tokens=int(os.environ.get("KB_CHUNK_MAX_TOKENS", _DEFAULT_MAX_TOKENS)),
            doc_language=os.environ.get("KB_DOC_LANGUAGE", _DEFAULT_DOC_LANGUAGE),
        )

    @property
    def dataset_id(self) -> str:
        """同步映射按 dataset 隔离，dev/prod 用不同知识库时不会串号。"""
        return self._dataset_id

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _process_rule(self) -> dict[str, Any]:
        """按独占一行的 `---` 分块，与本地知识文档的写法保持一致。

        **分隔符必须带上前后换行**。Dify 是纯字符串匹配，用裸的 `---` 会命中
        markdown 表格的分隔行 `|---|---|`，从表格中间劈开——实测一篇两块、含两张
        表的文档会被切成 7 块，其中 3 块内容只有一个 `|`。带换行后精确切 2 块。

        另注意 Dify 的切分器是递归的：超过 max_tokens 的块会被再切一刀，
        切开后表格会丢表头。所以写入端也要控制块长度（见 gap_agent 的校验）。
        """
        return {
            "mode": "custom",
            "rules": {
                "pre_processing_rules": [
                    {"id": "remove_extra_spaces", "enabled": True},
                    {"id": "remove_urls_emails", "enabled": False},
                ],
                "segmentation": {
                    "separator": _SEPARATOR,
                    "max_tokens": self._max_tokens,
                    "chunk_overlap": 50,
                },
            },
        }

    @staticmethod
    def _unwrap(resp: requests.Response) -> dict[str, Any]:
        data = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(
                f"dify dataset api failed [{data.get('code')}]: {data.get('message')}"
            )
        return data

    def create_document_by_text(self, *, name: str, text: str) -> DocumentRef:
        """新建文档。数据集第一篇必须带 indexing_technique，否则 Dify 报错。"""
        resp = requests.post(
            f"{self._base_url}/datasets/{self._dataset_id}/document/create-by-text",
            headers=self._headers(),
            json={
                "name": name,
                "text": text,
                "indexing_technique": "high_quality",
                "doc_form": "text_model",
                "doc_language": self._doc_language,
                "process_rule": self._process_rule(),
            },
            timeout=self._timeout,
        )
        return self._to_ref(self._unwrap(resp), fallback_name=name)

    def update_document_by_text(
        self, *, document_id: str, name: str, text: str
    ) -> DocumentRef:
        """全量覆盖已有文档。

        **永远走更新，不要删了重建**——重建会换掉 document_id，检索历史和引用全断。
        """
        resp = requests.post(
            f"{self._base_url}/datasets/{self._dataset_id}/documents/{document_id}/update-by-text",
            headers=self._headers(),
            json={
                # Dify 要求：带 text 时 name 必填
                "name": name,
                "text": text,
                "doc_language": self._doc_language,
                "process_rule": self._process_rule(),
            },
            timeout=self._timeout,
        )
        return self._to_ref(self._unwrap(resp), fallback_name=name)

    def list_documents(self, keyword: str = "", limit: int = 100) -> list[DifyDocument]:
        resp = requests.get(
            f"{self._base_url}/datasets/{self._dataset_id}/documents",
            headers=self._headers(),
            params={"keyword": keyword, "limit": limit},
            timeout=self._timeout,
        )
        return [
            DifyDocument(
                document_id=item.get("id", ""),
                name=item.get("name", ""),
                indexing_status=item.get("indexing_status", ""),
            )
            for item in self._unwrap(resp).get("data", [])
        ]

    def indexing_status(self, batch: str) -> str:
        """查一批文档的索引进度。

        create/update 返回 200 只代表**入队**，索引是异步的。不查这个就报「已同步」，
        用户可能拿到一篇状态为 error 的文档还以为成功了。
        """
        resp = requests.get(
            f"{self._base_url}/datasets/{self._dataset_id}/documents/{batch}/indexing-status",
            headers=self._headers(),
            timeout=self._timeout,
        )
        items = self._unwrap(resp).get("data", [])
        if not items:
            return "unknown"

        statuses = [item.get("indexing_status", "") for item in items]
        if "error" in statuses:
            return "error"
        if all(s == "completed" for s in statuses):
            return "completed"
        return next((s for s in statuses if s != "completed"), "unknown")

    def verify_retrievable(self, document_id: str, sample_text: str) -> bool:
        """`indexing_status=completed` 不代表这篇文档真的能被检索到。

        实测碰到过：Weaviate 磁盘写满进入只读模式、或者新写入的向量要等 Weaviate
        重启才刷新内存缓存——这两种情况下 Dify 都照样把文档标记成 completed，
        用户问相关问题却查不到。这里用推送内容本身的一段原文，不设分数阈值、不
        走 rerank，做一次语义检索，只确认这篇文档的 segment 有没有出现在候选里，
        而不是相信状态字段。
        """
        query = sample_text.strip()[:_VERIFY_QUERY_CHARS]
        if not query:
            return True

        resp = requests.post(
            f"{self._base_url}/datasets/{self._dataset_id}/hit-testing",
            headers=self._headers(),
            json={
                "query": query,
                "retrieval_model": {
                    "search_method": "semantic_search",
                    "reranking_enable": False,
                    "top_k": _VERIFY_TOP_K,
                    "score_threshold_enabled": False,
                    "score_threshold": 0,
                },
            },
            timeout=self._timeout,
        )
        records = self._unwrap(resp).get("records", [])
        return any(r.get("segment", {}).get("document_id") == document_id for r in records)

    @staticmethod
    def _to_ref(payload: dict[str, Any], *, fallback_name: str) -> DocumentRef:
        document = payload.get("document") or {}
        return DocumentRef(
            document_id=document.get("id", ""),
            batch=payload.get("batch", ""),
            name=document.get("name", fallback_name),
        )
