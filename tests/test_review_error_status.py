"""Reviewer parsing and CLI exit-status tests."""
from __future__ import annotations

import sys
import importlib

import pytest

from src.agents import reviewer

cli_main = importlib.import_module("src.cli.main")


@pytest.mark.parametrize(
    ("messages", "expected_type"),
    [
        ([{"type": "result", "subtype": "max_turns", "is_error": True}], "max_turns"),
        ([{"type": "assistant", "content": ["partial"]}], "incomplete"),
        ([{"type": "result", "subtype": "success"}], "incomplete"),
    ],
)
def test_reviewer_preserves_specific_error_status(messages, expected_type):
    agent = object.__new__(reviewer.CodeReviewAgent)

    result = agent._parse_review_result(messages)  # noqa: SLF001

    assert result["is_error"] is True
    assert result["result_type"] == expected_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_code", "expected_text", "forbidden_text"),
    [
        (
            {"is_error": False, "result_type": "success", "summary": "done", "tools_used": []},
            0,
            "✅ 审查完成",
            "❌ 审查失败",
        ),
        (
            {"is_error": True, "result_type": "max_turns", "summary": "partial", "tools_used": []},
            1,
            "❌ 审查失败（max_turns）",
            "✅ 审查完成",
        ),
    ],
)
async def test_yunxiao_command_returns_review_exit_code(
    monkeypatch,
    capsys,
    result,
    expected_code,
    expected_text,
    forbidden_text,
):
    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        async def review_yunxiao_mr(self, **_kwargs):
            return result

    monkeypatch.setattr(reviewer, "CodeReviewAgent", FakeAgent)

    code = await cli_main.cmd_yunxiao_mr("repo", "1", "org", None, False, "default")

    output = capsys.readouterr().out
    assert code == expected_code
    assert expected_text in output
    assert forbidden_text not in output


def test_main_propagates_review_exit_code(monkeypatch):
    async def fake_command(**_kwargs):
        return 1

    monkeypatch.setattr(cli_main, "cmd_yunxiao_mr", fake_command)
    monkeypatch.setattr(cli_main, "_check_env", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cli.py", "yunxiao-mr", "-r", "repo", "-m", "1", "--no-comment"],
    )

    with pytest.raises(SystemExit) as exc:
        cli_main.main()

    assert exc.value.code == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("command_name", ["files", "diff"])
async def test_local_review_commands_return_error_code(monkeypatch, capsys, command_name: str):
    class FakeAgent:
        async def review_files(self, **_kwargs):
            return {"is_error": True, "result_type": "incomplete", "summary": ""}

        async def review_git_diff(self, **_kwargs):
            return {"is_error": True, "result_type": "max_turns", "summary": ""}

    monkeypatch.setattr(reviewer, "CodeReviewAgent", FakeAgent)
    if command_name == "files":
        code = await cli_main.cmd_files(["a.py"], None)
    else:
        code = await cli_main.cmd_diff("main", "HEAD", None)

    assert code == 1
    assert "❌ 审查失败" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("command_name", "argv"),
    [
        ("cmd_files", ["cli.py", "files", "a.py"]),
        ("cmd_diff", ["cli.py", "diff", "-b", "main", "-t", "HEAD"]),
    ],
)
def test_main_propagates_local_review_exit_codes(monkeypatch, command_name: str, argv: list[str]):
    async def fake_command(*_args, **_kwargs):
        return 1

    monkeypatch.setattr(cli_main, command_name, fake_command)
    monkeypatch.setattr(cli_main, "_check_env", lambda: None)
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exc:
        cli_main.main()

    assert exc.value.code == 1
