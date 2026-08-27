"""把 Dify 的 answer 转成能发给用户的文案，顺手记录知识盲区。

这是 ws_bot 唯一需要感知的入口——检测、入队、话术都收在这里，ws_bot 只管调用，
保持它"收消息 -> 转发 -> 回话"的单一职责。

Chatflow 的信封把「答上来的」和「没覆盖的」分成两个字段，所以有四种组合：

    content 有 / unknown 空  -> 完整答案，直接发
    content 有 / unknown 有  -> 答了一半：答案照发，缺的部分入队并告知
    content 空 / unknown 有  -> 完全没答上：发提示语，入队
    content 空 / unknown 空  -> 信封是空的，说明编排有问题，发兜底话术
"""
from __future__ import annotations

import logging

import pymysql

from ..dify.intent import DifyEnvelope, parse_envelope
from ..storage.knowledge_gap_store import KnowledgeGapStore

logger = logging.getLogger(__name__)

_EMPTY_ENVELOPE_FALLBACK = "抱歉，我这边处理出了点问题，麻烦稍后再问一次。"

_GAP_ONLY = (
    "这个问题现有知识库还没覆盖到，我已经记下来了（编号 #{gap_id}）。\n"
    "在群里发 `/kb fill {gap_id}` 可以让我去代码库里查证并补充知识。"
)
_GAP_ONLY_NO_ID = "这个问题现有知识库还没覆盖到，我已经记下来了，稍后会补充。"

_PARTIAL_SUFFIX = (
    "\n\n---\n以下部分知识库还没覆盖，已记下（编号 #{gap_id}）：{gap_query}\n"
    "发 `/kb fill {gap_id}` 可以让我去代码库查证。"
)
_PARTIAL_SUFFIX_NO_ID = "\n\n---\n另有部分内容知识库还没覆盖，已记下，稍后会补充。"


def render_answer(
    answer: str,
    *,
    user_id: str,
    chat_id: str,
    message_id: str,
    gap_store: KnowledgeGapStore,
) -> str:
    """返回可直接发给用户的文案。

    判定为信封时**任何情况下都不回传原文**，否则用户会看到一坨 JSON。
    """
    envelope = parse_envelope(answer)
    if envelope is None:
        return answer if answer.strip() else _EMPTY_ENVELOPE_FALLBACK

    if not envelope.has_gap:
        return envelope.content or _EMPTY_ENVELOPE_FALLBACK

    gap_id = _enqueue(
        envelope, user_id=user_id, chat_id=chat_id, message_id=message_id, gap_store=gap_store
    )

    if not envelope.content:
        return _GAP_ONLY.format(gap_id=gap_id) if gap_id else _GAP_ONLY_NO_ID

    # 答了一半：答案照发，缺的部分附在后面说明
    suffix = (
        _PARTIAL_SUFFIX.format(gap_id=gap_id, gap_query=envelope.gap_query)
        if gap_id
        else _PARTIAL_SUFFIX_NO_ID
    )
    return envelope.content + suffix


def _enqueue(
    envelope: DifyEnvelope,
    *,
    user_id: str,
    chat_id: str,
    message_id: str,
    gap_store: KnowledgeGapStore,
) -> int | None:
    """入队并返回编号；失败返回 None。

    入队失败是我们的内部问题，不能因此让用户收不到答复，所以异常在这里咽掉，
    只记日志。
    """
    try:
        gap_id = gap_store.enqueue(
            user_id=user_id, chat_id=chat_id, message_id=message_id, envelope=envelope
        )
    except pymysql.MySQLError:
        logger.exception("知识盲区入队失败: message_id=%s", message_id)
        return None

    if gap_id is None:
        logger.info("该消息此前已记录过知识盲区，跳过入队: message_id=%s", message_id)
    else:
        logger.info("已记录知识盲区: gap_id=%s query=%s", gap_id, envelope.gap_query)
    return gap_id
