"""Git 操作工具 - MCP Server 实现"""
import subprocess
from pathlib import Path

from claude_agent_sdk import tool, create_sdk_mcp_server


@tool(
    name="get_git_diff",
    description="获取两个分支之间的 diff。返回变更文件列表和每个文件的详细 diff。",
    input_schema={
        "base_branch": {
            "type": "string",
            "description": "基准分支名称，默认为 main",
        },
        "target_branch": {
            "type": "string",
            "description": "目标分支名称，或 HEAD 表示当前更改",
        },
        "repo_path": {
            "type": "string",
            "description": "仓库路径，默认当前目录",
        },
    },
)
async def get_git_diff(
    base_branch: str = "main",
    target_branch: str = "HEAD",
    repo_path: str | None = None,
) -> dict:
    """获取 Git diff"""
    cwd = Path(repo_path) if repo_path else Path.cwd()

    # 获取变更文件列表
    result = subprocess.run(
        ["git", "diff", "--name-only", base_branch, target_branch],
        cwd=cwd,
        capture_output=True,
        text=True,
    )

    changed_files = result.stdout.strip().split("\n") if result.stdout.strip() else []

    if not changed_files:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "没有发现文件变更。",
                }
            ]
        }

    # 获取详细 diff
    diff_result = subprocess.run(
        ["git", "diff", base_branch, target_branch],
        cwd=cwd,
        capture_output=True,
        text=True,
    )

    return {
        "content": [
            {
                "type": "text",
                "text": f"变更文件 ({len(changed_files)}):\n"
                + "\n".join(f"- {f}" for f in changed_files)
                + "\n\n详细 Diff:\n"
                + diff_result.stdout[:10000],  # 限制长度
            }
        ],
        "changed_files": changed_files,
    }


@tool(
    name="get_file_content",
    description="读取指定文件的内容，支持指定行范围。",
    input_schema={
        "file_path": {
            "type": "string",
            "description": "文件路径",
        },
        "start_line": {
            "type": "integer",
            "description": "起始行号，可选",
        },
        "end_line": {
            "type": "integer",
            "description": "结束行号，可选",
        },
    },
)
async def get_file_content(
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict:
    """读取文件内容"""
    path = Path(file_path)

    if not path.exists():
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"文件不存在: {file_path}",
                }
            ]
        }

    with open(path, encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    if start_line and end_line:
        lines = lines[start_line - 1 : end_line]

    content = "".join(lines)

    return {
        "content": [
            {
                "type": "text",
                "text": content,
            }
        ],
        "total_lines": len(lines),
    }


@tool(
    name="analyze_commit_history",
    description="分析最近的 commit 历史，查找潜在问题模式。",
    input_schema={
        "limit": {
            "type": "integer",
            "description": "分析的 commit 数量，默认 10",
        },
        "repo_path": {
            "type": "string",
            "description": "仓库路径，可选",
        },
    },
)
async def analyze_commit_history(limit: int = 10, repo_path: str | None = None) -> dict:
    """分析 commit 历史"""
    cwd = Path(repo_path) if repo_path else Path.cwd()

    result = subprocess.run(
        [
            "git",
            "log",
            f"-{limit}",
            "--oneline",
            "--format=%h|%s|%an|%ar",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
    )

    commits = []
    for line in result.stdout.strip().split("\n"):
        if line:
            parts = line.split("|")
            commits.append(
                {
                    "hash": parts[0],
                    "message": parts[1],
                    "author": parts[2],
                    "date": parts[3],
                }
            )

    return {
        "content": [
            {
                "type": "text",
                "text": f"最近 {len(commits)} 个 commit:\n"
                + "\n".join(
                    f"- {c['hash']} {c['message']} ({c['author']}, {c['date']})"
                    for c in commits
                ),
            }
        ],
        "commits": commits,
    }


# 创建 Git MCP Server
git_server = create_sdk_mcp_server(
    name="git-tools",
    version="1.0.0",
    tools=[get_git_diff, get_file_content, analyze_commit_history],
)