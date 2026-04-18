"""Hooks 系统实现 - PreToolUse 验证、PostToolUse 审计和权限自动授权"""
import json
import time
from datetime import datetime
from typing import Any

from claude_agent_sdk import HookContext, HookMatcher, PermissionResultAllow

# 审计日志存储
_audit_log: list[dict[str, Any]] = []


async def pre_tool_validator(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: HookContext,
) -> dict[str, Any]:
    """PreToolUse Hook - 验证工具调用参数

    检查:
    1. 文件路径是否在安全范围内
    2. 命令是否包含危险操作
    3. 输入参数是否有效
    """
    tool_name = input_data.get("tool_name", "unknown")
    tool_input = input_data.get("tool_input", {})

    # 验证文件路径安全性
    if tool_name in ["Read", "Edit", "Write", "get_file_content"]:
        file_path = tool_input.get("file_path", "")
        if file_path:
            # 检查是否尝试访问敏感文件
            dangerous_paths = [
                ".env",
                ".git/config",
                "id_rsa",
                "credentials",
                "secrets",
            ]
            for dangerous in dangerous_paths:
                if dangerous in file_path.lower():
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": f"禁止访问敏感文件: {file_path}",
                        }
                    }

            # 检查文件大小限制
            import os
            if os.path.exists(file_path):
                size_kb = os.path.getsize(file_path) / 1024
                if size_kb > 500:  # 500KB 限制
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": f"文件过大 ({size_kb:.1f}KB)，超过 500KB 限制",
                        }
                    }

    # 验证 Bash 命令安全性
    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        dangerous_commands = [
            "rm -rf",
            "sudo",
            "chmod 777",
            "mkfs",
            "dd if=",
            ":(){ :|:& };:",  # Fork bomb
            "wget",
            "curl -X POST",  # 可能的 SSRF
        ]
        for dangerous in dangerous_commands:
            if dangerous in command:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": f"禁止执行危险命令: {command}",
                    }
                }

    # 验证 Git 操作参数
    if tool_name in ["get_git_diff", "analyze_commit_history"]:
        base_branch = tool_input.get("base_branch", "")
        if base_branch and not base_branch.replace("-", "").replace("/", "").isalnum():
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"无效的分支名称: {base_branch}",
                }
            }

    # 记录审计日志
    _audit_log.append(
        {
            "timestamp": datetime.now().isoformat(),
            "event": "PreToolUse",
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "input_summary": str(tool_input)[:200],
            "decision": "allow",
        }
    )

    return {}  # 允许执行


async def post_tool_audit(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: HookContext,
) -> dict[str, Any]:
    """PostToolUse Hook - 审计工具执行结果

    记录:
    1. 执行时间
    2. 结果摘要
    3. 错误信息
    """
    tool_name = input_data.get("tool_name", "unknown")
    result = input_data.get("tool_result", {})
    error = input_data.get("error", None)

    # 记录审计日志
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "event": "PostToolUse",
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "success": error is None,
        "error": str(error) if error else None,
    }

    # 如果是安全扫描工具，特别记录发现的问题数量
    if tool_name == "security_scan":
        issues = result.get("issues", [])
        log_entry["security_issues_found"] = len(issues)
        if issues:
            # 记录高危问题
            high_severity = [i for i in issues if i.get("severity") == "HIGH"]
            if high_severity:
                log_entry["high_severity_issues"] = len(high_severity)

    # 如果是复杂度分析，记录超标函数
    if tool_name == "analyze_complexity":
        issues = result.get("issues", [])
        log_entry["high_complexity_functions"] = len(issues)

    _audit_log.append(log_entry)

    # 如果发现安全问题，添加额外上下文
    if tool_name == "security_scan" and result.get("issues"):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "安全扫描发现问题，建议优先处理高危漏洞。",
            }
        }

    return {}


async def user_prompt_enricher(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: HookContext,
) -> dict[str, Any]:
    """UserPromptSubmit Hook - 添加上下文信息"""
    prompt = input_data.get("prompt", "")

    # 添加时间戳和审查维度提示
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    enriched_prompt = f"""[审查请求提交于 {timestamp}]

请按照以下维度进行代码审查:
1. **安全性**: SQL注入、XSS、敏感信息暴露、权限控制
2. **代码质量**: 命名规范、代码结构、复杂度、重复代码
3. **性能**: N+1查询、内存泄漏、不必要的计算

对每个问题提供:
- 严重程度 (Critical/High/Medium/Low)
- 具体位置 (文件路径和行号)
- 问题描述和修复建议

原始请求: {prompt}"""

    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": enriched_prompt,
        }
    }


async def yunxiao_permission_handler(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: HookContext,
) -> dict[str, Any]:
    """PermissionRequest Hook - 自动授权云效 MCP 工具调用"""
    tool_name = input_data.get("tool_name", "")

    if tool_name.startswith("mcp__yunxiao__"):
        _audit_log.append(
            {
                "timestamp": datetime.now().isoformat(),
                "event": "PermissionRequest",
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "decision": "auto_allow",
            }
        )
        allow = PermissionResultAllow()
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": allow.behavior},
            }
        }

    return {}


def get_audit_log() -> list[dict[str, Any]]:
    """获取审计日志"""
    return _audit_log.copy()


def clear_audit_log() -> None:
    """清除审计日志"""
    _audit_log.clear()


def get_hooks_config() -> dict[str, list[HookMatcher]]:
    """获取 Hooks 配置"""
    return {
        "PreToolUse": [
            HookMatcher(hooks=[pre_tool_validator]),
            HookMatcher(matcher="Bash", hooks=[pre_tool_validator]),
            HookMatcher(matcher="get_file_content", hooks=[pre_tool_validator]),
        ],
        "PostToolUse": [
            HookMatcher(hooks=[post_tool_audit]),
            HookMatcher(matcher="security_scan", hooks=[post_tool_audit]),
        ],
        "UserPromptSubmit": [
            HookMatcher(hooks=[user_prompt_enricher]),
        ],
        "PermissionRequest": [
            HookMatcher(matcher="mcp__yunxiao__*", hooks=[yunxiao_permission_handler]),
        ],
    }