"""CodeReviewAgent tests for the current provider-neutral runtime."""
from __future__ import annotations

import pytest

from src.agents import reviewer
from src.hooks import get_hooks_config


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
