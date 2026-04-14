"""Code Review Agent 核心实现"""
import os
from typing import Any

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from ..hooks import get_hooks_config
from ..tools import git_server, complexity_server, linter_server
from ..prompts import (
    load_agent_definition,
    load_business_agents,
    YUNXIAO_MR_AGENT,  # 云效 MR agent 保持固定配置
)

# 默认组织 ID
DEFAULT_ORG_ID = os.getenv("YUNXIAO_ORG_ID", "5ea86562f89c9700014a671f")

# 真实云效 MCP Server 配置（外部 stdio 进程）
YUNXIAO_MCP_CONFIG = {
    "command": "npx",
    "args": ["-y", "alibabacloud-devops-mcp-server"],
    "env": {
        "YUNXIAO_ACCESS_TOKEN": os.getenv("YUNXIAO_ACCESS_TOKEN", ""),
    },
}


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
        # mcp_servers 为 dict 格式：key=server名, value=server配置或对象
        self.mcp_servers = {
            "git-tools": git_server,
            "complexity-tools": complexity_server,
            "security-linter": linter_server,
            "yunxiao": YUNXIAO_MCP_CONFIG,  # 真实云效 MCP Server
        }
        self.hooks = custom_hooks or get_hooks_config()
        self._results: list[dict[str, Any]] = []

    def _get_options(
        self,
        dimensions: list[str] | None = None,
        permission_mode: str = "default",
    ) -> ClaudeAgentOptions:
        """获取 Agent 配置（不含云效工具）"""
        # 加载业务场景对应的 Agent 配置
        agents = load_business_agents(self.business_type)

        # 根据 dimensions 过滤
        if dimensions is not None and "all" not in dimensions:
            filtered_agents: dict[str, Any] = {}
            for dim in dimensions:
                agent_name = f"{dim}-reviewer"
                if agent_name in agents:
                    filtered_agents[agent_name] = agents[agent_name]
            agents = filtered_agents

        return ClaudeAgentOptions(
            allowed_tools=[
                "Read", "Grep", "Glob", "Agent",
                "get_git_diff", "get_file_content",
                "analyze_complexity", "analyze_maintainability", "check_code_duplication",
                "security_scan", "check_secrets", "lint_code",
            ],
            permission_mode=permission_mode,
            hooks=self.hooks,
            agents=agents,
            mcp_servers=self.mcp_servers,
        )

    def _get_options_with_yunxiao(
        self,
        dimensions: list[str] | None = None,
        permission_mode: str = "bypassPermissions",  # CLI/API 自动接受所有工具调用
    ) -> ClaudeAgentOptions:
        """获取包含云效工具的 Agent 配置。

        架构说明：
        - Claude Code 会话：subagent 通过父会话访问全局 yunxiao MCP（不传 mcp_servers）
        - CLI/API 独立进程：必须显式传 mcp_servers，否则子进程无法访问云效工具
        """
        return ClaudeAgentOptions(
            allowed_tools=["Agent"],
            permission_mode=permission_mode,
            hooks=self.hooks,
            agents={"yunxiao-mr-reviewer": YUNXIAO_MR_AGENT},
            # CLI/API 独立进程必须传 mcp_servers
            mcp_servers=self.mcp_servers,
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

        options = ClaudeAgentOptions(
            allowed_tools=["Agent"],
            permission_mode="default",
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
        comment_instruction = (
            "审查完成后，将所有问题合并为唯一一条中文评论发布到 MR（commentType=GLOBAL_COMMENT，只调用 1 次）。"
            if auto_comment
            else "生成审查报告，不需要在 MR 上添加评论（no_comment 模式）。"
        )

        prompt = f"""请调用 yunxiao-mr-reviewer subagent 对云效 MR 进行全面代码审查。

MR 信息:
- organizationId: {organization_id}
- repositoryId: {repository_id}
- localId: {local_id}

要求: {comment_instruction}

subagent 完成后，请用中文输出审查摘要。"""

        options = self._get_options_with_yunxiao(dimensions)
        result = await self._run_query(prompt, options)
        parsed = self._parse_review_result(result)

        parsed["metadata"] = {
            "repository_id": repository_id,
            "local_id": local_id,
            "organization_id": organization_id,
            "auto_comment": auto_comment,
        }

        return parsed

    async def _run_query(
        self,
        prompt: str,
        options: ClaudeAgentOptions,
    ) -> list[dict[str, Any]]:
        """执行 Agent 查询"""
        messages: list[dict[str, Any]] = []

        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                text_parts = []
                tool_uses = []

                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_uses.append({
                            "type": "tool_use",
                            "tool": block.name,
                            "input": block.input,
                        })

                if text_parts:
                    messages.append({
                        "type": "assistant",
                        "content": text_parts,
                    })
                messages.extend(tool_uses)

            elif isinstance(message, ResultMessage):
                messages.append({
                    "type": "result",
                    "subtype": message.subtype,
                    "content": message.result if hasattr(message, "result") else None,
                })

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
            "result_type": result_msg.get("subtype") if result_msg else None,
            "tools_used": tools_used,
        }


# 默认 agent 实例
default_review_agent = CodeReviewAgent()
