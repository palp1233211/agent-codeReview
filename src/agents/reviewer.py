"""Code Review Agent 核心实现"""
from dataclasses import dataclass
import os
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from .runtime import RuntimeOptions, create_agent_runtime
from .. import tools as _tools  # noqa: F401 - importing registers local tools
from ..hooks import get_hooks_config
from ..prompts import (
    load_agent_definition,
    load_business_agents,
    YUNXIAO_MR_AGENT,  # 云效 MR agent 保持固定配置
)

# 默认组织 ID
DEFAULT_ORG_ID = os.getenv("YUNXIAO_ORG_ID", "5ea86562f89c9700014a671f")
OPENAI_PROVIDERS = {"openai", "openai_sdk"}
YUNXIAO_MCP_SERVER_LABEL = "yunxiao"
YUNXIAO_MCP_DEFAULT_TOOLSETS = "code-management"
YUNXIAO_MR_PATH_MARKERS = {
    "change",
    "changes",
    "merge_request",
    "merge_requests",
    "merge-request",
    "merge-requests",
    "mr",
    "pull",
    "pulls",
}


@dataclass(frozen=True)
class YunxiaoMcpSettings:
    transport: str
    server_url: str | None
    token: str
    toolsets: str


def _get_yunxiao_mcp_settings() -> YunxiaoMcpSettings:
    """Read shared Yunxiao MCP settings for Claude and OpenAI runtimes."""
    transport = os.getenv("YUNXIAO_MCP_TRANSPORT", "http").strip().lower().replace("-", "_")
    toolsets = os.getenv("YUNXIAO_TOOLSETS", YUNXIAO_MCP_DEFAULT_TOOLSETS).strip()
    return YunxiaoMcpSettings(
        transport=transport,
        server_url=os.getenv("YUNXIAO_MCP_URL"),
        token=os.getenv("YUNXIAO_ACCESS_TOKEN") or os.getenv("YUNXIAO_TOKEN") or "",
        toolsets=toolsets or YUNXIAO_MCP_DEFAULT_TOOLSETS,
    )


def validate_openai_runtime_config(
    provider: str,
    *,
    require_yunxiao_mcp: bool = False,
) -> None:
    """Fail fast on missing OpenAI-compatible runtime configuration."""
    selected = provider.lower()
    if selected not in OPENAI_PROVIDERS:
        return

    missing = []
    if not os.getenv("OPENAI_API_KEY"):
        missing.append("OPENAI_API_KEY")
    if not os.getenv("OPENAI_MODEL"):
        missing.append("OPENAI_MODEL")
    if selected == "openai_sdk" and not os.getenv("OPENAI_BASE_URL"):
        missing.append("OPENAI_BASE_URL")
    if require_yunxiao_mcp:
        if not os.getenv("YUNXIAO_MCP_URL"):
            missing.append("YUNXIAO_MCP_URL")
        if not (os.getenv("YUNXIAO_ACCESS_TOKEN") or os.getenv("YUNXIAO_TOKEN")):
            missing.append("YUNXIAO_ACCESS_TOKEN")

    if missing:
        raise ValueError("OpenAI runtime 配置缺失: " + ", ".join(missing))


def _get_yunxiao_mcp_config() -> dict[str, Any]:
    """获取 OpenAI runtime 通过 HTTP 连接的云效 MCP 配置。"""
    settings = _get_yunxiao_mcp_settings()
    if settings.transport == "stdio":
        raise ValueError(
            "OpenAI runtime 不支持 stdio MCP；请配置 YUNXIAO_MCP_TRANSPORT=http/sse "
            "和 YUNXIAO_MCP_URL，或改用 AGENT_PROVIDER=claude。"
        )
    if settings.transport not in {"http", "streamable_http", "sse"}:
        raise ValueError(f"不支持的 YUNXIAO_MCP_TRANSPORT: {settings.transport}")
    headers = {}
    if not settings.server_url:
        raise ValueError(
            "OpenAI 云效 MR 审查缺少 YUNXIAO_MCP_URL；"
            "请配置 Streamable HTTP/SSE MCP 地址。"
        )
    if not settings.token:
        raise ValueError(
            "OpenAI 云效 MR 审查缺少 YUNXIAO_ACCESS_TOKEN；"
            "请配置云效访问令牌。"
        )
    headers["Authorization"] = f"Bearer {settings.token}"
    headers["X-Yunxiao-Token"] = settings.token
    headers["X-Devops-Toolsets"] = settings.toolsets
    config: dict[str, Any] = {
        "type": "mcp",
        "server_label": YUNXIAO_MCP_SERVER_LABEL,
        "server_url": settings.server_url,
        "transport": "sse" if settings.transport == "sse" else "http",
        "require_approval": "never",
    }
    timeout_value = os.getenv("YUNXIAO_MCP_TIMEOUT", "60").strip()
    try:
        config["timeout"] = max(1.0, float(timeout_value))
    except ValueError:
        raise ValueError("YUNXIAO_MCP_TIMEOUT 必须是正数") from None
    config["headers"] = headers
    return config


def _get_yunxiao_claude_mcp_config() -> dict[str, Any]:
    """获取 Claude Agent SDK 的 stdio 云效 MCP 配置。"""
    settings = _get_yunxiao_mcp_settings()
    return {
        "command": "npx",
        "args": ["-y", "alibabacloud-devops-mcp-server"],
        "env": {
            "YUNXIAO_ACCESS_TOKEN": settings.token,
            "DEVOPS_TOOLSETS": settings.toolsets,
        },
    }


def _normalize_yunxiao_repository_id(repository_id: str) -> str:
    """Normalize Yunxiao repository id/path for the DevOps MCP server."""
    value = repository_id.strip()
    if not value:
        return value

    if value.startswith(("http://", "https://")):
        path_parts = [part for part in urlparse(value).path.split("/") if part]
        if "change" in path_parts:
            path_parts = path_parts[: path_parts.index("change")]
        value = "/".join(path_parts)

    if value.isdigit():
        return value
    if "%2f" in value.lower():
        return value
    if "/" in value:
        return quote(unquote(value), safe="")
    return value


def parse_yunxiao_mr_reference(
    repository_or_url: str,
    local_id: str | None = None,
) -> tuple[str, str]:
    """Resolve either a repository/MR pair or a Yunxiao MR URL.

    The URL form is intentionally parsed only when a known MR path marker is
    present. This avoids treating an arbitrary repository URL as an MR URL.
    ``local_id`` remains accepted for backwards compatibility and must agree
    with the ID in the URL when both are supplied.
    """
    value = repository_or_url.strip()
    if not value:
        raise ValueError("云效 MR 地址或仓库 ID 不能为空")

    if not value.startswith(("http://", "https://")):
        if local_id is None or not str(local_id).strip():
            raise ValueError("使用仓库 ID 或仓库路径时必须同时提供 MR 编号")
        return value, str(local_id).strip()

    parsed = urlparse(value)
    path_parts = [unquote(part) for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)

    url_local_id = None
    repository_parts: list[str] | None = None
    for index, part in enumerate(path_parts[:-1]):
        if part.lower() not in YUNXIAO_MR_PATH_MARKERS:
            continue
        candidate_local_id = path_parts[index + 1].strip()
        if not candidate_local_id:
            continue
        url_local_id = candidate_local_id
        repository_parts = path_parts[:index]
        break

    if url_local_id is None:
        for key in ("localId", "local_id", "mrId", "mr_id", "changeRequestId"):
            candidates = query.get(key)
            if candidates and candidates[0].strip():
                url_local_id = candidates[0].strip()
                repository_parts = path_parts
                break

    if not repository_parts or not url_local_id:
        raise ValueError(
            "无法从云效 MR 地址解析仓库和 MR 编号；"
            "请确认地址包含 /change/<编号>、/merge_requests/<编号> 等路径"
        )

    if local_id is not None and str(local_id).strip() != url_local_id:
        raise ValueError("MR 地址中的编号与 --mr-id 不一致")

    repository = "/".join(repository_parts)
    return _normalize_yunxiao_repository_id(repository), url_local_id


class CodeReviewAgent:
    """Code Review Agent 主类"""

    def __init__(
        self,
        business_type: str = "default",
        custom_hooks: dict[str, Any] | None = None,
    ):
        """初始化 Code Review Agent

        Args:
            business_type: 业务类型（default, frontend, backend）
            custom_hooks: 自定义 hooks 配置
        """
        self.business_type = business_type
        self.hooks = custom_hooks or get_hooks_config()
        self.runtime = create_agent_runtime()
        self._results: list[dict[str, Any]] = []

    def _get_options(
        self,
        dimensions: list[str] | None = None,
    ) -> RuntimeOptions:
        """获取 Agent 配置（不含云效工具）"""
        # 加载业务场景对应的 Agent 配置
        agents = load_business_agents(self.business_type)
        allowed_agents = self._dimension_agent_names(dimensions)

        # 根据 dimensions 过滤
        if allowed_agents:
            filtered_agents: dict[str, Any] = {}
            for agent_name in allowed_agents:
                if agent_name in agents:
                    filtered_agents[agent_name] = agents[agent_name]
            agents = filtered_agents

        return RuntimeOptions(
            allowed_tools=[
                "Read", "Grep", "Glob", "Agent",
                "get_git_diff", "get_file_content",
                "analyze_complexity", "analyze_maintainability", "check_code_duplication",
                "security_scan", "check_secrets", "lint_code",
            ],
            allowed_agents=allowed_agents,
            hooks=self.hooks,
            agents=agents,
        )

    async def review_git_diff(
        self,
        base_branch: str = "main",
        target_branch: str = "HEAD",
        dimensions: list[str] | None = None,
    ) -> dict[str, Any]:
        """审查 Git diff"""
        prompt = f"""请审查 {base_branch} 和 {target_branch} 分支之间的代码变更。

步骤:
1. 使用 get_git_diff 获取变更文件列表和详细 diff
2. 按维度调用对应的 subagent 进行审查
3. 汇总所有发现的问题，提供整体评分和改进建议"""

        options = self._get_options(dimensions)
        result = await self._run_query(prompt, options)
        return self._parse_review_result(result)

    async def review_files(
        self,
        file_paths: list[str],
        dimensions: list[str] | None = None,
    ) -> dict[str, Any]:
        """审查指定文件"""
        files_list = "\n".join(f"- {p}" for p in file_paths)
        prompt = f"""请审查以下文件的代码质量:
{files_list}

步骤:
1. 使用 Read 工具读取每个文件
2. 使用分析工具检查复杂度、安全、重复代码
3. 按维度调用对应的 subagent 进行深度审查
4. 汇总所有发现的问题"""

        options = self._get_options(dimensions)
        result = await self._run_query(prompt, options)
        return self._parse_review_result(result)

    async def review_code_snippet(
        self,
        code: str,
        language: str | None = None,
        filename: str | None = None,
        dimensions: list[str] | None = None,
    ) -> dict[str, Any]:
        """审查代码片段"""
        lang_hint = f"语言: {language}" if language else ""
        file_hint = f"文件名: {filename}" if filename else ""

        prompt = f"""请审查以下代码片段:
{lang_hint}
{file_hint}

```{language or ''}
{code}
```

分析:
1. 安全漏洞
2. 代码质量问题
3. 性能隐患
4. 最佳实践建议"""

        options = RuntimeOptions(
            allowed_tools=["Agent"],
            allowed_agents=["security-reviewer", "quality-reviewer"],
            hooks=self.hooks,
            agents={
                "security-reviewer": load_agent_definition("security"),
                "quality-reviewer": load_agent_definition("quality"),
            },
        )

        result = await self._run_query(prompt, options)
        return self._parse_review_result(result)

    async def review_yunxiao_mr(
        self,
        repository_id: str,
        local_id: str | None = None,
        organization_id: str = DEFAULT_ORG_ID,
        dimensions: list[str] | None = None,
        auto_comment: bool = True,
    ) -> dict[str, Any]:
        """审查云效 MR

        Args:
            repository_id: 代码库ID（数字ID或路径）
            local_id: MR在代码库中的编号
            organization_id: 组织ID
            dimensions: 审查维度，None 表示全部
            auto_comment: 是否自动在 MR 上添加评论
        """
        normalized_repository_id, resolved_local_id = parse_yunxiao_mr_reference(
            repository_id,
            local_id,
        )

        comment_instruction = (
            "审查完成后，将所有问题合并为唯一一条中文评论发布到 MR（commentType=GLOBAL_COMMENT）。"
            "只有 Yunxiao 实际收到的评论请求才算一次发布；若工具返回 local_validation_rejected，"
            "表示 ReviewGuard 在本地拦截、Yunxiao 尚未被调用，必须修正文件、行号或变更证据后重新调用。"
            "校验通过后，只允许向 Yunxiao 实际发布 1 次。"
            if auto_comment
            else "生成审查报告，不需要在 MR 上添加评论（no_comment 模式）。"
        )
        dimension_note = (
            "当前只审查这些维度: " + ", ".join(dimensions)
            if dimensions and "all" not in dimensions
            else "当前审查全部维度。"
        )

        # 单层直调：把 subagent 的系统提示词拼进主 prompt，省掉 Agent 调度一层
        prompt = f"""{YUNXIAO_MR_AGENT.prompt}

---

本次任务 MR 信息:
- organizationId: {organization_id}
- repositoryId: {normalized_repository_id}
- localId: {resolved_local_id}

{dimension_note}

要求: {comment_instruction}

完成后请用中文输出审查摘要。"""

        provider = (os.getenv("AGENT_PROVIDER") or os.getenv("AGENT_SDK") or "claude").lower()
        validate_openai_runtime_config(provider, require_yunxiao_mcp=True)
        mcp_config = _get_yunxiao_mcp_config() if provider in OPENAI_PROVIDERS else {}
        options = RuntimeOptions(
            allowed_tools=list(YUNXIAO_MR_AGENT.tools),
            enforce_mr_review=True,
            allowed_agents=self._dimension_agent_names(dimensions)
            or ["security-reviewer", "quality-reviewer", "performance-reviewer"],
            hooks=self.hooks,
            remote_mcp_servers=[mcp_config] if mcp_config else [],
            claude_mcp_servers={"yunxiao": _get_yunxiao_claude_mcp_config()},
            verbose=True,
            progress_prefix="yunxiao-mr",
            disallowed_tools=[
                *([] if auto_comment else ['mcp__yunxiao__create_change_request_comment']),
                "Bash",
                "Read",
                "Write",
                "Edit",
                "MultiEdit",
                "Grep",
                "Glob",
                "LS",
            ],
        )
        result = await self._run_query(prompt, options)
        parsed = self._parse_review_result(result)

        parsed["metadata"] = {
            "repository_id": normalized_repository_id,
            "original_repository_id": repository_id,
            "local_id": resolved_local_id,
            "organization_id": organization_id,
            "auto_comment": auto_comment,
            "dimensions": dimensions or ["all"],
            "provider": provider,
        }

        return parsed

    @staticmethod
    def _dimension_agent_names(dimensions: list[str] | None) -> list[str]:
        if not dimensions or "all" in dimensions:
            return []
        return [f"{dim}-reviewer" for dim in dimensions]

    async def _run_query(
        self,
        prompt: str,
        options: RuntimeOptions,
    ) -> list[dict[str, Any]]:
        """执行 Agent 查询"""
        messages = await self.runtime.run(prompt, options)
        self._results = messages
        return messages

    def _parse_review_result(
        self,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """解析审查结果"""
        # 提取最终 result 消息
        result_msg = next(
            (msg for msg in reversed(messages) if msg.get("type") == "result"),
            None,
        )

        result_type = "incomplete"
        is_error = True
        if result_msg:
            is_error = bool(result_msg.get("is_error"))
            result_type = result_msg.get("subtype") or ("error" if is_error else "success")

        # 提取 assistant 最终输出（最后一条 assistant 消息）
        final_output = ""
        for msg in reversed(messages):
            if msg.get("type") == "assistant":
                final_output = "\n".join(msg.get("content", []))
                break
        if not final_output.strip() and (result_msg is None or not is_error):
            is_error = True
            result_type = "incomplete"

        # 提取使用过的工具列表
        tools_used = [
            msg.get("tool")
            for msg in messages
            if msg.get("type") == "tool_use"
        ]

        return {
            "raw_messages": messages,
            "summary": final_output,
            "result_type": result_type,
            "is_error": is_error,
            "tools_used": tools_used,
        }


# 默认 agent 实例
default_review_agent = CodeReviewAgent()
