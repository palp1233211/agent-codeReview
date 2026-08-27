"""Code Review Agent 核心实现"""
import os
from typing import Any
from urllib.parse import quote, unquote, urlparse

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


def _get_yunxiao_mcp_config() -> dict[str, Any]:
    """获取 OpenAI Responses API 的远程云效 MCP 配置。"""
    server_url = os.getenv("YUNXIAO_MCP_URL")
    if not server_url:
        return {}
    headers = {}
    token = os.getenv("YUNXIAO_ACCESS_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers["X-Devops-Toolsets"] = "code-management"
    config: dict[str, Any] = {
        "type": "mcp",
        "server_label": "yunxiao",
        "server_url": server_url,
        "require_approval": "never",
    }
    if headers:
        config["headers"] = headers
    return config


def _get_yunxiao_claude_mcp_config() -> dict[str, Any]:
    """获取 Claude Agent SDK 的 stdio 云效 MCP 配置。"""
    return {
        "command": "npx",
        "args": ["-y", "alibabacloud-devops-mcp-server"],
        "env": {
            "YUNXIAO_ACCESS_TOKEN": os.getenv("YUNXIAO_ACCESS_TOKEN", ""),
            "DEVOPS_TOOLSETS": "code-management",
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
        local_id: str,
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
        normalized_repository_id = _normalize_yunxiao_repository_id(repository_id)

        comment_instruction = (
            "审查完成后，将所有问题合并为唯一一条中文评论发布到 MR（commentType=GLOBAL_COMMENT，只调用 1 次）。"
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
- localId: {local_id}

{dimension_note}

要求: {comment_instruction}

完成后请用中文输出审查摘要。"""

        mcp_config = _get_yunxiao_mcp_config()
        options = RuntimeOptions(
            allowed_tools=list(YUNXIAO_MR_AGENT.tools),
            allowed_agents=self._dimension_agent_names(dimensions)
            or ["security-reviewer", "quality-reviewer", "performance-reviewer"],
            hooks=self.hooks,
            remote_mcp_servers=[mcp_config] if mcp_config else [],
            claude_mcp_servers={"yunxiao": _get_yunxiao_claude_mcp_config()},
            verbose=True,
            progress_prefix="yunxiao-mr",
            disallowed_tools=[
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
            "local_id": local_id,
            "organization_id": organization_id,
            "auto_comment": auto_comment,
            "dimensions": dimensions or ["all"],
            "provider": (os.getenv("AGENT_PROVIDER") or os.getenv("AGENT_SDK") or "claude").lower(),
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

        result_type = None
        is_error = False
        if result_msg:
            is_error = bool(result_msg.get("is_error"))
            if is_error:
                result_type = "error"
            else:
                result_type = result_msg.get("subtype")

        # 提取 assistant 最终输出（最后一条 assistant 消息）
        final_output = ""
        for msg in reversed(messages):
            if msg.get("type") == "assistant":
                final_output = "\n".join(msg.get("content", []))
                break

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
