"""识别飞书消息中的 Yunxiao MR 链接。"""
from __future__ import annotations

import re
import os
from urllib.parse import urlparse

from .reviewer import parse_yunxiao_mr_reference

_MR_URL_RE = re.compile(r"https?://[^\s<>]+")
_DEFAULT_MR_HOSTS = {"code.aliyun.com", "codeup.aliyun.com", "devops.aliyun.com"}


def _allowed_hosts() -> set[str]:
    configured = os.getenv("YUNXIAO_MR_ALLOWED_HOSTS", "")
    return {item.strip().lower() for item in configured.split(",") if item.strip()} or _DEFAULT_MR_HOSTS


def extract_mr_reference(text: str) -> tuple[str, str, str] | None:
    """返回 (完整链接, repository_id, local_id)，无法识别时返回 None。"""
    for match in _MR_URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;!?)]}")
        if urlparse(url).netloc.lower() not in _allowed_hosts():
            continue
        try:
            repository_id, local_id = parse_yunxiao_mr_reference(url)
        except ValueError:
            continue
        return url, repository_id, local_id
    return None


def is_review_message(*, chat_id: str, text: str, mentions: list | None, bot_open_id: str | None, review_chat_id: str) -> bool:
    """固定群的 Review 入口：必须是目标群、@机器人且包含合法 MR URL。"""
    if chat_id != review_chat_id or not bot_open_id:
        return False
    if not any(getattr(getattr(item, "id", None), "open_id", None) == bot_open_id for item in mentions or []):
        return False
    return extract_mr_reference(text) is not None
