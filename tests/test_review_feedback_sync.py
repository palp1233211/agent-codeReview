"""Yunxiao MR reply/close behavior for Code Review user feedback."""
from __future__ import annotations

import json

import pytest

from src.agents.review_feedback_sync import (
    ReviewFeedbackSyncError,
    format_yunxiao_feedback_reply,
    sync_review_feedback_to_yunxiao,
)


ORIGINAL_COMMENT = "## 🤖 AI 代码审查报告\n\n#### 1. [问题] `a.php:11`"
ANALYSIS = {
    "verdict": "false_positive",
    "reason_category": "incorrect_assumption",
    "reason_summary": "get_country_code() 实际返回 Ph。",
    "confidence": 0.96,
}


def _mcp_result(value):
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}


class _Client:
    def __init__(self, comments):
        self.comments = comments
        self.calls = []
        self.closed = False

    def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "list_change_request_comments":
            return _mcp_result(self.comments)
        if name == "create_change_request_comment":
            return _mcp_result({"comment_biz_id": "child_1"})
        if name == "update_change_request_comment":
            return _mcp_result({"updated": True})
        raise AssertionError(name)

    def close(self):
        self.closed = True


def _original_comment(content=ORIGINAL_COMMENT):
    return {
        "comment_biz_id": "root_1",
        "content": content,
        "related_patchset": {"patchSetBizId": "patch_1"},
    }


def test_sync_replies_to_exact_original_comment_and_closes_false_positive(monkeypatch):
    client = _Client([_original_comment()])
    monkeypatch.setattr(
        "src.agents.review_feedback_sync._get_yunxiao_mcp_config",
        lambda: {"server_url": "https://example.test/mcp"},
    )

    sync = sync_review_feedback_to_yunxiao(
        organization_id="org",
        repository_id="repo",
        local_id="12",
        original_comment_content=ORIGINAL_COMMENT,
        feedback_text="get_country_code() 返回的就是 Ph。",
        analysis=ANALYSIS,
        client_factory=lambda _configs: [client],
    )

    assert sync == {
        "original_comment_biz_id": "root_1",
        "reply_comment_biz_id": "child_1",
        "closed_original_comment": True,
    }
    assert client.closed is True
    assert [name for name, _args in client.calls] == [
        "list_change_request_comments",
        "create_change_request_comment",
        "update_change_request_comment",
    ]
    reply_args = client.calls[1][1]
    assert reply_args["parent_comment_biz_id"] == "root_1"
    assert reply_args["patchset_biz_id"] == "patch_1"
    assert "get_country_code() 返回的就是 Ph。" in reply_args["content"]
    assert "确认误报" in reply_args["content"]
    assert client.calls[2][1]["resolved"] is True


def test_sync_does_not_close_comment_for_correct_review(monkeypatch):
    client = _Client([_original_comment()])
    monkeypatch.setattr(
        "src.agents.review_feedback_sync._get_yunxiao_mcp_config",
        lambda: {"server_url": "https://example.test/mcp"},
    )

    sync = sync_review_feedback_to_yunxiao(
        organization_id="org",
        repository_id="repo",
        local_id="12",
        original_comment_content=ORIGINAL_COMMENT,
        feedback_text="我会修复。",
        analysis={**ANALYSIS, "verdict": "correct", "reason_summary": ""},
        client_factory=lambda _configs: [client],
    )

    assert sync["closed_original_comment"] is False
    assert [name for name, _args in client.calls] == [
        "list_change_request_comments",
        "create_change_request_comment",
    ]


def test_sync_refuses_to_guess_when_original_comment_is_not_unique(monkeypatch):
    client = _Client([_original_comment(), _original_comment()])
    monkeypatch.setattr(
        "src.agents.review_feedback_sync._get_yunxiao_mcp_config",
        lambda: {"server_url": "https://example.test/mcp"},
    )

    with pytest.raises(ReviewFeedbackSyncError, match="不会猜测目标评论"):
        sync_review_feedback_to_yunxiao(
            organization_id="org",
            repository_id="repo",
            local_id="12",
            original_comment_content=ORIGINAL_COMMENT,
            feedback_text="这是误报。",
            analysis=ANALYSIS,
            client_factory=lambda _configs: [client],
        )

    assert [name for name, _args in client.calls] == ["list_change_request_comments"]
    assert client.closed is True


def test_mr_feedback_reply_includes_user_text_and_ai_analysis():
    text = format_yunxiao_feedback_reply(
        feedback_text="get_country_code() 返回的就是 Ph。",
        analysis=ANALYSIS,
    )

    assert "### 用户回复" in text
    assert "> get_country_code() 返回的就是 Ph。" in text
    assert "### AI 反馈分析" in text
    assert "误报原因：get_country_code() 实际返回 Ph。" in text
