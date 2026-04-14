"""安全 Linter 工具 - Bandit 安全扫描"""
import json
import subprocess
from pathlib import Path

from claude_agent_sdk import tool, create_sdk_mcp_server


@tool(
    name="security_scan",
    description="使用 Bandit 扫描 Python 代码中的安全漏洞。",
    input_schema={
        "file_path": {
            "type": "string",
            "description": "要扫描的文件路径",
        },
        "severity_level": {
            "type": "string",
            "description": "严重程度阈值: all, low, medium, high",
        },
    },
)
async def security_scan(
    file_path: str,
    severity_level: str = "all",
) -> dict:
    """安全漏洞扫描"""
    path = Path(file_path)

    if not path.exists():
        return {
            "content": [
                {"type": "text", "text": f"文件不存在: {file_path}"}]
        }

    if path.suffix != ".py":
        return {
            "content": [
                {"type": "text", "text": "安全扫描仅支持 Python 代码"}]
        }

    # 运行 Bandit
    severity_map = {"low": "-l", "medium": "-ll", "high": "-lll"}
    severity_arg = severity_map.get(severity_level)

    cmd = ["bandit", "-f", "json"]
    if severity_arg:
        cmd.append(severity_arg)
    cmd.append(file_path)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )

    issues = []
    if result.returncode != 0 and result.stdout:
        try:
            data = json.loads(result.stdout)
            for issue in data.get("results", []):
                issues.append(
                    {
                        "test_id": issue.get("test_id"),
                        "severity": issue.get("issue_severity"),
                        "confidence": issue.get("issue_confidence"),
                        "line": issue.get("line_number"),
                        "col": issue.get("col_offset"),
                        "message": issue.get("issue_text"),
                        "cwe": issue.get("issue_cwe", {}).get("id"),
                    }
                )
        except json.JSONDecodeError:
            # 如果不是 JSON，返回原始输出
            return {
                "content": [
                    {"type": "text", "text": f"安全扫描输出:\n{result.stdout}"}]
            }

    # 按严重程度排序
    issues.sort(key=lambda x: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(x["severity"], 3))

    return {
        "content": [
            {
                "type": "text",
                "text": f"安全扫描结果:\n"
                f"- 发现 {len(issues)} 个潜在安全问题\n"
                + "\n".join(
                    f"- [{i['severity']}] 行 {i['line']}: {i['message']}"
                    for i in issues[:15]
                ),
            }
        ],
        "issues": issues,
    }


@tool(
    name="check_secrets",
    description="检查代码中可能暴露的敏感信息（API keys、密码等）。",
    input_schema={
        "file_path": {
            "type": "string",
            "description": "文件路径",
        },
    },
)
async def check_secrets(file_path: str) -> dict:
    """检查敏感信息"""
    path = Path(file_path)

    if not path.exists():
        return {
            "content": [
                {"type": "text", "text": f"文件不存在: {file_path}"}]
        }

    with open(path, encoding="utf-8", errors="ignore") as f:
        content = f.read()

    issues = []

    # 常见的敏感信息模式
    patterns = [
        ("API Key", r'api[_-]?key\s*[=:]\s*[\'"]?[a-zA-Z0-9_-]{20,}[\'"]?'),
        ("Secret Key", r'secret[_-]?key\s*[=:]\s*[\'"]?[a-zA-Z0-9_-]{20,}[\'"]?'),
        ("Password", r'password\s*[=:]\s*[\'"]?[^\s\'"]{8,}[\'"]?'),
        ("Token", r'token\s*[=:]\s*[\'"]?[a-zA-Z0-9_-]{20,}[\'"]?'),
        ("AWS Key", r'AKIA[0-9A-Z]{16}'),
        ("Private Key", r'-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----'),
    ]

    import re

    lines = content.split("\n")
    for i, line in enumerate(lines, 1):
        for name, pattern in patterns:
            if re.search(pattern, line, re.IGNORECASE):
                issues.append(
                    {
                        "type": name,
                        "line": i,
                        "content": line.strip()[:100],  # 截断显示
                        "message": f"发现可能的敏感信息: {name}",
                    }
                )

    return {
        "content": [
            {
                "type": "text",
                "text": f"敏感信息检查:\n"
                f"- 发现 {len(issues)} 处潜在敏感信息\n"
                + "\n".join(
                    f"- 行 {i['line']}: {i['message']}"
                    for i in issues
                ),
            }
        ],
        "issues": issues,
    }


@tool(
    name="lint_code",
    description="使用 Pylint 进行代码质量检查。",
    input_schema={
        "file_path": {
            "type": "string",
            "description": "文件路径",
        },
        "disable": {
            "type": "string",
            "description": "禁用的检查项，逗号分隔",
        },
    },
)
async def lint_code(file_path: str, disable: str = "") -> dict:
    """Pylint 代码检查"""
    path = Path(file_path)

    if path.suffix != ".py":
        return {
            "content": [
                {"type": "text", "text": "Pylint 仅支持 Python 代码"}]
        }

    args = ["pylint", "-f", "json", file_path]
    if disable:
        args.extend(["--disable", disable])

    result = subprocess.run(args, capture_output=True, text=True)

    issues = []
    if result.stdout:
        try:
            data = json.loads(result.stdout)
            for item in data:
                issues.append(
                    {
                        "type": item.get("type"),
                        "module": item.get("module"),
                        "line": item.get("line"),
                        "column": item.get("column"),
                        "message": item.get("message"),
                        "symbol": item.get("symbol"),
                    }
                )
        except json.JSONDecodeError:
            pass

    # 按类型分组统计
    type_counts: dict[str, int] = {}
    for issue in issues:
        t = issue.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    return {
        "content": [
            {
                "type": "text",
                "text": f"Pylint 检查结果:\n"
                + "\n".join(
                    f"- {t}: {c} 个" for t, c in type_counts.items()
                )
                + "\n\n详细问题:\n"
                + "\n".join(
                    f"- [{i['type']}] {i['module']}:{i['line']}: {i['message']}"
                    for i in issues[:20]
                ),
            }
        ],
        "issues": issues,
        "summary": type_counts,
    }


# 创建安全 Linter MCP Server
linter_server = create_sdk_mcp_server(
    name="security-linter",
    version="1.0.0",
    tools=[security_scan, check_secrets, lint_code],
)