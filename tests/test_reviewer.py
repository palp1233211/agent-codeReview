"""CodeReviewAgent tests for the current provider-neutral runtime."""
from __future__ import annotations

import pytest

from src.agents import reviewer
from src.hooks import get_hooks_config


@pytest.mark.parametrize(
    ("reference", "local_id", "expected_repository", "expected_local_id"),
    [
        (
            "https://code.aliyun.com/org/project/change/123",
            None,
            "org%2Fproject",
            "123",
        ),
        (
            "https://code.aliyun.com/org/project/merge_requests/456?foo=bar",
            None,
            "org%2Fproject",
            "456",
        ),
        ("2835387", "42", "2835387", "42"),
    ],
)
def test_parse_yunxiao_mr_reference(
    reference,
    local_id,
    expected_repository,
    expected_local_id,
):
    assert reviewer.parse_yunxiao_mr_reference(reference, local_id) == (
        expected_repository,
        expected_local_id,
    )


def test_parse_yunxiao_mr_reference_rejects_missing_local_id():
    with pytest.raises(ValueError, match="必须同时提供 MR 编号"):
        reviewer.parse_yunxiao_mr_reference("2835387")


def test_parse_yunxiao_mr_reference_rejects_mismatched_ids():
    with pytest.raises(ValueError, match="不一致"):
        reviewer.parse_yunxiao_mr_reference(
            "https://code.aliyun.com/org/project/change/123",
            "456",
        )


def test_hooks_config():
    config = get_hooks_config()

    assert "PreToolUse" in config
    assert "PostToolUse" in config
    assert "UserPromptSubmit" in config


def test_agent_initialization_uses_runtime_factory(monkeypatch):
    runtime = object()
    hooks = {"PreToolUse": []}
    monkeypatch.setattr(reviewer, "create_agent_runtime", lambda: runtime)

    agent = reviewer.CodeReviewAgent(business_type="backend", custom_hooks=hooks)

    assert agent.runtime is runtime
    assert agent.hooks is hooks
    assert agent.business_type == "backend"


@pytest.mark.asyncio
async def test_review_code_snippet_uses_agent_runtime(monkeypatch):
    class FakeRuntime:
        prompt = None
        options = None

        async def run(self, prompt, options):
            self.prompt = prompt
            self.options = options
            return [
                {"type": "assistant", "content": ["reviewed"]},
                {"type": "result", "subtype": "success", "content": "reviewed"},
            ]

    runtime = FakeRuntime()
    monkeypatch.setattr(reviewer, "create_agent_runtime", lambda: runtime)
    agent = reviewer.CodeReviewAgent(custom_hooks={"PreToolUse": []})

    result = await agent.review_code_snippet(
        code="password = 'hardcoded_secret'",
        language="python",
        filename="example.py",
        dimensions=["security"],
    )

    assert "password = 'hardcoded_secret'" in runtime.prompt
    assert "语言: python" in runtime.prompt
    assert "文件名: example.py" in runtime.prompt
    assert runtime.options.allowed_tools == ["Agent"]
    assert runtime.options.allowed_agents == ["security-reviewer", "quality-reviewer"]
    assert result["summary"] == "reviewed"
    assert result["is_error"] is False
