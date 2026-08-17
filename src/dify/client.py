"""Dify Chatflow (advanced-chat) API 客户端：按 user_id 维护多轮会话上下文。"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass

import requests

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think_tags(text: str) -> str:
    """去掉推理模型输出里 <think>...</think> 包裹的思考过程，只保留最终内容。"""
    return _THINK_TAG_RE.sub("", text).strip()


@dataclass(frozen=True)
class ChatReply:
    answer: str
    conversation_id: str


class DifyClient:
    def __init__(self, base_url: str, api_key: str):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._conversations: dict[str, str] = {}
        self._conversations_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "DifyClient":
        return cls(
            base_url=os.environ.get("DIFY_BASE_URL", "http://localhost/v1"),
            api_key=os.environ["DIFY_API_KEY"],
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def chat(self, user_id: str, query: str) -> ChatReply:
        """发送一条消息给 Dify，同一 user_id 的多轮对话共用一个 conversation_id。

        每条飞书消息都在独立线程里处理（见 ws_bot.py），同一用户短时间连发消息时
        可能并发调用到这里，所以对 _conversations 的读写加锁；HTTP 请求本身不加锁，
        避免一个慢请求卡住其他用户。
        """
        with self._conversations_lock:
            conversation_id = self._conversations.get(user_id, "")

        resp = requests.post(
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
        data = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(f"dify chat failed [{data.get('code')}]: {data.get('message')}")

        new_conversation_id = data.get("conversation_id") or conversation_id
        if data.get("conversation_id"):
            with self._conversations_lock:
                self._conversations[user_id] = new_conversation_id

        return ChatReply(
            answer=strip_think_tags(data.get("answer", "")),
            conversation_id=new_conversation_id,
        )
