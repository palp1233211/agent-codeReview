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
import time
from collections.abc import Callable
from typing import Any

from .code_qa import CodeQA, CodeQADrafter, render_reply

logger = logging.getLogger(__name__)

_ACK = "收到，正在查代码，通常要 1~5 分钟，查完回复你。"
_NOT_ALLOWED = "这个会话还没有开通代码问答。"
_BUSY = "代码问答正在处理别的问题，请 1~2 分钟后再问一次。"
_TIMEOUT = (
    "这个问题涉及的代码比较多，限定时间内没查完。可以缩小范围再问一次，"
    "比如指定国家（泰国/菲律宾）、处罚项或具体的表。"
)
_FAILED = "代码问答这次出错了，请稍后再试；如果一直这样，请联系管理员。"
_DRAFT_NOTE = "\n\n已整理成知识草稿 #{gap_id}，管理员确认后可用 `/kb approve {gap_id}` 入库。"

_DEFAULT_MAX_CONCURRENT = 2
_DEFAULT_CONTEXT_TURNS = 3
_DEFAULT_CONTEXT_MINUTES = 30


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
        context_turns: int = _DEFAULT_CONTEXT_TURNS,
        context_minutes: int = _DEFAULT_CONTEXT_MINUTES,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._qa = qa
        self._drafter = drafter
        self._send = send
        self.allowed_chats = allowed_chats
        self.max_concurrent = max_concurrent
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._executor = executor
        self._context_turns = context_turns
        self._context_seconds = context_minutes * 60
        self._clock = clock
        # user_id -> (最后更新时间, ((问题, 答案), ...))；值整体替换，不原地修改
        self._history: dict[str, tuple[float, tuple[tuple[str, str], ...]]] = {}
        self._history_lock = threading.Lock()

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
            context_minutes=int(os.environ.get("CODE_QA_CONTEXT_MINUTES", _DEFAULT_CONTEXT_MINUTES)),
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
        except TimeoutError:
            logger.warning("代码问答超时: message_id=%s question=%s", message_id, question[:80])
            text = _TIMEOUT
        except Exception:  # noqa: BLE001 - 线程边界：agent/模型任何故障都要告知用户，不能静默
            logger.exception("代码问答失败: message_id=%s question=%s", message_id, question[:80])
            text = _FAILED
        finally:
            self._slots.release()
        self._finish(chat_id, text, on_done)

    def _answer(self, question: str, user_id: str, chat_id: str, message_id: str) -> str:
        answer = self._qa.ask(question, history=self._history_of(user_id))
        self._remember(user_id, question, answer)
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

    def _history_of(self, user_id: str) -> tuple[tuple[str, str], ...]:
        with self._history_lock:
            entry = self._history.get(user_id)
        if entry is None or self._clock() - entry[0] > self._context_seconds:
            return ()
        return entry[1]

    def _remember(self, user_id: str, question: str, answer: Any) -> None:
        """只在 agent 正常返回后记；崩溃的那轮没有可靠答案，不进上下文。"""
        summary = answer.answer if answer.found else f"代码里没有找到依据：{answer.reason}"
        turns = (*self._history_of(user_id), (question, summary))[-self._context_turns:]
        now = self._clock()
        with self._history_lock:
            # 顺手清掉过期的用户，字典不会随着提问的人越来越多而无限长大
            fresh = {u: e for u, e in self._history.items() if now - e[0] <= self._context_seconds}
            self._history = {**fresh, user_id: (now, turns)}

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
