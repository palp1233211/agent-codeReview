"""Dify Chatflow 结构化信封解析。

Chatflow 的最终节点返回 JSON 而非自然语言，用来把「答上来的部分」和「知识库没
覆盖的部分」分开：

    {"content": "punish_category 是 21。", "unknown": "审核状态有哪些取值"}

两个字段各自独立：content 有值就发给用户，unknown 有值就进补全队列。两者可以
同时有值——那是「答上来一半」的情况，答案照发，缺的部分照记。

同时兼容早期形状 {"intent": "unknown", "original_query": "..."}，那种只能表达
全有或全无。

**安全约束**：一旦判定为信封，调用方就绝不能把原文发给用户（否则用户看到一坨
JSON）。所以判定必须保守：json.loads 成功、结果是 dict、且含已知键，才算信封；
其余一律返回 None 当普通回答透传。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

UNKNOWN_INTENT = "unknown"

# LLM 习惯把 JSON 包在 ```json ... ``` 围栏里，剥掉再解析
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)

_ANSWER_KEYS = ("content", "unknown")
_LEGACY_KEY = "intent"


@dataclass(frozen=True)
class DifyEnvelope:
    """Chatflow 的结构化输出。"""

    content: str
    """给用户看的答复。为空表示知识库完全没答上来。"""

    gap_query: str
    """知识库没覆盖到的问题，非空即需要进补全队列。"""

    raw_json: str
    """原始信封，入库留档，便于事后排查 Chatflow 输出变化。"""

    @property
    def has_gap(self) -> bool:
        return bool(self.gap_query)


def _text(value: Any) -> str:
    """归一化成字符串：null/缺失 -> ''，列表按行拼接。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "\n".join(_text(v) for v in value if _text(v))
    return str(value)


def _strip_fence(answer: str) -> str:
    matched = _FENCE_RE.match(answer)
    return matched.group(1) if matched else answer


def parse_envelope(answer: str) -> DifyEnvelope | None:
    """把 Dify 的 answer 解析为信封；不是信封则返回 None（调用方原样透传）。"""
    if not answer or not answer.strip():
        return None

    try:
        payload = json.loads(_strip_fence(answer))
    except (TypeError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None

    raw = answer.strip()

    if any(key in payload for key in _ANSWER_KEYS):
        return DifyEnvelope(
            content=_text(payload.get("content")),
            gap_query=_text(payload.get("unknown")),
            raw_json=raw,
        )

    return _parse_legacy(payload, raw)


def _parse_legacy(payload: dict[str, Any], raw: str) -> DifyEnvelope | None:
    """早期形状：{"intent": "unknown", "original_query": "..."}。

    非 unknown 的 intent 也要认成信封——它本该被 Chatflow 内部消化掉，漏出来说明
    编排变了，但无论如何都不能把 JSON 发给用户。
    """
    intent = payload.get(_LEGACY_KEY)
    if not isinstance(intent, str) or not intent.strip():
        return None

    is_unknown = intent.strip() == UNKNOWN_INTENT
    return DifyEnvelope(
        content="",
        gap_query=_text(payload.get("original_query")) if is_unknown else "",
        raw_json=raw,
    )
