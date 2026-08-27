"""知识盲区补全：跑 agent 读 PHP 代码，校验产出后写入本地知识文档。

**agent 不直接写文件**——它只返回结构化 JSON，由这里校验（锚点齐全、块不超长）
再落盘。把写入权收在 Python 侧，才能真正强制执行分块规则；交给 agent 自己写，
规则就只是提示词里的建议，违反了也没人拦。

写本地不需要人工确认（有 git 可回滚），但推 Dify 必须确认，见 commands.py。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .docs_repo import DocsRepo

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_BARE_JSON_RE = re.compile(r"(\{.*\})", re.DOTALL)
_SRC_ANCHOR_RE = re.compile(r"<!--\s*src:\s*.+?\s*-->", re.IGNORECASE)
_BLOCK_SEPARATOR = "\n---\n"

# Dify 默认 max chunk 800 token；中文+代码标识符混排实测 token/非空白字符 ≈ 0.65，
# 所以 800 token ≈ 1200 字符。留一档余量，超过就要求拆块。
_MAX_BLOCK_CHARS = 1200

_DEFAULT_MAX_TURNS = 60

@dataclass(frozen=True)
class FillResult:
    found: bool
    topic: str = ""
    title: str = ""
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    blocks: str = ""
    summary: str = ""
    reason: str = ""


class AgentOutputError(ValueError):
    """agent 输出不符合约定（无法解析 / 缺锚点 / 块超长）。"""


def parse_agent_output(text: str) -> FillResult:
    """从 agent 的最终输出里抽出约定的 JSON。"""
    payload = _extract_json(text)

    if not payload.get("found"):
        reason = str(payload.get("reason") or "").strip()
        return FillResult(found=False, reason=reason or "agent 未说明原因")

    blocks = str(payload.get("blocks") or "").strip()
    if not blocks:
        raise AgentOutputError("agent 声称查到了，但没有给出知识块内容")

    _validate_blocks(blocks)

    return FillResult(
        found=True,
        topic=str(payload.get("topic") or "").strip(),
        title=str(payload.get("title") or "").strip(),
        aliases=tuple(payload.get("aliases") or ()),
        keywords=tuple(payload.get("keywords") or ()),
        blocks=blocks,
        summary=str(payload.get("summary") or "").strip(),
    )


def _extract_json(text: str) -> dict[str, Any]:
    for pattern in (_FENCE_RE, _BARE_JSON_RE):
        matched = pattern.search(text or "")
        if not matched:
            continue
        try:
            payload = json.loads(matched.group(1))
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload

    raise AgentOutputError("agent 输出里找不到约定格式的 JSON")


def _validate_blocks(blocks: str) -> None:
    """校验每个块都有源码锚点且长度可控。

    没锚点 -> 日后代码变更时无法定位这块知识，等于放弃维护。
    超长   -> Dify 会强制二次切分，切开后表格丢表头，整块作废。
    """
    for raw in blocks.split(_BLOCK_SEPARATOR):
        block = raw.strip()
        if not block:
            continue

        if not _SRC_ANCHOR_RE.search(block):
            heading = block.splitlines()[0][:40]
            raise AgentOutputError(f"知识块缺少 <!-- src: --> 源码锚点：{heading}")

        if len(block) > _MAX_BLOCK_CHARS:
            heading = block.splitlines()[0][:40]
            raise AgentOutputError(
                f"知识块过长（{len(block)} 字符，上限 {_MAX_BLOCK_CHARS}），需拆分：{heading}"
            )


def _run_agent(prompt: str, *, repo_path: str, max_turns: int) -> str:
    """在 fbi 代码库里跑一次 agent，返回最后一段助手输出。

    调用方是 ws_bot 派生的守护线程，线程内没有事件循环，asyncio.run 可以安全新建一个。
    """
    from .. import tools as _tools  # noqa: F401 - importing registers local tools
    from ..agents.runtime import RuntimeOptions, create_agent_runtime
    from ..prompts import load_agent_definition

    definition = load_agent_definition("kb_fill")
    options = RuntimeOptions(
        allowed_tools=list(definition.tools),
        cwd=repo_path,
        max_turns=max_turns,
    )
    runtime = create_agent_runtime()

    async def _collect() -> str:
        messages = await runtime.run(f"{definition.prompt}\n\n---\n\n{prompt}", options)
        for message in reversed(messages):
            if message.get("type") == "assistant":
                return "\n".join(message.get("content", []))
        return ""

    return asyncio.run(_collect())


class GapFiller:
    def __init__(
        self,
        *,
        gap_store: Any,
        docs_repo: DocsRepo,
        notify: Callable[[str, str], None],
        repo_path: str,
        run_agent: Callable[[str], str] | None = None,
        max_turns: int = _DEFAULT_MAX_TURNS,
    ):
        self._gap_store = gap_store
        self._docs_repo = docs_repo
        self._notify = notify
        self._repo_path = repo_path
        self._max_turns = max_turns
        self._run_agent = run_agent or (
            lambda prompt: _run_agent(
                prompt, repo_path=self._repo_path, max_turns=self._max_turns
            )
        )

    @classmethod
    def from_env(cls, *, gap_store: Any, notify: Callable[[str, str], None]) -> "GapFiller":
        return cls(
            gap_store=gap_store,
            docs_repo=DocsRepo.from_env(),
            notify=notify,
            repo_path=os.environ["FBI_REPO_PATH"],
            max_turns=int(os.environ.get("KB_MAX_TURNS", _DEFAULT_MAX_TURNS)),
        )

    def fill(self, gap: Any, *, chat_id: str, hint: str = "") -> None:
        """查证一条知识盲区并写入本地文档；查不出则标 needs_human。

        `hint` 是用户在 `/kb fill <编号> <文本>` 里额外给的线索（通常是涉及的
        项目名/方法名），原样并入 prompt——FBI_REPO_PATH 下可能挂了不止一个
        项目子目录，agent 光凭问题原文猜不出该进哪个目录时就靠它定位。
        """
        raw = self._run_agent(self._build_prompt(gap, hint=hint))

        try:
            result = parse_agent_output(raw)
        except AgentOutputError as exc:
            logger.warning("agent 输出不合约定: gap_id=%s %s", gap.id, exc)
            self._reject(gap, chat_id=chat_id, reason=str(exc))
            return

        if not result.found:
            self._reject(gap, chat_id=chat_id, reason=result.reason)
            return

        doc_path = self._resolve_doc_path(gap, result)
        content_sha256 = self._docs_repo.append_blocks(doc_path, result.blocks)

        self._gap_store.mark_drafted(
            gap.id,
            doc_path=doc_path,
            draft_summary=result.summary,
            content_sha256=content_sha256,
        )
        logger.info("知识盲区已起草: gap_id=%s doc=%s", gap.id, doc_path)
        self._notify(
            chat_id,
            f"#{gap.id} 查证完成：{result.summary}\n"
            f"已写入 {doc_path}（还没推到 Dify）。\n"
            f"确认无误后发 `/kb approve {gap.id}` 推送到知识库。",
        )

    # ---------- 内部 ----------

    def _build_prompt(self, gap: Any, *, hint: str = "") -> str:
        lines = [
            "## 本次要查证的问题",
            "",
            f"用户提问：{gap.original_query}",
        ]
        if gap.task_name:
            lines.append(f"Chatflow 识别的菜单/任务：{gap.task_name}")
        if hint:
            lines.append(f"人工补充线索（涉及的项目/方法，优先按此定位）：{hint}")
        lines += [
            "",
            f"代码库根目录：{self._repo_path}（已是当前工作目录）",
            "",
            "已有知识文档主题（同主题请复用，不要另起新 topic）：",
        ]
        entries = self._docs_repo.load_index()
        lines += (
            [f"- {e.topic}（{'/'.join(e.aliases) or '无别名'}）" for e in entries]
            if entries
            else ["- （目前还没有任何文档）"]
        )
        return "\n".join(lines)

    def _resolve_doc_path(self, gap: Any, result: FillResult) -> str:
        """先按索引路由到已有文档；路由不到才新建，避免同主题散落成多篇。"""
        entry = self._docs_repo.route(
            task_name=result.topic or gap.task_name,
            keyword="",
            query=gap.original_query,
        )
        if entry is not None:
            return entry.path

        topic = result.topic or gap.task_name or f"gap_{gap.id}"
        return self._docs_repo.create_doc(
            topic=topic,
            title=result.title or topic,
            aliases=list(result.aliases),
            keywords=list(result.keywords),
        )

    def _reject(self, gap: Any, *, chat_id: str, reason: str) -> None:
        self._gap_store.mark_needs_human(gap.id, fail_reason=reason)
        self._notify(
            chat_id,
            f"#{gap.id}「{gap.original_query}」我没能从代码里确定答案，已标记为需要人工处理。\n"
            f"原因：{reason}",
        )
