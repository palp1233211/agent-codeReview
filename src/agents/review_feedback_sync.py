"""Publish Code Review feedback below the original Yunxiao MR comment."""
from __future__ import annotations

import json
from typing import Any, Callable

from .mcp_client import HttpMcpClient, SseMcpClient, create_http_mcp_clients
from .reviewer import _get_yunxiao_mcp_config


class ReviewFeedbackSyncError(RuntimeError):
    """The feedback was analysed, but could not be safely synced to Yunxiao."""


def _decode_mcp_json(result: dict[str, Any]) -> Any:
    texts = [
        str(block.get("text") or "")
        for block in result.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    if not texts:
        raise ReviewFeedbackSyncError("云效工具未返回可解析的文本结果")
    try:
        return json.loads("\n".join(texts))
    except (TypeError, ValueError) as exc:
        raise ReviewFeedbackSyncError("云效工具返回的不是 JSON 数据") from exc


def _normalise_comment_content(content: str) -> str:
    return content.replace("\r\n", "\n").strip()


def _find_original_comment(
    comments: Any,
    *,
    original_content: str,
) -> dict[str, Any]:
    if not original_content:
        raise ReviewFeedbackSyncError("任务未保存原 MR 评论正文，无法精确定位要回复的评论")
    if not isinstance(comments, list):
        raise ReviewFeedbackSyncError("云效评论列表格式不正确")
    expected = _normalise_comment_content(original_content)
    matches = [
        item for item in comments
        if isinstance(item, dict)
        and _normalise_comment_content(str(item.get("content") or "")) == expected
    ]
    if len(matches) != 1:
        raise ReviewFeedbackSyncError(
            f"无法精确定位原 MR 评论：匹配数量为 {len(matches)}，不会猜测目标评论"
        )
    comment = matches[0]
    if not str(comment.get("comment_biz_id") or ""):
        raise ReviewFeedbackSyncError("原 MR 评论缺少 comment_biz_id")
    patchset = comment.get("related_patchset")
    if not isinstance(patchset, dict) or not str(patchset.get("patchSetBizId") or ""):
        raise ReviewFeedbackSyncError("原 MR 评论缺少关联 patchSetBizId")
    return comment


def _quote_markdown(text: str) -> str:
    return "\n".join(">" if not line else f"> {line}" for line in text.strip().splitlines())


def format_yunxiao_feedback_reply(
    *,
    feedback_text: str,
    analysis: dict[str, Any],
) -> str:
    """Build one child MR comment containing both the user's feedback and AI verdict."""
    verdict_labels = {
        "correct": "✅ 原 Review 结论成立",
        "false_positive": "⚠️ 确认误报",
        "uncertain": "❓ 信息不足，暂无法判断",
    }
    verdict = str(analysis.get("verdict") or "uncertain")
    confidence = f"{float(analysis.get('confidence') or 0) * 100:.0f}%"
    lines = [
        "## 🤖 Code Review 反馈",
        "",
        "### 用户回复",
        _quote_markdown(feedback_text),
        "",
        "### AI 反馈分析",
        f"- 结论：{verdict_labels.get(verdict, verdict_labels['uncertain'])}",
        f"- 置信度：{confidence}",
    ]
    if verdict == "false_positive":
        lines.append(f"- 误报原因：{str(analysis.get('reason_summary') or '未提供')}")
    return "\n".join(lines)


def _comment_id_from_result(result: dict[str, Any]) -> str:
    value = _decode_mcp_json(result)
    candidates = [value]
    if isinstance(value, dict):
        candidates.extend([value.get("data"), value.get("comment")])
    for candidate in candidates:
        if isinstance(candidate, dict):
            comment_id = candidate.get("comment_biz_id") or candidate.get("commentBizId")
            if comment_id:
                return str(comment_id)
    return ""


def sync_review_feedback_to_yunxiao(
    *,
    organization_id: str,
    repository_id: str,
    local_id: str,
    original_comment_content: str,
    feedback_text: str,
    analysis: dict[str, Any],
    client_factory: Callable[[list[dict[str, Any]]], list[HttpMcpClient | SseMcpClient]] = create_http_mcp_clients,
) -> dict[str, Any]:
    """Reply to the exact original review comment and close it only for a false positive."""
    clients = client_factory([_get_yunxiao_mcp_config()])
    if len(clients) != 1:
        raise ReviewFeedbackSyncError("云效 MCP 客户端初始化异常")
    client = clients[0]
    try:
        comments_result = client.call_tool(
            "list_change_request_comments",
            {
                "organizationId": organization_id,
                "repositoryId": repository_id,
                "localId": local_id,
                "commentType": "GLOBAL_COMMENT",
                "state": "OPENED",
                "resolved": False,
            },
        )
        original = _find_original_comment(
            _decode_mcp_json(comments_result),
            original_content=original_comment_content,
        )
        original_comment_id = str(original["comment_biz_id"])
        patchset_biz_id = str(original["related_patchset"]["patchSetBizId"])
        reply_result = client.call_tool(
            "create_change_request_comment",
            {
                "organizationId": organization_id,
                "repositoryId": repository_id,
                "localId": local_id,
                "comment_type": "GLOBAL_COMMENT",
                "patchset_biz_id": patchset_biz_id,
                "parent_comment_biz_id": original_comment_id,
                "content": format_yunxiao_feedback_reply(
                    feedback_text=feedback_text,
                    analysis=analysis,
                ),
            },
        )
        sync = {
            "original_comment_biz_id": original_comment_id,
            "reply_comment_biz_id": _comment_id_from_result(reply_result),
            "closed_original_comment": False,
        }
        if analysis.get("verdict") == "false_positive":
            client.call_tool(
                "update_change_request_comment",
                {
                    "organizationId": organization_id,
                    "repositoryId": repository_id,
                    "localId": local_id,
                    "commentBizId": original_comment_id,
                    "resolved": True,
                },
            )
            sync["closed_original_comment"] = True
        return sync
    finally:
        client.close()
