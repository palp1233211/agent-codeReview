#!/usr/bin/env python3
"""
Code Review CLI - 基于 Claude Agent SDK
"""
import asyncio
import argparse
import os
import sys
from dotenv import load_dotenv

# 强制覆盖系统环境变量，确保 .env 优先
load_dotenv(override=True)

try:
    from claude_agent_sdk import query, ClaudeAgentOptions, AgentDefinition
except ImportError:
    print("❌ 错误: 请先安装 claude-agent-sdk")
    print("   pip install claude-agent-sdk")
    sys.exit(1)


def _check_env():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    base_url = os.getenv("ANTHROPIC_BASE_URL", "")
    if not api_key:
        print("❌ 错误: 未设置 ANTHROPIC_API_KEY，请在 .env 文件中配置")
        sys.exit(1)
    if base_url:
        print(f"🔗 API endpoint: {base_url}")


async def cmd_yunxiao_mr(
    repository_id: str,
    local_id: str,
    organization_id: str,
    dimensions: list[str] | None,
    auto_comment: bool,
) -> None:
    """审查云效 MR，直接调用 CodeReviewAgent"""
    from src.agents.reviewer import CodeReviewAgent

    print(f"\n🚀 开始审查 MR #{local_id}（仓库: {repository_id}）")
    print(f"   维度: {dimensions or 'all'}")
    print(f"   自动评论: {auto_comment}")
    print()

    agent = CodeReviewAgent()

    dim_values = None
    if dimensions and "all" not in dimensions:
        dim_values = dimensions

    result = await agent.review_yunxiao_mr(
        repository_id=repository_id,
        local_id=local_id,
        organization_id=organization_id,
        dimensions=dim_values,
        auto_comment=auto_comment,
    )

    # 统计实际工具调用
    tools_used = result.get("tools_used", [])
    comment_calls = [t for t in tools_used if "create_change_request_comment" in t]

    print("=" * 50)
    print("✅ 审查完成")
    print("=" * 50)
    print(f"\n🔧 工具调用次数: {len(tools_used)}")

    if auto_comment:
        if comment_calls:
            print(f"💬 评论已发布到 MR（共 {len(comment_calls)} 条）")
        else:
            print("⚠️  未检测到评论调用，评论可能未成功发布")

    print("\n📝 审查摘要:")
    print("-" * 40)
    summary = result.get("summary", "（无摘要）")
    print(summary[:800] + "..." if len(summary) > 800 else summary)


async def cmd_files(file_paths: list[str], dimensions: list[str] | None) -> None:
    """审查本地文件"""
    from src.agents.reviewer import CodeReviewAgent

    print(f"\n🚀 审查文件: {file_paths}")
    agent = CodeReviewAgent()
    result = await agent.review_files(
        file_paths=file_paths,
        dimensions=dimensions if dimensions and "all" not in dimensions else None,
    )
    print("\n📝 审查结果:")
    print(result.get("summary", "（无摘要）"))


async def cmd_diff(base: str, target: str, dimensions: list[str] | None) -> None:
    """审查 Git diff"""
    from src.agents.reviewer import CodeReviewAgent

    print(f"\n🚀 审查 diff: {base} → {target}")
    agent = CodeReviewAgent()
    result = await agent.review_git_diff(
        base_branch=base,
        target_branch=target,
        dimensions=dimensions if dimensions and "all" not in dimensions else None,
    )
    print("\n📝 审查结果:")
    print(result.get("summary", "（无摘要）"))


def main():
    _check_env()

    parser = argparse.ArgumentParser(
        description="Code Review CLI - 基于 Claude Agent SDK",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 审查云效 MR（自动发中文评论）
  python cli.py yunxiao-mr -r 3865544 -m 968

  # 不发评论，只看报告
  python cli.py yunxiao-mr -r 3865544 -m 968 --no-comment

  # 只审查安全维度
  python cli.py yunxiao-mr -r 3865544 -m 968 -d security

  # 审查本地文件
  python cli.py files src/main.py

  # 审查 Git diff
  python cli.py diff -b main -t feature
""",
    )

    subparsers = parser.add_subparsers(dest="command")

    # yunxiao-mr
    p = subparsers.add_parser("yunxiao-mr", help="审查云效 MR")
    p.add_argument("-r", "--repository", required=True, help="仓库ID")
    p.add_argument("-m", "--mr-id", required=True, help="MR编号")
    p.add_argument("-o", "--organization", default=None, help="组织ID（默认读取 YUNXIAO_ORG_ID 环境变量）")
    p.add_argument("-d", "--dimensions", nargs="+",
                   choices=["security", "quality", "performance", "all"],
                   default=["all"], help="审查维度")
    p.add_argument("--no-comment", action="store_true", help="不自动发评论")

    # files
    p = subparsers.add_parser("files", help="审查本地文件")
    p.add_argument("paths", nargs="+", help="文件路径")
    p.add_argument("-d", "--dimensions", nargs="+",
                   choices=["security", "quality", "performance", "all"],
                   default=["all"])

    # diff
    p = subparsers.add_parser("diff", help="审查 Git diff")
    p.add_argument("-b", "--base", default="main", help="基准分支")
    p.add_argument("-t", "--target", default="HEAD", help="目标分支")
    p.add_argument("-d", "--dimensions", nargs="+",
                   choices=["security", "quality", "performance", "all"],
                   default=["all"])

    args = parser.parse_args()

    print("=" * 50)
    print("🔍 Claude Agent Code Review")
    print("=" * 50)

    if args.command == "yunxiao-mr":
        asyncio.run(cmd_yunxiao_mr(
            repository_id=args.repository,
            local_id=args.mr_id,
            organization_id=args.organization or os.getenv("YUNXIAO_ORG_ID", "5ea86562f89c9700014a671f"),
            dimensions=args.dimensions,
            auto_comment=not args.no_comment,
        ))
    elif args.command == "files":
        asyncio.run(cmd_files(args.paths, args.dimensions))
    elif args.command == "diff":
        asyncio.run(cmd_diff(args.base, args.target, args.dimensions))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
