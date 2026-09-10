"""Review 反馈 AI 分析的输出约束测试。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.review_feedback import (
    analyze_review_feedback,
    feedback_reply_idempotency_key,
    format_feedback_reply,
)
from src.lark.code_review_ws_bot import _run_feedback_analysis


class _Runtime:
    def __init__(self, output: str):
        self.output = output
        self.prompt = ""

    async def run(self, prompt, options):
        self.prompt = prompt
        assert "Dify" in __import__("src.agents.review_feedback", fromlist=["__name__"]).__doc__
        assert options.allowed_tools == []
        return [
            {"type": "assistant", "content": [self.output]},
            {"type": "result", "subtype": "success", "is_error": False},
        ]


def test_false_positive_keeps_structured_reason():
    result = asyncio.run(
        analyze_review_feedback(
            review_text="没有判空会异常",
            feedback_text="前置校验已保证不为空",
            runtime=_Runtime(
                '{"verdict":"false_positive","reason_category":"missing_context",'
                '"reason_summary":"Review 忽略了前置非空校验。","confidence":0.91}'
            ),
        )
    )

    assert result["verdict"] == "false_positive"
    assert result["reason_category"] == "missing_context"
    assert result["confidence"] == 0.91


def test_feedback_analysis_receives_full_mr_review_evidence():
    runtime = _Runtime(
        '{"verdict":"false_positive","reason_category":"incorrect_assumption",'
        '"reason_summary":"实际返回值为 Ph，原结论的大小写假设不成立。","confidence":0.96}'
    )

    result = asyncio.run(
        analyze_review_feedback(
            review_text="原 Review：get_country_code() 的返回值为小写 ph，因此比较会失败。",
            feedback_text="get_country_code() 返回的就是 Ph，首字母大写。",
            runtime=runtime,
        )
    )

    assert "get_country_code() 的返回值为小写 ph" in runtime.prompt
    assert result["verdict"] == "false_positive"
    assert result["reason_category"] == "incorrect_assumption"


def test_correct_result_clears_false_positive_fields():
    result = asyncio.run(
        analyze_review_feedback(
            review_text="存在 SQL 注入风险",
            feedback_text="我会修复",
            runtime=_Runtime(
                '{"verdict":"correct","reason_category":"other",'
                '"reason_summary":"不应保留","confidence":0.8}'
            ),
        )
    )

    assert result["verdict"] == "correct"
    assert result["reason_category"] == ""
    assert result["reason_summary"] == ""


def test_false_positive_without_reason_is_rejected():
    with pytest.raises(ValueError, match="没有说明原因"):
        asyncio.run(
            analyze_review_feedback(
                review_text="Review",
                feedback_text="不对",
                runtime=_Runtime(
                    '{"verdict":"false_positive","reason_category":"other",'
                    '"reason_summary":"","confidence":0.5}'
                ),
            )
        )


def test_feedback_reply_text_contains_false_positive_reason():
    text = format_feedback_reply(
        {
            "verdict": "false_positive",
            "reason_summary": "前置校验已保证非空。",
            "confidence": 0.91,
        }
    )

    assert "确认误报" in text
    assert "前置校验已保证非空。" in text
    assert feedback_reply_idempotency_key(7) == feedback_reply_idempotency_key(7)


def test_feedback_analysis_replies_to_the_user_feedback_message(monkeypatch):
    analysis = {
        "verdict": "correct",
        "reason_category": "",
        "reason_summary": "",
        "confidence": 0.8,
        "raw": {"verdict": "correct"},
    }
    monkeypatch.setattr(
        "src.lark.code_review_ws_bot.analyze_review_feedback",
        AsyncMock(return_value=analysis),
    )
    monkeypatch.setattr(
        "src.lark.code_review_ws_bot.sync_review_feedback_to_yunxiao",
        lambda **_kwargs: {
            "original_comment_biz_id": "root_1",
            "reply_comment_biz_id": "child_1",
            "closed_original_comment": False,
        },
    )
    store = MagicMock()
    lark_client = MagicMock()
    lark_client.reply_text_message.return_value = "om_bot_reply"

    _run_feedback_analysis(
        feedback_id=9,
        feedback_message_id="om_user_feedback",
        review_text="Review 内容",
        feedback_text="我会修复",
        organization_id="org",
        repository_id="repo",
        local_id="12",
        original_comment_content="## 🤖 AI 代码审查报告\n原评论",
        store=store,
        lark_client=lark_client,
    )

    lark_client.reply_text_message.assert_called_once()
    assert lark_client.reply_text_message.call_args.args[0] == "om_user_feedback"
    assert "原 Review 结论成立" in lark_client.reply_text_message.call_args.args[1]
    assert "云效 MR 评论：已同步。" in lark_client.reply_text_message.call_args.args[1]
    statuses = [call.kwargs["status"] for call in store.update_feedback.call_args_list]
    assert statuses == ["analyzing", "analyzed"]


def test_reply_failure_does_not_discard_completed_analysis(monkeypatch):
    analysis = {
        "verdict": "false_positive",
        "reason_category": "missing_context",
        "reason_summary": "漏看前置校验。",
        "confidence": 0.9,
        "raw": {"verdict": "false_positive"},
    }
    monkeypatch.setattr(
        "src.lark.code_review_ws_bot.analyze_review_feedback",
        AsyncMock(return_value=analysis),
    )
    monkeypatch.setattr(
        "src.lark.code_review_ws_bot.sync_review_feedback_to_yunxiao",
        lambda **_kwargs: {
            "original_comment_biz_id": "root_1",
            "reply_comment_biz_id": "child_1",
            "closed_original_comment": True,
        },
    )
    store = MagicMock()
    lark_client = MagicMock()
    lark_client.reply_text_message.side_effect = RuntimeError("send failed")

    _run_feedback_analysis(
        feedback_id=10,
        feedback_message_id="om_user_feedback",
        review_text="Review 内容",
        feedback_text="这是误报",
        organization_id="org",
        repository_id="repo",
        local_id="12",
        original_comment_content="## 🤖 AI 代码审查报告\n原评论",
        store=store,
        lark_client=lark_client,
    )

    assert store.update_feedback.call_args_list[-1].kwargs == {
        "status": "analyzed",
        "error_message": "飞书群回复失败: send failed",
    }
