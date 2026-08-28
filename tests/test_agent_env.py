"""clean_claude_env 测试。

踩过的坑：本服务在 Claude Code 会话里启动时，CLAUDECODE / CLAUDE_CODE_* 会被
继承给我们 fork 的 claude CLI，它会误判自己是受控子会话、等待父进程的控制协议，
表现为静默挂死没有任何输出——排查时极易误判成"凭证有问题"。
"""
from unittest.mock import patch

from src.agents.runtime import clean_claude_env


def test_strips_claude_code_session_vars():
    fake = {
        "CLAUDECODE": "1",
        "CLAUDE_CODE_CHILD_SESSION": "1",
        "CLAUDE_CODE_SESSION_ID": "abc",
        "CLAUDE_AGENT_SDK_VERSION": "0.3.229",
        "PATH": "/usr/bin",
    }
    with patch.dict("os.environ", fake, clear=True):
        env = clean_claude_env()

    assert "CLAUDECODE" not in env
    assert not [k for k in env if k.startswith("CLAUDE_")]
    assert env["PATH"] == "/usr/bin"


def test_keeps_anthropic_credentials():
    """凭证必须留下，剥掉就没法鉴权了。"""
    fake = {
        "ANTHROPIC_API_KEY": "sk-x",
        "ANTHROPIC_BASE_URL": "https://example.com",
        "ANTHROPIC_AUTH_TOKEN": "tok",
        "CLAUDECODE": "1",
    }
    with patch.dict("os.environ", fake, clear=True):
        env = clean_claude_env()

    assert env["ANTHROPIC_API_KEY"] == "sk-x"
    assert env["ANTHROPIC_BASE_URL"] == "https://example.com"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "tok"


def test_keeps_pipeline_config():
    fake = {"FBI_REPO_PATH": "/repo", "KNOWLEDGE_DOCS_DIR": "/docs", "CLAUDE_PID": "1"}
    with patch.dict("os.environ", fake, clear=True):
        env = clean_claude_env()

    assert env["FBI_REPO_PATH"] == "/repo"
    assert env["KNOWLEDGE_DOCS_DIR"] == "/docs"
    assert "CLAUDE_PID" not in env


def test_returns_plain_dict_not_environ_view():
    """必须是快照，不能是 os.environ 的活引用，否则改一处影响全局。"""
    with patch.dict("os.environ", {"A": "1"}, clear=True):
        env = clean_claude_env()
        env["B"] = "2"

    import os

    assert "B" not in os.environ
