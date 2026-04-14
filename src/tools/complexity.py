"""代码复杂度分析工具"""
import json
import subprocess
from pathlib import Path

from claude_agent_sdk import tool, create_sdk_mcp_server


@tool(
    name="analyze_complexity",
    description="分析代码的圈复杂度（Cyclomatic Complexity）和可维护性指数。",
    input_schema={
        "file_path": {
            "type": "string",
            "description": "要分析的文件路径",
        },
        "language": {
            "type": "string",
            "description": "编程语言，可选（自动检测）",
        },
    },
)
async def analyze_complexity(file_path: str, language: str | None = None) -> dict:
    """分析代码复杂度"""
    path = Path(file_path)

    if not path.exists():
        return {
            "content": [{"type": "text", "text": f"文件不存在: {file_path}"}]
        }

    # 使用 radon 分析 Python 代码复杂度
    if path.suffix == ".py":
        result = subprocess.run(
            ["radon", "cc", "-j", file_path],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                issues = []

                for filename, functions in data.items():
                    for func in functions:
                        complexity = func.get("complexity", 0)
                        rank = func.get("rank", "A")

                        # 复杂度等级: A(简单) B(较低) C(中等) D(较高) E(很高) F(极高)
                        if rank in ["D", "E", "F"]:
                            issues.append(
                                {
                                    "function": func.get("name"),
                                    "line": func.get("lineno"),
                                    "complexity": complexity,
                                    "rank": rank,
                                    "message": f"函数 {func.get('name')} 复杂度等级 {rank}（值为 {complexity}），建议拆分或简化",
                                }
                            )

                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"复杂度分析结果:\n"
                            + f"- 总函数数: {sum(len(f) for f in data.values())}\n"
                            + f"- 高复杂度函数: {len(issues)}\n"
                            + "\n".join(
                                f"- {i['function']} (行 {i['line']}): 等级 {i['rank']}"
                                for i in issues
                            ),
                        }
                    ],
                    "issues": issues,
                }
            except json.JSONDecodeError:
                return {
                    "content": [
                        {"type": "text", "text": "无法解析复杂度分析结果"}
                    ]
                }

    # 其他语言的简单分析
    with open(path, encoding="utf-8", errors="ignore") as f:
        content = f.read()

    lines = content.split("\n")
    total_lines = len(lines)
    code_lines = sum(1 for line in lines if line.strip() and not line.strip().startswith("#"))

    # 简单的嵌套深度分析
    max_nesting = 0
    current_nesting = 0
    for line in lines:
        if any(kw in line for kw in ["if", "for", "while", "def", "function", "class"]):
            current_nesting += 1
            max_nesting = max(max_nesting, current_nesting)
        if any(kw in line for kw in ["end", "}", "}"]):
            current_nesting -= 1

    return {
        "content": [
            {
                "type": "text",
                "text": f"代码统计:\n"
                f"- 总行数: {total_lines}\n"
                f"- 代码行数: {code_lines}\n"
                f"- 最大嵌套深度: {max_nesting}",
            }
        ],
        "metrics": {
            "total_lines": total_lines,
            "code_lines": code_lines,
            "max_nesting": max_nesting,
        },
    }


@tool(
    name="analyze_maintainability",
    description="分析代码的可维护性指数（Maintainability Index）。",
    input_schema={
        "file_path": {
            "type": "string",
            "description": "文件路径",
        },
    },
)
async def analyze_maintainability(file_path: str) -> dict:
    """分析可维护性指数"""
    path = Path(file_path)

    if path.suffix != ".py":
        return {
            "content": [
                {"type": "text", "text": "可维护性分析仅支持 Python 代码"}
            ]
        }

    result = subprocess.run(
        ["radon", "mi", "-j", file_path],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        try:
            data = json.loads(result.stdout)
            issues = []

            for filename, mi in data.items():
                # MI 范围 0-100, < 65 表示难以维护
                if isinstance(mi, (int, float)) and mi < 65:
                    issues.append(
                        {
                            "file": filename,
                            "mi": mi,
                            "message": f"可维护性指数 {mi:.1f}，低于 65 表示难以维护",
                        }
                    )

            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"可维护性分析:\n"
                        + "\n".join(
                            f"- {i['file']}: MI={i['mi']:.1f}"
                            for i in issues
                        )
                        if issues
                        else "所有文件可维护性良好",
                    }
                ],
                "issues": issues,
            }
        except json.JSONDecodeError:
            pass

    return {
        "content": [
            {"type": "text", "text": "可维护性分析完成"}
        ]
    }


@tool(
    name="check_code_duplication",
    description="检查代码重复（Copy-Paste Detector）。",
    input_schema={
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "要检查的文件/目录路径列表",
        },
        "min_lines": {
            "type": "integer",
            "description": "最小重复行数阈值，默认 4",
        },
    },
)
async def check_code_duplication(paths: list[str], min_lines: int = 4) -> dict:
    """检查代码重复"""
    # 简化的重复检测逻辑
    duplicates = []

    for path_str in paths:
        path = Path(path_str)
        if not path.exists():
            continue

        files = [path] if path.is_file() else list(path.rglob("*.py"))

        # 读取所有文件内容
        file_blocks: dict[str, list[tuple[int, str]]] = {}
        for file in files:
            with open(file, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                # 将连续的代码块分组
                block = []
                block_start = 1
                for i, line in enumerate(lines, 1):
                    if line.strip():
                        block.append(line.strip())
                    if len(block) >= min_lines:
                        file_blocks[str(file)] = file_blocks.get(str(file), [])
                        file_blocks[str(file)].append((block_start, "\n".join(block)))
                        block = []
                        block_start = i + 1

        # 检查跨文件重复
        for file1, blocks1 in file_blocks.items():
            for file2, blocks2 in file_blocks.items():
                if file1 != file2:
                    for start1, code1 in blocks1:
                        for start2, code2 in blocks2:
                            if code1 == code2 and len(code1) >= min_lines:
                                duplicates.append(
                                    {
                                        "file1": file1,
                                        "line1": start1,
                                        "file2": file2,
                                        "line2": start2,
                                        "lines": len(code1.split("\n")),
                                    }
                                )

    return {
        "content": [
            {
                "type": "text",
                "text": f"重复代码检测:\n"
                + f"- 发现 {len(duplicates)} 处重复\n"
                + "\n".join(
                    f"- {d['file1']}:{d['line1']} 与 {d['file2']}:{d['line2']} 重复 {d['lines']} 行"
                    for d in duplicates[:10]  # 只显示前10个
                ),
            }
        ],
        "duplicates": duplicates,
    }


# 创建复杂度分析 MCP Server
complexity_server = create_sdk_mcp_server(
    name="complexity-tools",
    version="1.0.0",
    tools=[analyze_complexity, analyze_maintainability, check_code_duplication],
)