"""Code Review Agent 测试"""
import pytest
from src.models import (
    ReviewRequest,
    ReviewDimension,
    SourceType,
    FilesSource,
    GitDiffSource,
    CodeSnippetSource,
)


def test_review_request_files():
    """测试文件审查请求模型"""
    request = ReviewRequest(
        source=FilesSource(
            type=SourceType.FILES,
            paths=["src/main.py", "src/utils.py"],
        ),
        dimensions=[ReviewDimension.SECURITY, ReviewDimension.QUALITY],
    )

    assert request.source.type == SourceType.FILES
    assert len(request.source.paths) == 2
    assert ReviewDimension.SECURITY in request.dimensions


def test_review_request_git_diff():
    """测试 Git diff 审查请求模型"""
    request = ReviewRequest(
        source=GitDiffSource(
            type=SourceType.GIT_DIFF,
            base_branch="main",
            target_branch="feature/test",
        ),
        dimensions=[ReviewDimension.ALL],
    )

    assert request.source.type == SourceType.GIT_DIFF
    assert request.source.base_branch == "main"


def test_review_request_code_snippet():
    """测试代码片段审查请求模型"""
    request = ReviewRequest(
        source=CodeSnippetSource(
            type=SourceType.CODE_SNIPPET,
            code="def hello(): print('hello')",
            language="python",
        ),
        dimensions=[ReviewDimension.QUALITY],
    )

    assert request.source.type == SourceType.CODE_SNIPPET
    assert "hello" in request.source.code


@pytest.mark.asyncio
async def test_hooks_config():
    """测试 Hooks 配置"""
    from src.hooks import get_hooks_config

    config = get_hooks_config()

    assert "PreToolUse" in config
    assert "PostToolUse" in config
    assert "UserPromptSubmit" in config


@pytest.mark.asyncio
async def test_agent_initialization():
    """测试 Agent 初始化"""
    from src.agents import CodeReviewAgent

    agent = CodeReviewAgent()

    assert agent.mcp_servers is not None
    assert agent.hooks is not None


@pytest.mark.asyncio
async def test_review_code_snippet():
    """测试代码片段审查"""
    from src.agents import default_review_agent

    # 简单的代码片段审查
    result = await default_review_agent.review_code_snippet(
        code="password = 'hardcoded_secret'",
        language="python",
        dimensions=["security"],
    )

    assert result is not None
    assert "summary" in result