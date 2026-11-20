"""代码问答的飞书侧调度：先回执，后台读代码，查完追发结果。

读代码要一两分钟，不能占着飞书的事件回调，也不该让 Dify 同步干等（网关有并发限制、
工作流有超时），所以和 `/kb fill` 一样走后台线程；同时用名额限制并发，满了就直说，
不排队——排队的请求会在用户已经忘了的时候才回复。

范围控制：`CODE_QA_ALLOWED_CHATS` 非空时只有这些会话能用；为空表示机器人所在的会话都能用
（机器人本来就只被拉进了特定的群）。
"""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Any

from .code_qa import CodeQA, CodeQADrafter, render_reply

logger = logging.getLogger(__name__)

_ACK = "收到，正在查代码，通常要 1~2 分钟，查完回复你。"
_NOT_ALLOWED = "这个会话还没有开通代码问答。"
_BUSY = "代码问答正在处理别的问题，请 1~2 分钟后再问一次。"
_FAILED = "代码问答这次出错了，请稍后再试；如果一直这样，请联系管理员。"
_DRAFT_NOTE = "\n\n已整理成知识草稿 #{gap_id}，管理员确认后可用 `/kb approve {gap_id}` 入库。"

_DEFAULT_MAX_CONCURRENT = 2


def _thread_executor(job: Callable[[], None]) -> None:
    threading.Thread(target=job, daemon=True).start()


class CodeQaService:
    def __init__(
        self,
        *,
        qa: CodeQA,
        drafter: CodeQADrafter | None,
        send: Callable[[str, str], None],
        allowed_chats: frozenset[str] = frozenset(),
        max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
        executor: Callable[[Callable[[], None]], None] = _thread_executor,
    ):
        self._qa = qa
        self._drafter = drafter
        self._send = send
        self.allowed_chats = allowed_chats
        self.max_concurrent = max_concurrent
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._executor = executor

    @property
    def qa(self) -> CodeQA:
        return self._qa

    @classmethod
    def from_env(
        cls, *, send: Callable[[str, str], None], drafter: CodeQADrafter | None
    ) -> "CodeQaService":
        raw = os.environ.get("CODE_QA_ALLOWED_CHATS", "")
        return cls(
            qa=CodeQA.from_env(),
            drafter=drafter,
            send=send,
            allowed_chats=frozenset(c.strip() for c in raw.split(",") if c.strip()),
            max_concurrent=int(os.environ.get("CODE_QA_MAX_CONCURRENT", _DEFAULT_MAX_CONCURRENT)),
        )

    def submit(
        self,
        question: str,
        *,
        user_id: str,
        chat_id: str,
        message_id: str,
        on_done: Callable[[str], None] | None = None,
    ) -> bool:
        """受理一个代码问题。返回 True 表示已开始后台查询；False 表示被拒（已告知用户）。

        `on_done` 拿到最终发给用户的文案（被拒时就是拒绝语），调用方用它记对话日志。
        """
        if self.allowed_chats and chat_id not in self.allowed_chats:
            self._finish(chat_id, _NOT_ALLOWED, on_done)
            return False

        if not self._slots.acquire(blocking=False):
            self._finish(chat_id, _BUSY, on_done)
            return False

        self._safe_send(chat_id, _ACK)
        try:
            self._executor(
                lambda: self._run(question, user_id, chat_id, message_id, on_done)
            )
        except RuntimeError:  # 线程都起不来，名额不能白白占着
            self._slots.release()
            logger.exception("启动代码问答后台线程失败")
            self._finish(chat_id, _FAILED, on_done)
            return False
        return True

    # ---------- 内部 ----------

    def _run(
        self,
        question: str,
        user_id: str,
        chat_id: str,
        message_id: str,
        on_done: Callable[[str], None] | None,
    ) -> None:
        try:
            text = self._answer(question, user_id, chat_id, message_id)
        except Exception:  # noqa: BLE001 - 线程边界：agent/模型任何故障都要告知用户，不能静默
            logger.exception("代码问答失败: message_id=%s question=%s", message_id, question[:80])
            text = _FAILED
        finally:
            self._slots.release()
        self._finish(chat_id, text, on_done)

    def _answer(self, question: str, user_id: str, chat_id: str, message_id: str) -> str:
        answer = self._qa.ask(question)
        off = self._qa.off_branch_projects()
        warning = (
            f"以下项目当前不在 {self._qa.required_branch} 分支，答案可能和线上不一致：{'、'.join(off)}"
            if off
            else ""
        )
        text = render_reply(answer, versions=self._qa.versions(), warning=warning)

        if answer.found and self._drafter is not None:
            gap_id = self._drafter.save(
                question, answer, user_id=user_id, chat_id=chat_id, message_id=message_id
            )
            if gap_id is not None:
                text += _DRAFT_NOTE.format(gap_id=gap_id)
        return text

    def _finish(self, chat_id: str, text: str, on_done: Callable[[str], None] | None) -> None:
        self._safe_send(chat_id, text)
        if on_done is not None:
            try:
                on_done(text)
            except Exception:  # noqa: BLE001 - 记日志的回调出错不能影响已发出的回复
                logger.exception("代码问答完成回调失败")

    def _safe_send(self, chat_id: str, text: str) -> None:
        try:
            self._send(chat_id, text)
        except (RuntimeError, ValueError):
            logger.exception("回复代码问答消息失败: chat_id=%s", chat_id)
