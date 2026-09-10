"""Review 结果格式化和 Yunxiao 返回数据适配。"""
from __future__ import annotations

from typing import Any
import logging
import uuid
import json
import re

from .reviewer import CodeReviewAgent
from .reviewer import _get_yunxiao_mcp_config
from .mcp_client import create_http_mcp_clients

logger = logging.getLogger(__name__)


def _mask_email(email: str) -> str:
    local, separator, domain = email.partition("@")
    if not separator:
        return "[invalid-email]"
    return f"{local[:1]}***@{domain}"


def extract_merger(result: dict[str, Any]) -> tuple[str, str]:
    """从 MR 结果的常见字段中读取合并人；字段缺失时返回空值。"""
    metadata = result.get("metadata") or {}
    merger = result.get("merger") or result.get("merged_by") or metadata.get("merger") or {}
    if isinstance(merger, str):
        return merger, ""
    if not isinstance(merger, dict):
        return "", ""
    return str(merger.get("open_id") or merger.get("openId") or merger.get("id") or ""), str(merger.get("name") or "")


def fetch_mr_metadata(
    repository_id: str,
    local_id: str,
    organization_id: str,
) -> dict[str, Any]:
    """读取 MR 基础信息，用于飞书通知，不修改 MR。"""
    client = create_http_mcp_clients([_get_yunxiao_mcp_config()])[0]
    try:
        response = client.call_tool(
            "get_change_request",
            {
                "organizationId": organization_id,
                "repositoryId": repository_id,
                "localId": local_id,
            },
        )
    finally:
        client.close()
    for item in response.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            payload = json.loads(item.get("text") or "{}")
            if isinstance(payload, dict):
                return payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return {}


def _merge_suggestion(summary: str) -> str:
    """Derive the Feishu merge suggestion from the review severity counts."""
    counts_line = re.search(r"问题统计[：:]\s*([^\n]+)", summary, flags=re.IGNORECASE)
    if counts_line:
        counts = {
            severity.lower(): int(match.group(1))
            for severity in ("Critical", "High", "Medium", "Low", "Info")
            if (match := re.search(
                rf"\b{severity}\s*[：:]?\s*(\d+)",
                counts_line.group(1),
                flags=re.IGNORECASE,
            ))
        }
        if "critical" in counts and "high" in counts and "medium" in counts:
            if counts["critical"] > 0 or counts["high"] > 0:
                return "❌ 不允许合并"
            if counts["medium"] > 0:
                return "⚠️ 需要人工复查"
            return "✅ 建议合并"
    return "⚠️ 需要人工复查"


def _notification_fields(result: dict[str, Any], mr_url: str) -> dict[str, str]:
    metadata = result.get("mr_metadata") or {}
    author = metadata.get("author") if isinstance(metadata.get("author"), dict) else {}
    return {
        "title": str(metadata.get("title") or "（无标题）"),
        "author_name": str(author.get("name") or author.get("username") or "（未知）"),
        "author_username": str(author.get("username") or ""),
        "author_email": str(author.get("email") or ""),
        "source_branch": str(metadata.get("sourceBranch") or "（未知）"),
        "target_branch": str(metadata.get("targetBranch") or "（未知）"),
        "mr_url": mr_url,
        "suggestion": _merge_suggestion(str(result.get("summary") or "")),
        "detail_text": (
            "📄 详细审查结果已提交到 MR 评论中。"
            if (result.get("metadata") or {}).get("auto_comment", True)
            else "📄 详细审查结果请查看本次 CLI 输出。"
        ),
    }


def format_review_result(result: dict[str, Any], *, mr_url: str, merger_name: str = "") -> str:
    fields = _notification_fields(result, mr_url)
    return "\n".join([
        "📋 代码审查完成",
        "━━━━━━━━━━━━━━━━",
        "",
        f"📌 标题: {fields['title']}",
        f"👤 作者: {fields['author_name']}",
        f"📧 邮箱: {fields['author_email'] or '（未知）'}",
        f"🌿 分支: {fields['source_branch']} → {fields['target_branch']}",
        f"🔗 链接: {fields['mr_url']}",
        "",
        "━━━━━━━━━━━━━━━━",
        f"🎯 合并建议: {fields['suggestion']}",
        "━━━━━━━━━━━━━━━━",
        "",
        fields["detail_text"],
    ])


def build_review_content_rows(
    result: dict[str, Any],
    mr_url: str,
    author_open_id: str,
) -> list[list[dict[str, Any]]]:
    """构造与通知截图一致的飞书富文本行。"""
    fields = _notification_fields(result, mr_url)
    author_row: list[dict[str, Any]] = [{"tag": "text", "text": "👤 作者: "}]
    if author_open_id:
        author_row.append({"tag": "at", "user_id": author_open_id})
    else:
        author_row.append({"tag": "text", "text": fields["author_name"]})
    rows: list[list[dict[str, Any]]] = [
        [{"tag": "text", "text": "━━━━━━━━━━━━━━━━"}],
        [{"tag": "text", "text": f"📌 标题: {fields['title']}"}],
        author_row,
        [{"tag": "text", "text": f"📧 邮箱: {fields['author_email'] or '（未知）'}"}],
        [{"tag": "text", "text": f"🌿 分支: {fields['source_branch']} → {fields['target_branch']}"}],
        [{"tag": "text", "text": "🔗 链接: "}, {"tag": "a", "text": mr_url, "href": mr_url}],
        [{"tag": "text", "text": "━━━━━━━━━━━━━━━━"}],
        [{"tag": "text", "text": f"🎯 合并建议: {fields['suggestion']}"}],
        [{"tag": "text", "text": "━━━━━━━━━━━━━━━━"}],
        [{"tag": "text", "text": fields["detail_text"]}],
    ]
    return rows


def publish_review_result(
    *,
    task_id: int,
    result: dict[str, Any],
    mr_url: str,
    store: Any,
    lark_client: Any,
    chat_id: str,
) -> str:
    """把已有 Review 结果幂等发送到飞书并回写任务状态。"""
    fields = _notification_fields(result, mr_url)
    author_open_id = ""
    try:
        author_open_id = store.find_feishu_user_id_by_name(fields["author_name"])
    except Exception as exc:
        logger.warning(
            "从 feishu_user 查询作者失败: author=%s error=%s",
            fields["author_name"],
            exc,
        )
    if not author_open_id and fields["author_email"]:
        try:
            author_open_id = lark_client.get_user_open_id_by_email(fields["author_email"])
        except Exception as exc:
            logger.warning(
                "通过邮箱解析飞书用户失败: email=%s error=%s",
                _mask_email(fields["author_email"]),
                exc,
            )
    text = format_review_result(result, mr_url=mr_url)
    store.update_result(
        task_id,
        status="reviewed",
        review_result=result,
        review_text=text,
        merger_open_id=author_open_id,
        merger_name=fields["author_name"],
        mr_url=mr_url,
    )
    idempotency_key = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"lark-code-review-task:{task_id}")
    )
    store.update_result(task_id, status="sending")
    message_id = lark_client.send_review_message(
        chat_id,
        text,
        idempotency_key,
        author_open_id,
        fields["author_name"],
        build_review_content_rows(result, mr_url, author_open_id),
    )
    store.update_result(
        task_id,
        status="sent",
        review_message_id=message_id,
    )
    return message_id


async def execute_review_task(*, task_id: int, repository_id: str, local_id: str, organization_id: str, mr_url: str, store: Any, lark_client: Any, chat_id: str) -> dict[str, Any]:
    """执行一次群触发的 Review，写入一条 MR 评论后回传飞书。"""
    store.update_result(task_id, status="running")
    try:
        result = await CodeReviewAgent().review_yunxiao_mr(
            repository_id=repository_id,
            local_id=local_id,
            organization_id=organization_id,
            # 用户从固定飞书群触发的 Review 必须把完整报告写回 MR；审查器会
            # 约束为唯一一条 GLOBAL_COMMENT，避免一轮审查产生重复评论。
            auto_comment=True,
        )
        if result.get("is_error"):
            raise RuntimeError(str(result.get("summary") or "Review 执行失败"))
        if not result.get("mr_metadata"):
            result["mr_metadata"] = fetch_mr_metadata(
                repository_id,
                local_id,
                organization_id,
            )
        publish_review_result(
            task_id=task_id,
            result=result,
            mr_url=mr_url,
            store=store,
            lark_client=lark_client,
            chat_id=chat_id,
        )
        return result
    except Exception as exc:
        try:
            store.update_result(task_id, status="failed", error_message=str(exc))
        except Exception:
            logger.exception("Review 失败状态回写失败: task_id=%s", task_id)
        raise
