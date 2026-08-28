#!/usr/bin/env python3
"""
Code Review CLI - 支持 Claude Agent SDK / OpenAI SDK
"""
import asyncio
import argparse
import os
import sys
from dotenv import load_dotenv

# 强制覆盖系统环境变量，确保 .env 优先
load_dotenv(override=True)

try:
    from openai import OpenAI  # noqa: F401
    from claude_agent_sdk import query as _claude_query  # noqa: F401
except ImportError:
    print("❌ 错误: 请先安装项目依赖")
    print("   pip install -r requirements.txt")
    sys.exit(1)


def _check_env():
    provider = (os.getenv("AGENT_PROVIDER") or os.getenv("AGENT_SDK") or "claude").lower()
    print(f"🧭 Provider: {provider}")
    if provider in {"openai", "openai_sdk"}:
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL", "")
        model = os.getenv("OPENAI_MODEL", "gpt-5.4")
        if not api_key:
            print("❌ 错误: 未设置 OPENAI_API_KEY，请在 .env 文件中配置")
            sys.exit(1)
        if base_url:
            print(f"🔗 API endpoint: {base_url}")
        print(f"🤖 Model: {model}")
        return

    api_key = os.getenv("ANTHROPIC_API_KEY")
    base_url = os.getenv("ANTHROPIC_BASE_URL", "")
    if not api_key:
        print("❌ 错误: 未设置 ANTHROPIC_API_KEY，请在 .env 文件中配置")
        sys.exit(1)
    if base_url:
        print(f"🔗 Claude endpoint: {base_url}")


async def cmd_yunxiao_mr(
    repository_id: str,
    local_id: str,
    organization_id: str,
    dimensions: list[str] | None,
    auto_comment: bool,
    business_type: str,
) -> int:
    """审查云效 MR，直接调用 CodeReviewAgent"""
    from src.agents.reviewer import CodeReviewAgent, _normalize_yunxiao_repository_id

    print(f"\n🚀 开始审查 MR #{local_id}（仓库: {repository_id}）")
    print(f"   MCP仓库参数: {_normalize_yunxiao_repository_id(repository_id)}")
    print(f"   业务类型: {business_type}")
    print(f"   维度: {dimensions or 'all'}")
    print(f"   自动评论: {auto_comment}")
    print()

    agent = CodeReviewAgent(business_type=business_type)

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
    comment_calls = [
        t for t in tools_used
        if "comment_on_yunxiao_mr" in t or "create_change_request_comment" in t
    ]

    if result.get("is_error"):
        result_type = result.get("result_type") or "error"
        print("=" * 50)
        print(f"❌ 审查失败（{result_type}）")
        print("=" * 50)
        summary = result.get("summary")
        if summary:
            print("\n📝 当前输出:")
            print("-" * 40)
            print(summary)
        return 1

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
    print(summary)
    return 0


def cmd_lark_bot() -> int:
    """启动飞书机器人长连接（WebSocket 模式），阻塞运行直到被中断"""
    from src.lark.ws_bot import run_ws_bot

    print("\n🤖 启动飞书长连接机器人...")
    return run_ws_bot()


def cmd_kb_doctor() -> int:
    """检查知识盲区流水线的运行前提（路径 / 密钥 / 数据表）"""
    from src.knowledge.doctor import format_report, run_checks

    print("\n🩺 知识盲区流水线就绪检查\n")
    report, ok = format_report(run_checks())
    print(report)
    print("\n" + ("✅ 全部就绪" if ok else "❌ 有阻塞项，修好再跑 lark-bot"))
    return 0 if ok else 1


def cmd_kb_import(dry_run: bool) -> int:
    """接管 Dify 知识库里已存在的文档：拉回本地 + 登记索引 + 建立映射"""
    import os

    from src.dify.dataset_client import DifyDatasetClient
    from src.knowledge.docs_repo import DocsRepo
    from src.knowledge.importer import DocumentImporter, fetch_document_text
    from src.storage.kb_document_store import KbDocumentStore

    try:
        dataset_client = DifyDatasetClient.from_env()
        docs_repo = DocsRepo.from_env()
        doc_store = KbDocumentStore.from_env()
    except (KeyError, ValueError) as exc:
        print(f"\n❌ 配置不完整：{exc}")
        return 1

    base_url = os.environ.get("DIFY_BASE_URL", "http://localhost/v1")
    api_key = os.environ["DIFY_DATASET_API_KEY"]

    importer = DocumentImporter(
        docs_repo=docs_repo,
        doc_store=doc_store,
        dataset_client=dataset_client,
        fetch_text=lambda document_id: fetch_document_text(
            base_url=base_url,
            api_key=api_key,
            dataset_id=dataset_client.dataset_id,
            document_id=document_id,
        ),
    )

    print(f"\n📥 {'试运行（不写任何东西）' if dry_run else '开始接管'} Dify 已有文档...")
    imported = importer.import_all(dry_run=dry_run)

    if not imported:
        print("没有需要接管的文档（可能都已接管过）。")
        return 0

    for doc in imported:
        print(f"  ✅ {doc.dify_document_name}")
        print(f"     -> {doc.doc_path}（{doc.segments} 段）")

    if dry_run:
        print(f"\n共 {len(imported)} 篇待接管。去掉 --dry-run 真正执行。")
    else:
        print(f"\n共接管 {len(imported)} 篇。请检查本地文件内容是否完整后再做同步。")
    return 0


async def cmd_bi_weekly_doc(date_str: str | None = None) -> None:
    """创建 BI 双周迭代上线文档套件"""
    from src.agents.bi_weekly_doc import run_bi_weekly_doc

    print("\n🚀 开始创建 BI 双周迭代文档...")
    result = await run_bi_weekly_doc(date_str=date_str)
    print(f"\n✅ 完成！日期：{result['date']}")
    print(f"\n  汇总文档：{result['summary_url']}")
    print(f"  泰国文档：{result['thai_url']}")
    print(f"  菲律宾文档：{result['ph_url']}")


async def cmd_files(file_paths: list[str], dimensions: list[str] | None) -> int:
    """审查本地文件"""
    from src.agents.reviewer import CodeReviewAgent

    print(f"\n🚀 审查文件: {file_paths}")
    agent = CodeReviewAgent()
    result = await agent.review_files(
        file_paths=file_paths,
        dimensions=dimensions if dimensions and "all" not in dimensions else None,
    )
    if result.get("is_error"):
        print(f"\n❌ 审查失败（{result.get('result_type') or 'error'}）")
        return 1
    print("\n📝 审查结果:")
    print(result.get("summary", "（无摘要）"))
    return 0


async def cmd_diff(base: str, target: str, dimensions: list[str] | None) -> int:
    """审查 Git diff"""
    from src.agents.reviewer import CodeReviewAgent

    print(f"\n🚀 审查 diff: {base} → {target}")
    agent = CodeReviewAgent()
    result = await agent.review_git_diff(
        base_branch=base,
        target_branch=target,
        dimensions=dimensions if dimensions and "all" not in dimensions else None,
    )
    if result.get("is_error"):
        print(f"\n❌ 审查失败（{result.get('result_type') or 'error'}）")
        return 1
    print("\n📝 审查结果:")
    print(result.get("summary", "（无摘要）"))
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Code Review CLI - 支持 Claude Agent SDK / OpenAI SDK",
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

  # 启动飞书长连接机器人
  python cli.py lark-bot

  # 接管 Dify 里已有的知识文档（先 --dry-run 看看会动哪些）
  python cli.py kb-import --dry-run
""",
    )

    subparsers = parser.add_subparsers(dest="command")

    # bi-weekly-doc
    p = subparsers.add_parser("bi-weekly-doc", help="创建 BI 双周迭代上线文档套件（汇总 + 泰国 + 菲律宾）")
    p.add_argument("--date", default=None, help="指定日期，格式 YYYYMMDD（默认取本周四）")

    # lark-bot
    subparsers.add_parser("lark-bot", help="启动飞书机器人长连接（WebSocket 模式）")

    # kb-doctor
    subparsers.add_parser("kb-doctor", help="检查知识盲区流水线的运行前提")

    # kb-import
    p = subparsers.add_parser("kb-import", help="接管 Dify 知识库里已存在的文档到本地")
    p.add_argument("--dry-run", action="store_true", help="只看会接管哪些，不写任何东西")

    # yunxiao-mr
    p = subparsers.add_parser("yunxiao-mr", help="审查云效 MR")
    p.add_argument("-r", "--repository", required=True, help="仓库数字ID、完整路径或 MR URL")
    p.add_argument("-m", "--mr-id", required=True, help="MR编号")
    p.add_argument("-o", "--organization", default=None, help="组织ID（默认读取 YUNXIAO_ORG_ID 环境变量）")
    p.add_argument("-d", "--dimensions", nargs="+",
                   choices=["security", "quality", "performance", "all"],
                   default=["all"], help="审查维度")
    p.add_argument("--no-comment", action="store_true", help="不自动发评论")
    p.add_argument("-b", "--business", default="default",
                   choices=["default", "frontend", "backend"],
                   help="业务类型（default/frontend/backend）")

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

    if args.command == "bi-weekly-doc":
        asyncio.run(cmd_bi_weekly_doc(date_str=args.date))
        return

    if args.command == "lark-bot":
        sys.exit(cmd_lark_bot())

    if args.command == "kb-doctor":
        sys.exit(cmd_kb_doctor())

    if args.command == "kb-import":
        sys.exit(cmd_kb_import(dry_run=args.dry_run))

    _check_env()
    print("=" * 50)
    print("🔍 Agent Code Review")
    print("=" * 50)

    if args.command == "yunxiao-mr":
        sys.exit(asyncio.run(cmd_yunxiao_mr(
            repository_id=args.repository,
            local_id=args.mr_id,
            organization_id=args.organization or os.getenv("YUNXIAO_ORG_ID", "5ea86562f89c9700014a671f"),
            dimensions=args.dimensions,
            auto_comment=not args.no_comment,
            business_type=args.business,
        )))
    elif args.command == "files":
        sys.exit(asyncio.run(cmd_files(args.paths, args.dimensions)))
    elif args.command == "diff":
        sys.exit(asyncio.run(cmd_diff(args.base, args.target, args.dimensions)))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
