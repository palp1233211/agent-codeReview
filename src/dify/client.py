"""Dify Chatflow (advanced-chat) API 客户端：按 user_id 维护多轮会话上下文。"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests

# 用户闲置超过这么久，下一句话就开新会话（秒）。Dify 的会话记忆会把上一个话题
# （比如上午问的运单）带进下一个问题，分类器会据此把新问题路由错。
DEFAULT_IDLE_TIMEOUT_SECONDS = 30 * 60

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think_tags(text: str) -> str:
    """去掉推理模型输出里 <think>...</think> 包裹的思考过程，只保留最终内容。"""
    return _THINK_TAG_RE.sub("", text).strip()


@dataclass(frozen=True)
class ChatReply:
    answer: str
    conversation_id: str


class DifyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._idle_timeout_seconds = idle_timeout_seconds
        self._clock = clock
        # user_id -> (conversation_id, 最近一次成功对话的时间)
        self._conversations: dict[str, tuple[str, float]] = {}
        self._conversations_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "DifyClient":
        raw = os.environ.get("DIFY_CONVERSATION_IDLE_MINUTES", "30")
        try:
            idle_minutes = float(raw)
        except ValueError:
            raise ValueError(f"DIFY_CONVERSATION_IDLE_MINUTES 必须是数字（分钟，0 表示不轮换）: {raw!r}") from None
        if idle_minutes < 0:
            raise ValueError(f"DIFY_CONVERSATION_IDLE_MINUTES 不能为负数: {raw!r}")
        return cls(
            base_url=os.environ.get("DIFY_BASE_URL", "http://localhost/v1"),
            api_key=os.environ["DIFY_API_KEY"],
            idle_timeout_seconds=idle_minutes * 60,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _current_conversation_id(self, user_id: str) -> str:
        """取该用户当前会话；闲置超时的会话丢掉，返回空串让 Dify 开新会话。"""
        with self._conversations_lock:
            entry = self._conversations.get(user_id)
            if entry is None:
                return ""
            conversation_id, last_used = entry
            timeout = self._idle_timeout_seconds
            if timeout > 0 and self._clock() - last_used > timeout:
                del self._conversations[user_id]
                return ""
            return conversation_id

    def _forget(self, user_id: str, conversation_id: str) -> None:
        with self._conversations_lock:
            entry = self._conversations.get(user_id)
            if entry is not None and entry[0] == conversation_id:
                del self._conversations[user_id]

    def _post_chat(self, user_id: str, query: str, conversation_id: str) -> requests.Response:
        return requests.post(
            f"{self._base_url}/chat-messages",
            headers=self._headers(),
            json={
                "inputs": {},
                "query": query,
                "response_mode": "blocking",
                "conversation_id": conversation_id,
                "user": user_id,
            },
            timeout=60,
        )

    def chat(self, user_id: str, query: str) -> ChatReply:
        """发送一条消息给 Dify。同一 user_id 在闲置超时前共用一个 conversation_id。

        会话 id 什么时候换：
        - 用户闲置超过 ``idle_timeout_seconds``（默认 30 分钟），下一句开新会话；
        - Dify 报会话不存在（404 not_found，比如被清理了），丢掉旧 id 用新会话重试一次，
          否则这个用户会一直报错直到进程重启；
        - 进程重启后全部重新开始。

        每条飞书消息都在独立线程里处理（见 ws_bot.py），同一用户短时间连发消息时
        可能并发调用到这里，所以对 _conversations 的读写加锁；HTTP 请求本身不加锁，
        避免一个慢请求卡住其他用户。
        """
        conversation_id = self._current_conversation_id(user_id)
        resp = self._post_chat(user_id, query, conversation_id)
        data = resp.json()

        if resp.status_code == 404 and data.get("code") == "not_found" and conversation_id:
            self._forget(user_id, conversation_id)
            conversation_id = ""
            resp = self._post_chat(user_id, query, conversation_id)
            data = resp.json()

        if resp.status_code != 200:
            raise RuntimeError(f"dify chat failed [{data.get('code')}]: {data.get('message')}")

        new_conversation_id = data.get("conversation_id") or conversation_id
        if new_conversation_id:
            with self._conversations_lock:
                self._conversations[user_id] = (new_conversation_id, self._clock())

        return ChatReply(
            answer=strip_think_tags(data.get("answer", "")),
            conversation_id=new_conversation_id,
        )
