"""Code Review Agent 核心实现"""
import os
from typing import Any

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AgentDefinition,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from ..hooks import get_hooks_config
from ..tools import git_server, complexity_server, linter_server

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

# Security Review Subagent
SECURITY_AGENT = AgentDefinition(
    description="安全审查专家。检查代码中的安全漏洞、敏感信息暴露、权限控制问题。",
    prompt="""你是安全审查专家，专注于发现代码中的安全风险。

审查重点:
1. **注入漏洞**: SQL注入、XSS、命令注入
2. **认证授权**: 弱密码、缺少权限检查、会话管理
3. **敏感信息**: API密钥、密码、个人数据暴露
4. **加密安全**: 弱加密算法、不安全的随机数
5. **网络安全**: SSRF、CSRF、开放重定向

每个问题输出:
- severity: Critical/High/Medium/Low
- location: 文件路径和行号
- description: 问题描述
- fix_suggestion: 修复建议

使用 security_scan 和 check_secrets 工具进行扫描。""",
    tools=["security_scan", "check_secrets", "Read", "Grep", "Glob"],
)

# Quality Review Subagent
QUALITY_AGENT = AgentDefinition(
    description="代码质量审查专家。检查代码结构、命名规范、复杂度、重复代码。",
    prompt="""你是代码质量审查专家，专注于提升代码的可维护性。

审查重点:
1. **命名规范**: 变量、函数、类名是否清晰有意义
2. **代码结构**: 函数是否过长、职责是否单一
3. **复杂度**: 圈复杂度是否过高、嵌套是否过深
4. **重复代码**: 是否有可以抽取的公共逻辑
5. **错误处理**: 异常是否正确处理、边界条件是否考虑

每个问题输出:
- severity: Critical/High/Medium/Low/Info
- location: 文件路径和行号
- description: 问题描述
- fix_suggestion: 改进建议

使用 analyze_complexity、analyze_maintainability、check_code_duplication 工具进行分析。""",
    tools=["analyze_complexity", "analyze_maintainability", "check_code_duplication", "Read", "Grep", "Glob"],
)

# Performance Review Subagent
PERFORMANCE_AGENT = AgentDefinition(
    description="性能审查专家。检查代码中的性能瓶颈、资源使用问题。",
    prompt="""你是性能审查专家，专注于发现代码的性能问题。

审查重点:
1. **数据库**: N+1查询、缺少索引、连接未关闭
2. **内存**: 内存泄漏、大对象未释放
3. **计算**: 重复计算、不必要的循环、算法效率
4. **IO**: 阻塞操作、大量小文件读写
5. **并发**: 死锁风险、竞态条件

每个问题输出:
- severity: Critical/High/Medium/Low
- location: 文件路径和行号
- description: 问题描述
- impact: 预估性能影响
- fix_suggestion: 优化建议

使用 analyze_complexity 工具结合代码阅读进行分析。""",
    tools=["analyze_complexity", "Read", "Grep", "Glob"],
)

# 云效 MR Review Subagent
# 工具名使用 mcp__yunxiao__* 格式（Claude Code 全局已配置 yunxiao MCP server）
YUNXIAO_MR_AGENT = AgentDefinition(
    description="云效 MR 审查专家。审查云效平台上的 Merge Request，分析代码变更并添加评论。",
    prompt="""你是云效 MR 审查专家。所有输出内容必须使用中文，包括评论内容。

审查流程:
1. 使用 mcp__yunxiao__get_change_request 获取 MR 详情
   参数: organizationId, repositoryId, localId
2. 使用 mcp__yunxiao__list_change_request_patch_sets 获取最新的 patch set
   参数: organizationId, repositoryId, localId
3. 使用 mcp__yunxiao__compare 获取代码差异
   参数: organizationId, repositoryId, from(目标分支), to(源分支), sourceType="branch", targetType="branch"
4. 使用 mcp__yunxiao__get_file_blobs 读取关键变更文件完整内容（按需）
   参数: organizationId, repositoryId, filePath, ref
5. 在脑中整理所有发现的问题，按文件和严重程度分组
6. 调用 mcp__yunxiao__create_change_request_comment 发布评论

【评论策略 - 严格遵守】
- 总评论次数：只能调用 mcp__yunxiao__create_change_request_comment **1次**
- 必须使用 commentType="GLOBAL_COMMENT"（全局评论）
- 禁止多次调用，将所有内容合并到这唯一一条评论中
- 评论内容语言：必须全程使用中文

唯一一条全局评论的格式（Markdown）:

## 🤖 AI 代码审查报告

### 🔴 高危问题
对每个高危问题，格式：
**[问题类型]** `文件名:行号`
> 问题描述（中文）
> 修复建议（中文）

### 🟡 中等问题
对每个中等问题，格式同上

### 🟢 建议改进
对每个低优先级建议，格式同上

### 📊 整体评价
一段话总结变更质量、主要风险、是否建议合并""",
    tools=[
        "mcp__yunxiao__get_change_request",
        "mcp__yunxiao__list_change_request_patch_sets",
        "mcp__yunxiao__compare",
        "mcp__yunxiao__list_change_request_comments",
        "mcp__yunxiao__get_file_blobs",
        "mcp__yunxiao__list_files",
        "mcp__yunxiao__create_change_request_comment",
        "Agent",
    ],
)


class CodeReviewAgent:
    """Code Review Agent 主类"""

    def __init__(
        self,
        custom_hooks: dict[str, Any] | None = None,
    ):
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
        agents: dict[str, AgentDefinition] = {}

        if dimensions is None or "all" in dimensions:
            agents = {
                "security-reviewer": SECURITY_AGENT,
                "quality-reviewer": QUALITY_AGENT,
                "performance-reviewer": PERFORMANCE_AGENT,
            }
        else:
            if "security" in dimensions:
                agents["security-reviewer"] = SECURITY_AGENT
            if "quality" in dimensions:
                agents["quality-reviewer"] = QUALITY_AGENT
            if "performance" in dimensions:
                agents["performance-reviewer"] = PERFORMANCE_AGENT

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
        permission_mode: str = "default",
    ) -> ClaudeAgentOptions:
        """获取包含云效工具的 Agent 配置。

        架构说明：
        - query() 子进程本身没有 yunxiao MCP 工具（新进程，无全局 Claude Code 设置）
        - 但 query() 子进程通过 Agent 工具启动的 subagent，运行在父 Claude Code 会话中，
          因此可以访问父会话全局注册的 yunxiao MCP server（~/.claude/settings.json）
        - 所以正确方案是：outer agent 调用 Agent 工具 → yunxiao-mr-reviewer subagent → 调用 mcp__yunxiao__* 工具
        - 注意：不能显式传 mcp_servers（会启动新进程导致认证失败）
        """
        return ClaudeAgentOptions(
            allowed_tools=["Agent"],  # outer agent 只需能调用 Agent 工具（启动 subagent）
            permission_mode=permission_mode,
            hooks=self.hooks,
            agents={"yunxiao-mr-reviewer": YUNXIAO_MR_AGENT},
            # 不传 mcp_servers！让 subagent 通过父 Claude Code 会话访问全局 yunxiao MCP server
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
                "security-reviewer": SECURITY_AGENT,
                "quality-reviewer": QUALITY_AGENT,
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
