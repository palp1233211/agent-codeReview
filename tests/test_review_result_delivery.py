"""Review 群消息发送的幂等行为。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.review_result import (
    _merge_suggestion,
    build_review_content_rows,
    execute_review_task,
    format_review_result,
    publish_review_result,
)


MR_RESULT = {
    "summary": "### 📊 审查结论\n- 问题统计：Critical 0 / High 0 / Medium 0 / Low 0 / Info 0\n- 合并建议：建议合并。",
    "is_error": False,
    "metadata": {"auto_comment": True},
    "mr_metadata": {
        "title": "同步 已签收未收到 parent_source",
        "author": {
            "name": "刘文超",
            "username": "刘文超",
            "email": "liuwenchao@flashexpress.com",
        },
        "sourceBranch": "feature/20260910/lwc_fix",
        "targetBranch": "feature/20260910/common",
    },
}


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("问题统计：Critical 1 / High 0 / Medium 0 / Low 0 / Info 0", "❌ 不允许合并"),
        ("问题统计：Critical 0 / High 2 / Medium 1 / Low 0 / Info 0", "❌ 不允许合并"),
        ("问题统计：Critical 0 / High 0 / Medium 1 / Low 2 / Info 0", "⚠️ 需要人工复查"),
        ("问题统计：Critical 0 / High 0 / Medium 0 / Low 3 / Info 1", "✅ 建议合并"),
        ("问题统计：Critical 0 / High 0 / Medium 0 / Low 0 / Info 0", "✅ 建议合并"),
    ],
)
def test_merge_suggestion_uses_severity_counts(summary, expected):
    assert _merge_suggestion(summary) == expected


def test_merge_suggestion_requires_manual_review_when_counts_are_missing():
    assert _merge_suggestion("合并建议：建议合并。") == "⚠️ 需要人工复查"


def test_review_delivery_uses_stable_uuid_and_persists_message_id():
    result = MR_RESULT
    agent = MagicMock()
    agent.review_yunxiao_mr = AsyncMock(return_value=result)
    store = MagicMock()
    store.find_feishu_user_id_by_name.return_value = "d59cef46"
    lark = MagicMock()
    lark.get_user_open_id_by_email.return_value = "ou_author"
    lark.send_review_message.return_value = "om_result"

    with patch("src.agents.review_result.CodeReviewAgent", return_value=agent):
        asyncio.run(execute_review_task(
            task_id=7,
            repository_id="repo",
            local_id="12",
            organization_id="org",
            mr_url="https://codeup.aliyun.com/a/b/change/12",
            store=store,
            lark_client=lark,
            chat_id="oc_review",
        ))

    first_uuid = lark.send_review_message.call_args.args[2]
    assert len(first_uuid) == 36
    assert agent.review_yunxiao_mr.call_args.kwargs["auto_comment"] is True
    assert lark.send_review_message.call_args.args[3] == "d59cef46"
    lark.get_user_open_id_by_email.assert_not_called()
    assert store.update_result.call_args_list[-1].kwargs["status"] == "sent"
    assert store.update_result.call_args_list[-1].kwargs["review_message_id"] == "om_result"


def test_review_notification_matches_requested_layout():
    text = format_review_result(MR_RESULT, mr_url="https://codeup.example/change/1")
    assert "📋 代码审查完成" in text
    assert "📌 标题: 同步 已签收未收到 parent_source" in text
    assert "👤 作者: 刘文超" in text
    assert "via Codeup" not in text
    assert "🌿 分支: feature/20260910/lwc_fix → feature/20260910/common" in text
    assert "🎯 合并建议: ✅ 建议合并" in text
    assert "详细审查结果已提交到 MR 评论中" in text

    rows = build_review_content_rows(
        MR_RESULT,
        "https://codeup.example/change/1",
        "ou_author",
    )
    assert rows[2][-1] == {
        "tag": "at",
        "user_id": "ou_author",
    }
    assert all(item.get("tag") != "at" for item in rows[-1])
    assert not any(
        item.get("tag") == "text" and not item.get("text")
        for row in rows
        for item in row
    )


def test_no_comment_notification_does_not_claim_mr_comment_created():
    result = {**MR_RESULT, "metadata": {"auto_comment": False}}
    text = format_review_result(result, mr_url="https://codeup.example/change/1")
    assert "详细审查结果请查看本次 CLI 输出" in text
    assert "已提交到 MR 评论" not in text


def test_same_task_uses_same_lark_uuid_on_retry():
    store = MagicMock()
    store.find_feishu_user_id_by_name.return_value = "d59cef46"
    lark = MagicMock()
    lark.get_user_open_id_by_email.return_value = "ou_author"
    lark.send_review_message.return_value = "om_result"

    for _ in range(2):
        publish_review_result(
            task_id=9,
            result=MR_RESULT,
            mr_url="https://codeup.example/change/1",
            store=store,
            lark_client=lark,
            chat_id="oc_review",
        )

    uuids = [call.args[2] for call in lark.send_review_message.call_args_list]
    assert len(set(uuids)) == 1


def test_email_lookup_is_fallback_when_name_mapping_missing():
    store = MagicMock()
    store.find_feishu_user_id_by_name.return_value = ""
    lark = MagicMock()
    lark.get_user_open_id_by_email.return_value = "ou_author"
    lark.send_review_message.return_value = "om_result"

    publish_review_result(
        task_id=10,
        result=MR_RESULT,
        mr_url="https://codeup.example/change/1",
        store=store,
        lark_client=lark,
        chat_id="oc_review",
    )

    lark.get_user_open_id_by_email.assert_called_once_with(
        "liuwenchao@flashexpress.com"
    )
    assert lark.send_review_message.call_args.args[3] == "ou_author"
