"""飞书群 /kb 指令：知识盲区队列的人工触发入口。

指令必须在转发 Dify **之前**拦截，否则会被当成普通提问送进 Chatflow。

慢操作（fill / sync）的执行方式由 executor 注入：生产环境是后台守护线程（飞书
长连接的事件回调必须立刻返回），测试里注入同步执行，省掉线程带来的不确定性。
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_COMMAND_PREFIX_RE = re.compile(r"^/kb(?:\s|$)", re.IGNORECASE)
_ACTIONS_WITH_ID = {"fill", "approve", "sync", "delete"}
_ACTIONS = {"list", "help", *_ACTIONS_WITH_ID}
# 处理中的行有后台线程在跑，删掉会导致它写完文档/落库时找不到行——等它跑完再删
_UNDELETABLE_STATUSES = {"filling", "syncing"}

_USAGE = (
    "知识盲区队列指令：\n"
    "• `/kb list` — 看待补全的问题\n"
    "• `/kb fill <编号> [涉及的项目/方法说明]` — 让我去代码库查证并补充知识"
    "（慢，完成后会在群里通知）。FBI_REPO_PATH 下可能有多个项目子目录，"
    "拿不准该查哪个项目、哪个方法时，把线索写在编号后面，例如："
    "`/kb fill 12 fbi 项目 PunishBLL::getList`\n"
    "• `/kb approve <编号>` — 确认草稿无误，推送到 Dify 知识库\n"
    "• `/kb sync <编号>` — 推送失败后重试\n"
    "• `/kb delete <编号>` — 从队列中删除（只删本地记录，已推送到 Dify 的内容不会被撤回）"
)


@dataclass(frozen=True)
class KbCommand:
    action: str
    gap_id: int | None
    hint: str = ""


def parse_command(text: str) -> KbCommand | None:
    """解析 /kb 指令；不是 /kb 开头返回 None（放行给 Dify）。

    `fill` 之后除了编号，还可以跟一段自由文本（涉及的项目名/方法名），
    原样透传给补全 agent 当提示——FBI_REPO_PATH 下可能挂了不止一个项目，
    agent 自己猜不出该进哪个子目录时就靠这段文本定位。
    """
    if not text:
        return None

    stripped = text.strip()
    if not _COMMAND_PREFIX_RE.match(stripped):
        return None

    tokens = stripped.split(maxsplit=3)
    action = (tokens[1].lower() if len(tokens) >= 2 else "help")
    if action not in _ACTIONS:
        return KbCommand(action="help", gap_id=None)

    gap_id = None
    hint = ""
    if len(tokens) >= 3 and tokens[2].isdigit():
        gap_id = int(tokens[2])
        if len(tokens) == 4:
            hint = tokens[3].strip()

    return KbCommand(action=action, gap_id=gap_id, hint=hint)


def _spawn_daemon(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


class KbCommands:
    def __init__(
        self,
        *,
        gap_store: Any,
        fill_gap: Callable[..., None],
        sync_gap: Callable[..., None],
        notify: Callable[[str, str], None],
        executor: Callable[[Callable[[], None]], None] = _spawn_daemon,
        max_concurrent_fills: int = 1,
    ):
        self._gap_store = gap_store
        self._fill_gap = fill_gap
        self._sync_gap = sync_gap
        self._notify = notify
        self._executor = executor
        # 每个 fill 会 fork 一个 claude CLI 子进程，这个信号量同时是内存上限
        self._fill_slots = threading.BoundedSemaphore(max_concurrent_fills)

    def handle(self, text: str, *, chat_id: str, user_id: str) -> str | None:
        """返回要立刻回给用户的文案；返回 None 表示这不是指令，应放行给 Dify。"""
        command = parse_command(text)
        if command is None:
            return None

        if command.action == "list":
            return self._handle_list()
        if command.action == "fill":
            return self._handle_fill(
                command.gap_id, chat_id=chat_id, user_id=user_id, hint=command.hint
            )
        if command.action in ("approve", "sync"):
            return self._handle_sync(
                command.gap_id, chat_id=chat_id, user_id=user_id, action=command.action
            )
        if command.action == "delete":
            return self._handle_delete(command.gap_id)
        return _USAGE

    # ---------- 各指令 ----------

    def _handle_list(self) -> str:
        pending = self._gap_store.list_by_status("pending")
        drafted = self._gap_store.list_by_status("drafted")
        filling = self._gap_store.list_by_status("filling")
        needs_human = self._gap_store.list_by_status("needs_human")
        failed = self._gap_store.list_by_status("failed")

        if not (pending or drafted or filling or needs_human or failed):
            return "队列是空的，当前没有待补全的问题。"

        lines: list[str] = []
        if pending:
            lines.append("待补全（发 `/kb fill <编号>` 开始）：")
            lines += [f"  #{g.id} {g.original_query}" for g in pending]
        if filling:
            # 展示出来，否则进程中途挂掉的任务会静默消失，没人知道它卡在哪
            lines.append("正在查证中：")
            lines += [f"  #{g.id} {g.original_query}" for g in filling]
        if drafted:
            lines.append("待确认（发 `/kb approve <编号>` 推送到 Dify）：")
            lines += [f"  #{g.id} {g.original_query} — {g.draft_summary}" for g in drafted]
        if needs_human:
            lines.append("需要人工介入：")
            lines += [f"  #{g.id} {g.original_query} — {g.fail_reason}" for g in needs_human]
        if failed:
            lines.append("执行失败：")
            lines += [f"  #{g.id} {g.original_query} — {g.fail_reason}" for g in failed]

        return "\n".join(lines)

    def _handle_fill(
        self, gap_id: int | None, *, chat_id: str, user_id: str, hint: str = ""
    ) -> str:
        if gap_id is None:
            return f"缺少编号。用法：`/kb fill <编号> [涉及的项目/方法说明]`\n\n{_USAGE}"

        gap = self._gap_store.get(gap_id)
        if gap is None:
            return f"找不到编号 #{gap_id}，用 `/kb list` 看看现在有哪些。"

        if not self._gap_store.try_claim(
            gap_id, from_status="pending", to_status="filling", operator_id=user_id
        ):
            return f"#{gap_id} 已经在处理或已处理过了（当前状态：{gap.status}）。"

        self._executor(lambda: self._run_fill(gap, chat_id=chat_id, hint=hint))
        extra = f"\n已收到线索：{hint}" if hint else ""
        return f"已开始查证 #{gap_id}：{gap.original_query}{extra}\n查完会在群里告诉你结果。"

    def _handle_sync(
        self, gap_id: int | None, *, chat_id: str, user_id: str, action: str
    ) -> str:
        if gap_id is None:
            return f"缺少编号。用法：`/kb {action} <编号>`\n\n{_USAGE}"

        gap = self._gap_store.get(gap_id)
        if gap is None:
            return f"找不到编号 #{gap_id}，用 `/kb list` 看看现在有哪些。"

        # 只有起草完成的才允许推送，避免把空文档推上线
        from_status = "drafted" if action == "approve" else "approved"
        if not self._gap_store.try_claim(
            gap_id, from_status=from_status, to_status="syncing", operator_id=user_id
        ):
            return (
                f"#{gap_id} 当前状态是 {gap.status}，不能执行 {action}。"
                f"（{action} 只对 {from_status} 状态生效）"
            )

        self._executor(lambda: self._run_sync(gap, chat_id=chat_id))
        return f"正在把 #{gap_id} 推送到 Dify 知识库..."

    def _handle_delete(self, gap_id: int | None) -> str:
        if gap_id is None:
            return f"缺少编号。用法：`/kb delete <编号>`\n\n{_USAGE}"

        gap = self._gap_store.get(gap_id)
        if gap is None:
            return f"找不到编号 #{gap_id}，用 `/kb list` 看看现在有哪些。"

        if gap.status in _UNDELETABLE_STATUSES:
            return f"#{gap_id} 正在处理中（{gap.status}），等它跑完再删。"

        if not self._gap_store.delete(gap_id):
            return f"#{gap_id} 删除失败，可能刚被别人删掉了。"

        return f"已删除 #{gap_id}：{gap.original_query}"

    # ---------- 后台执行 ----------

    def _run_fill(self, gap: Any, *, chat_id: str, hint: str = "") -> None:
        with self._fill_slots:
            try:
                self._fill_gap(gap, chat_id=chat_id, hint=hint)
            except Exception:
                # 后台线程里的异常没人接，必须自己兜住并落库，否则条目会永远卡在 filling
                logger.exception("补全知识失败: gap_id=%s", gap.id)
                self._fail(
                    gap,
                    chat_id=chat_id,
                    reason="补全过程异常，详见服务日志",
                    message=f"#{gap.id} 补全失败了，具体原因看服务日志。",
                )

    def _run_sync(self, gap: Any, *, chat_id: str) -> None:
        try:
            self._sync_gap(gap, chat_id=chat_id)
        except Exception:
            logger.exception("同步 Dify 失败: gap_id=%s", gap.id)
            self._fail(
                gap,
                chat_id=chat_id,
                reason="推送 Dify 异常，详见服务日志",
                message=f"#{gap.id} 推送 Dify 失败，修好后可以发 `/kb sync {gap.id}` 重试。",
            )

    def _fail(self, gap: Any, *, chat_id: str, reason: str, message: str) -> None:
        """标记失败并通知。

        落库本身也可能失败（行已被删、状态被并发改掉，mark_* 会抛 GapRowMissing）。
        这里单独兜住：落不了库是我们的内部问题，**用户的通知一定要发出去**，
        否则任务看起来就像凭空消失了。
        """
        try:
            self._gap_store.mark_failed(gap.id, fail_reason=reason)
        except Exception:
            logger.exception("标记失败状态也没成功: gap_id=%s", gap.id)

        self._notify(chat_id, message)
