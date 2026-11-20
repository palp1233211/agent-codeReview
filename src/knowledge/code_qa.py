"""代码问答：知识库答不了、但读代码能确定的问题，现场读代码回答。

和 `gap_agent` 一脉相承——**agent 只返回结构化 JSON，Python 校验后才放行**：
来源必须是开放项目里的真实路径、答案里不能带凭据、可沉淀的知识块过同一套锚点/长度校验。
校验不过就老实说"没查出来"，不把没把握的东西当结论发给用户。

三道防线，缺一不可：
1. 工具只给 Read/Grep/Glob，且读文件前过 `make_read_guard`（不出仓库、不碰密钥文件）；
2. 提示词禁止输出凭据（建议，不可靠）；
3. 输出里命中凭据特征一律拦截（强制）。
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

import pymysql

from ..dify.intent import DifyEnvelope
from .docs_repo import DocsRepo
from .gap_agent import AgentOutputError, _extract_json, _validate_blocks, resolve_doc_path

logger = logging.getLogger(__name__)

DEFAULT_PROJECTS = ("fbi", "bi-common", "ard-api", "ard-etl")
_DEFAULT_MAX_TURNS = 40
_DEFAULT_TIMEOUT_SECONDS = 300
_DEFAULT_BRANCH = "master"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_MAX_ANSWER_CHARS = 4000
_MAX_HISTORY_CHARS = 1500

_SOURCE_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[\w.\-]+)+(?::\d+(?:-\d+)?)?$")

_SENSITIVE_DIRS = frozenset({".git", ".ssh", "config", "configs"})
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks")
_SENSITIVE_PREFIXES = (".env", "id_rsa", "id_ed25519")
_SENSITIVE_WORDS = ("secret", "credential", "password")

_SECRET_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"[a-z][a-z0-9+.\-]*://[^\s:@/]+:[^\s@/]+@",                          # 带账号密码的连接串
        r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]{4,}",
        r"AKIA[0-9A-Z]{16}",                                                   # AWS access key
        r"sk-[A-Za-z0-9]{20,}",                                                # OpenAI 风格密钥
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    )
)


class CodeAnswerError(ValueError):
    """agent 输出不合约定或未通过安全校验。消息会展示给用户，不得回显敏感内容。"""


@dataclass(frozen=True)
class CodeAnswer:
    found: bool
    answer: str = ""
    sources: tuple[str, ...] = ()
    reason: str = ""
    # 以下是可沉淀成知识草稿的部分，整体可选
    topic: str = ""
    title: str = ""
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    blocks: str = ""
    summary: str = ""


# ---------- 敏感路径与读文件守卫 ----------

def is_sensitive_path(path: str) -> bool:
    """路径是否指向密钥/配置/版本库内部等不该让 agent 读的东西。"""
    segments = [s for s in path.replace("\\", "/").split("/") if s]
    if not segments:
        return False
    if any(s.lower() in _SENSITIVE_DIRS for s in segments[:-1]):
        return True
    name = segments[-1].lower()
    return (
        name in _SENSITIVE_DIRS
        or name.startswith(_SENSITIVE_PREFIXES)
        or name.endswith(_SENSITIVE_SUFFIXES)
        or any(word in name for word in _SENSITIVE_WORDS)
    )


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _inside(root: str, target: str) -> bool:
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:  # 不同盘符等
        return False


def make_read_guard(repo_path: str) -> Callable[..., Any]:
    """PreToolUse 钩子：Read/Grep/Glob 只能碰仓库内的非敏感路径。

    路径用 realpath 展开后再比较，符号链接逃不出去。Grep 的 `pattern` 是正则不是路径，不检查。
    """
    root = os.path.realpath(repo_path)

    def _path_fields(tool: str, tool_input: dict[str, Any]) -> list[str]:
        keys = {"Read": ("file_path",), "Grep": ("path", "glob"), "Glob": ("pattern", "path")}
        return [str(tool_input[k]) for k in keys.get(tool, ()) if tool_input.get(k)]

    async def guard(input_data: dict[str, Any], tool_use_id: Any, context: Any) -> dict[str, Any]:
        tool = str(input_data.get("tool_name", ""))
        for raw in _path_fields(tool, input_data.get("tool_input") or {}):
            if is_sensitive_path(raw):
                return _deny(f"禁止访问敏感路径: {raw}")
            if ".." in raw.replace("\\", "/").split("/"):
                return _deny(f"禁止访问仓库外路径: {raw}")
            if os.path.isabs(raw):
                resolved = os.path.realpath(raw)
                if not _inside(root, resolved):
                    return _deny(f"禁止访问仓库外路径: {raw}")
            elif "*" not in raw and "?" not in raw:
                resolved = os.path.realpath(os.path.join(root, raw))
                if not _inside(root, resolved):
                    return _deny(f"禁止访问仓库外路径: {raw}")
        return {}

    return guard


def read_guard_hooks(repo_path: str) -> dict[str, list[Any]]:
    """按当前 provider 的钩子格式包装守卫：Claude SDK 要 HookMatcher，OpenAI 运行时要字典。"""
    guard = make_read_guard(repo_path)
    provider = (os.getenv("AGENT_PROVIDER") or os.getenv("AGENT_SDK") or "claude").lower()
    if provider in {"claude", "claude_sdk", "anthropic"}:
        from claude_agent_sdk import HookMatcher

        return {"PreToolUse": [HookMatcher(matcher="Read|Grep|Glob", hooks=[guard])]}
    return {"PreToolUse": [{"matcher": "*", "hooks": [guard]}]}


# ---------- 输出校验 ----------

def _looks_like_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRET_PATTERNS)


def _validate_sources(sources: Any, allowed_projects: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(sources, (list, tuple)) or not sources:
        raise CodeAnswerError("答案没有给出代码来源，不能当作结论")

    checked: list[str] = []
    for raw in sources:
        source = str(raw).strip()
        path = source.split(":", 1)[0]
        segments = path.split("/")
        ok = (
            bool(_SOURCE_RE.match(source))
            and segments[0] in allowed_projects
            and not any(s in ("", ".", "..") for s in segments)
            and not is_sensitive_path(path)
        )
        if not ok:
            raise CodeAnswerError(f"来源不合规（需为开放项目内、带项目前缀的非敏感路径）：{source[:80]}")
        checked.append(source)
    return tuple(checked)


def parse_code_answer(
    text: str, *, allowed_projects: tuple[str, ...] = DEFAULT_PROJECTS
) -> CodeAnswer:
    """从 agent 的最终输出里取出约定的 JSON 并校验；不合格抛 CodeAnswerError。"""
    try:
        payload = _extract_json(text)
    except AgentOutputError as exc:
        raise CodeAnswerError(str(exc)) from exc

    if not payload.get("found"):
        reason = str(payload.get("reason") or "").strip()
        return CodeAnswer(found=False, reason=reason or "agent 未说明原因")

    answer = str(payload.get("answer") or "").strip()
    if not answer:
        raise CodeAnswerError("agent 声称查到了，但没有给出答案")
    if len(answer) > _MAX_ANSWER_CHARS:
        raise CodeAnswerError(f"答案过长（{len(answer)} 字符，上限 {_MAX_ANSWER_CHARS}）")
    if _looks_like_secret(answer):
        raise CodeAnswerError("答案疑似包含敏感信息（账号/密钥/连接串），已拦截")

    sources = _validate_sources(payload.get("sources"), allowed_projects)
    draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else {}
    blocks = str(draft.get("blocks") or "").strip()
    if blocks:
        if _looks_like_secret(blocks):
            raise CodeAnswerError("知识块疑似包含敏感信息（账号/密钥/连接串），已拦截")
        try:
            _validate_blocks(blocks)
        except AgentOutputError as exc:
            raise CodeAnswerError(str(exc)) from exc

    return CodeAnswer(
        found=True,
        answer=answer,
        sources=sources,
        topic=str(draft.get("topic") or "").strip(),
        title=str(draft.get("title") or "").strip(),
        aliases=tuple(draft.get("aliases") or ()),
        keywords=tuple(draft.get("keywords") or ()),
        blocks=blocks,
        summary=str(draft.get("summary") or "").strip(),
    )


# ---------- 回复文案 ----------

_NOT_EXECUTED_NOTICE = "以上结论来自代码分析，没有实际执行 SQL；用于线上报表前请先小范围核对。"


def render_reply(answer: CodeAnswer, *, versions: tuple[str, ...], warning: str = "") -> str:
    if not answer.found:
        return f"我没能从代码里确定这个问题的答案。\n原因：{answer.reason}"

    lines = [answer.answer, "", "---", "来源："]
    lines += [f"- `{s}`" for s in answer.sources]
    if versions:
        lines.append(f"代码版本：{'、'.join(versions)}")
    if warning:
        lines.append(f"⚠️ {warning}")
    lines.append(_NOT_EXECUTED_NOTICE)
    return "\n".join(lines)


def _git_dir(directory: str) -> str | None:
    """项目的 .git 目录；.git 是文件（工作树/子模块）时跟到它指向的目录。"""
    dot_git = os.path.join(directory, ".git")
    try:
        if os.path.isdir(dot_git):
            return dot_git
        if os.path.isfile(dot_git):
            with open(dot_git, encoding="utf-8") as fh:
                first = fh.readline().strip()
            if first.startswith("gitdir:"):
                target = first.split(":", 1)[1].strip()
                return target if os.path.isabs(target) else os.path.normpath(os.path.join(directory, target))
    except OSError:
        return None
    return None


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


_REF_PREFIX = "ref: "
_HEADS = "refs/heads/"


def git_branch(directory: str) -> str:
    """目录当前检出的分支名；不是 git 仓库、读不到或处于游离 HEAD 都返回空串。

    直接读 `.git/HEAD`，不调用 git 命令——运行镜像里没有 git，而且挂载进容器的仓库
    属主和容器用户不同，git 还会因为 dubious ownership 拒绝工作。
    """
    git_dir = _git_dir(directory)
    if git_dir is None:
        return ""
    head = _read_text(os.path.join(git_dir, "HEAD"))
    if head.startswith(_REF_PREFIX + _HEADS):
        return head[len(_REF_PREFIX + _HEADS):]
    return ""


def git_commit(directory: str) -> str:
    """当前提交的 7 位短哈希；读不到返回空串。先找松散 ref，再找 packed-refs。"""
    git_dir = _git_dir(directory)
    if git_dir is None:
        return ""
    head = _read_text(os.path.join(git_dir, "HEAD"))
    if not head.startswith(_REF_PREFIX):
        return head[:7] if re.fullmatch(r"[0-9a-f]{40}", head) else ""

    ref = head[len(_REF_PREFIX):]
    sha = _read_text(os.path.join(git_dir, *ref.split("/")))
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        sha = ""
        for line in _read_text(os.path.join(git_dir, "packed-refs")).splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1] == ref and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
                sha = parts[0]
                break
    return sha[:7]


def code_versions(repo_path: str, projects: tuple[str, ...]) -> tuple[str, ...]:
    """各项目当前检出的 `项目@短哈希(分支)`，让提问的人知道答案对应哪版代码。读不到就略过。"""
    versions: list[str] = []
    for project in projects:
        directory = os.path.join(repo_path, project)
        branch = git_branch(directory)
        if not branch:
            continue
        commit = git_commit(directory)
        versions.append(f"{project}@{commit}({branch})" if commit else f"{project}({branch})")
    return tuple(versions)


# ---------- agent 调用 ----------

def _run_agent(prompt: str, *, repo_path: str, max_turns: int, timeout_seconds: int) -> str:
    """在代码库根目录跑一次只读 agent，返回最后一段助手输出。

    调用方是后台守护线程，线程里没有事件循环，asyncio.run 可以安全新建一个。
    """
    from .. import tools as _tools  # noqa: F401 - importing registers local tools
    from ..agents.runtime import RuntimeOptions, create_agent_runtime
    from ..prompts import load_agent_definition

    definition = load_agent_definition("code_qa")
    options = RuntimeOptions(
        allowed_tools=list(definition.tools),
        cwd=repo_path,
        max_turns=max_turns,
        hooks=read_guard_hooks(repo_path),
    )
    runtime = create_agent_runtime()

    async def _collect() -> str:
        messages = await runtime.run(f"{definition.prompt}\n\n---\n\n{prompt}", options)
        for message in reversed(messages):
            if message.get("type") == "assistant":
                return "\n".join(message.get("content", []))
        return ""

    async def _bounded() -> str:
        try:
            return await asyncio.wait_for(_collect(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"代码问答超过 {timeout_seconds} 秒仍未完成") from exc

    return asyncio.run(_bounded())


class CodeQA:
    def __init__(
        self,
        *,
        repo_path: str,
        run_agent: Callable[[str], str] | None = None,
        allowed_projects: tuple[str, ...] = DEFAULT_PROJECTS,
        max_turns: int = _DEFAULT_MAX_TURNS,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        required_branch: str = _DEFAULT_BRANCH,
        branch_of: Callable[[str], str] = git_branch,
        allow_any_branch: bool = False,
    ):
        self._repo_path = repo_path
        self._allow_any_branch = allow_any_branch
        self._allowed_projects = allowed_projects
        self._required_branch = required_branch
        self._branch_of = branch_of
        self._run_agent = run_agent or (
            lambda prompt: _run_agent(
                prompt, repo_path=repo_path, max_turns=max_turns, timeout_seconds=timeout_seconds
            )
        )

    @classmethod
    def from_env(cls) -> "CodeQA":
        raw = os.environ.get("CODE_QA_PROJECTS", "")
        projects = tuple(p.strip() for p in raw.split(",") if p.strip()) or DEFAULT_PROJECTS
        return cls(
            repo_path=os.environ["FBI_REPO_PATH"],
            allowed_projects=projects,
            max_turns=int(os.environ.get("CODE_QA_MAX_TURNS", _DEFAULT_MAX_TURNS)),
            timeout_seconds=int(os.environ.get("CODE_QA_TIMEOUT", _DEFAULT_TIMEOUT_SECONDS)),
            required_branch=os.environ.get("CODE_QA_BRANCH", "").strip() or _DEFAULT_BRANCH,
            allow_any_branch=os.environ.get("CODE_QA_ALLOW_ANY_BRANCH", "").strip().lower()
            in _TRUTHY,
        )

    def _present_projects(self) -> tuple[str, ...]:
        return tuple(
            p for p in self._allowed_projects if os.path.isdir(os.path.join(self._repo_path, p))
        )

    @property
    def required_branch(self) -> str:
        return self._required_branch

    @property
    def available_projects(self) -> tuple[str, ...]:
        """开放范围内、本机存在、且检出的正是要求分支（默认 master）的项目。

        答案要和线上口径一致，所以不在要求分支上的项目一律不让 agent 看，也不接受它的来源。
        本机开发可以用 `CODE_QA_ALLOW_ANY_BRANCH` 放开（此时回复会带上分支警告）；生产不要开。
        """
        present = self._present_projects()
        if self._allow_any_branch:
            return present
        return tuple(
            p for p in present
            if self._branch_of(os.path.join(self._repo_path, p)) == self._required_branch
        )

    def off_branch_projects(self) -> tuple[str, ...]:
        """放开分支限制时，实际在回答里用到、但不在要求分支上的项目；没放开就恒为空。"""
        if not self._allow_any_branch:
            return ()
        return tuple(
            p for p in self.available_projects
            if self._branch_of(os.path.join(self._repo_path, p)) != self._required_branch
        )

    def versions(self) -> tuple[str, ...]:
        return code_versions(self._repo_path, self.available_projects)

    def ask(
        self,
        question: str,
        *,
        hint: str = "",
        history: tuple[tuple[str, str], ...] = (),
    ) -> CodeAnswer:
        """读代码回答一个问题。

        `history` 是该用户最近几轮的（问题, 答案），只用来听懂"那个/这个/它"指什么。

        agent 输出没过校验时返回 found=False（对用户诚实说没查出来）；
        agent 本身跑挂了（模型不可用等）则让异常冒出去，由调用方决定怎么告知。
        """
        question = question.strip()
        if not question:
            raise ValueError("问题不能为空")

        if not self.available_projects:
            return CodeAnswer(found=False, reason=self._no_project_reason())

        raw = self._run_agent(self._build_prompt(question, hint, history))
        try:
            return parse_code_answer(raw, allowed_projects=self.available_projects)
        except CodeAnswerError as exc:
            logger.warning("代码问答输出未通过校验: question=%s reason=%s", question[:60], exc)
            return CodeAnswer(found=False, reason=f"查到的结果没通过校验：{exc}")

    def _no_project_reason(self) -> str:
        present = {
            p: self._branch_of(os.path.join(self._repo_path, p)) or "非 git 仓库"
            for p in self._present_projects()
        }
        if not present:
            return "代码库里没有可查阅的项目，暂时无法回答。"
        detail = "、".join(f"{p}（当前 {b}）" for p, b in present.items())
        return (
            f"只回答 {self._required_branch} 分支上的代码，但这些项目没在该分支上：{detail}。"
            "为避免答案和线上不一致，暂不回答。"
        )

    def _project_line(self, project: str) -> str:
        branch = self._branch_of(os.path.join(self._repo_path, project))
        if branch == self._required_branch:
            return f"- {project}"
        return f"- {project}（当前分支 {branch or '未知'}，不是 {self._required_branch}，答案里要说明）"

    def _build_prompt(
        self, question: str, hint: str, history: tuple[tuple[str, str], ...] = ()
    ) -> str:
        lines: list[str] = []
        if history:
            lines += [
                "## 之前的对话（只用来理解“这个/那个/它”指什么，结论仍须读代码确认，不能照抄之前的答案）",
                "",
            ]
            for old_question, old_answer in history:
                lines += [f"- 用户问：{old_question}", f"  回答摘要：{old_answer[:_MAX_HISTORY_CHARS]}"]
            lines.append("")
        lines += ["## 本次要回答的问题", "", f"用户提问：{question}"]
        if hint:
            lines.append(f"人工补充线索（涉及的项目/方法，优先按此定位）：{hint}")
        lines += [
            "",
            f"代码库根目录：{self._repo_path}（已是当前工作目录）",
            "",
            "本次允许查阅的项目：",
        ]
        lines += [self._project_line(p) for p in self.available_projects] or [
            "- （本机没有可查阅的项目）"
        ]
        return "\n".join(lines)


# ---------- 沉淀成知识草稿 ----------

class CodeQADrafter:
    """把可复用的答案写成知识草稿（待人工 `/kb approve` 后才会推到 Dify）。

    写本地 markdown 不需要确认（git 可回滚）；推线上知识库永远需要确认，这里不碰。
    """

    def __init__(self, *, gap_store: Any, docs_repo: DocsRepo):
        self._gap_store = gap_store
        self._docs_repo = docs_repo

    def save(
        self,
        question: str,
        answer: CodeAnswer,
        *,
        user_id: str,
        chat_id: str,
        message_id: str,
    ) -> int | None:
        """返回草稿对应的队列编号；没有可沉淀的内容、重复投递或写入失败返回 None。

        草稿是锦上添花——任何失败都只记日志，绝不能挡住已经查到的答案发给用户。
        """
        if not (answer.found and answer.blocks):
            return None

        envelope = DifyEnvelope(
            content="",
            gap_query=question,
            raw_json=json.dumps({"intent": "code_question", "question": question}, ensure_ascii=False),
        )
        try:
            gap_id = self._gap_store.enqueue(
                user_id=user_id, chat_id=chat_id, message_id=message_id, envelope=envelope
            )
            if gap_id is None:
                logger.info("该消息已记录过，跳过知识草稿: message_id=%s", message_id)
                return None

            if not self._gap_store.try_claim(
                gap_id, from_status="pending", to_status="filling", operator_id="code_qa"
            ):
                logger.info("知识草稿行已被他处领走: gap_id=%s", gap_id)
                return None

            doc_path = resolve_doc_path(
                self._docs_repo,
                task_name="",
                query=question,
                topic=answer.topic,
                title=answer.title,
                aliases=answer.aliases,
                keywords=answer.keywords,
                fallback_topic=f"gap_{gap_id}",
            )
            sha = self._docs_repo.append_blocks(doc_path, answer.blocks)
            self._gap_store.mark_drafted(
                gap_id,
                doc_path=doc_path,
                draft_summary=answer.summary or question,
                content_sha256=sha,
            )
        except (pymysql.MySQLError, OSError, ValueError):
            logger.exception("保存知识草稿失败: message_id=%s", message_id)
            return None

        logger.info("代码问答已沉淀为知识草稿: gap_id=%s doc=%s", gap_id, doc_path)
        return gap_id
