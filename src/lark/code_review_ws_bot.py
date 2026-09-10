"""Code Review 飞书机器人长连接入口。

该进程只使用 Code Review 飞书应用的凭证，只接收固定审查群中 @ 这个机器人且
带有合法 Yunxiao MR 链接的文本消息。它不会初始化或调用 Dify。
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading

import lark_oapi as lark
import pymysql
import requests

from ..agents.review_request import extract_mr_reference, is_review_message
from ..agents.review_feedback import (
    analyze_review_feedback,
    feedback_reply_idempotency_key,
    format_feedback_reply,
)
from ..agents.review_feedback_sync import sync_review_feedback_to_yunxiao
from ..agents.review_result import execute_review_task
from ..storage.code_review_store import CodeReviewStore
from .client import LarkClient
from .ws_bot import (
    FilteringEventDispatcherHandler,
    _RecentMessageIds,
    extract_text,
)

logger = logging.getLogger(__name__)

_DEFAULT_REVIEW_CHAT_ID = "oc_7ff28089a62b19bf781ca9fa2ac3275d"
_DEFAULT_ORGANIZATION_ID = "5ea86562f89c9700014a671f"


def _run_review_task(
    *,
    task_id: int,
    repository_id: str,
    local_id: str,
    mr_url: str,
    organization_id: str,
    store: CodeReviewStore,
    lark_client: LarkClient,
    chat_id: str,
) -> None:
    """在线程中执行慢速 MR 审查，保证 WebSocket 回调可立即 ACK。"""
    try:
        asyncio.run(
            execute_review_task(
                task_id=task_id,
                repository_id=repository_id,
                local_id=local_id,
                organization_id=organization_id,
                mr_url=mr_url,
                store=store,
                lark_client=lark_client,
                chat_id=chat_id,
            )
        )
    except Exception:
        # execute_review_task 已尝试回写 failed；这里只保留进程日志，不能将
        # 异常抛回飞书 SDK 的事件回调。
        logger.exception("Code Review 任务执行失败: task_id=%s", task_id)


def _sender_open_id(data: lark.im.v1.P2ImMessageReceiveV1) -> str:
    sender = getattr(data.event, "sender", None)
    sender_id = getattr(sender, "sender_id", None) if sender else None
    return str(getattr(sender_id, "open_id", "") or "")


def _run_feedback_analysis(
    *,
    feedback_id: int,
    feedback_message_id: str,
    review_text: str,
    feedback_text: str,
    organization_id: str,
    repository_id: str,
    local_id: str,
    original_comment_content: str,
    store: CodeReviewStore,
    lark_client: LarkClient,
) -> None:
    """异步分析用户引用回复，并将可用于提示词优化的误报原因落库。"""
    try:
        store.update_feedback(feedback_id, status="analyzing")
        analysis = asyncio.run(
            analyze_review_feedback(
                review_text=review_text,
                feedback_text=feedback_text,
            )
        )
        sync_error = ""
        sync_result: dict[str, object] = {}
        try:
            sync_result = sync_review_feedback_to_yunxiao(
                organization_id=organization_id,
                repository_id=repository_id,
                local_id=local_id,
                original_comment_content=original_comment_content,
                feedback_text=feedback_text,
                analysis=analysis,
            )
        except Exception as exc:
            sync_error = f"云效评论同步失败: {exc}"
            logger.exception("Code Review 反馈同步到云效失败: feedback_id=%s", feedback_id)
        analysis_raw = {
            **analysis["raw"],
            "yunxiao_sync": sync_result or {"error": sync_error},
        }
        store.update_feedback(
            feedback_id,
            status="analyzed",
            verdict=analysis["verdict"],
            reason_category=analysis["reason_category"] or None,
            reason_summary=analysis["reason_summary"] or None,
            confidence=analysis["confidence"],
            analysis_raw=analysis_raw,
            error_message=sync_error or None,
        )
        reply_text = format_feedback_reply(analysis)
        if sync_error:
            reply_text += "\n云效 MR 评论：同步失败，详见任务记录。"
        elif sync_result.get("closed_original_comment"):
            reply_text += "\n云效 MR 评论：已同步，原 Review 评论已关闭。"
        else:
            reply_text += "\n云效 MR 评论：已同步。"
        lark_client.reply_text_message(
            feedback_message_id,
            reply_text,
            feedback_reply_idempotency_key(feedback_id),
        )
        logger.info(
            "Code Review 反馈分析完成: feedback_id=%s verdict=%s",
            feedback_id,
            analysis["verdict"],
        )
    except Exception as exc:
        # AI 失败和群回复失败分别记录；已分析成功的结论始终保留。
        analysis_completed = "analysis" in locals()
        try:
            if analysis_completed:
                store.update_feedback(
                    feedback_id,
                    status="analyzed",
                    error_message=f"飞书群回复失败: {exc}",
                )
            else:
                store.update_feedback(feedback_id, status="failed", error_message=str(exc))
        except Exception:
            logger.exception("Code Review 反馈失败状态回写失败: feedback_id=%s", feedback_id)
        logger.exception("Code Review 反馈分析失败: feedback_id=%s", feedback_id)


def build_review_message_receive_handler(
    *,
    lark_client: LarkClient,
    store: CodeReviewStore,
    bot_open_id: str,
    review_chat_id: str,
    organization_id: str,
):
    """构造 Code Review 专用消息处理器。"""
    recent_message_ids = _RecentMessageIds()

    def on_message_receive(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        message = data.event.message
        # 只记录路由元数据，不记录完整消息正文；这样既能定位飞书投递/过滤问题，
        # 也不会把群聊内容泄露到容器日志。
        logger.info(
            "收到 Code Review 消息: message_id=%s chat_id=%s msg_type=%s mention_count=%s",
            message.message_id,
            message.chat_id,
            message.message_type,
            len(message.mentions or []),
        )
        if not recent_message_ids.add_if_new(message.message_id):
            logger.info("重复 Code Review 事件，跳过: message_id=%s", message.message_id)
            return
        if message.message_type != "text":
            logger.info(
                "Code Review 消息不是文本，跳过: message_id=%s msg_type=%s",
                message.message_id,
                message.message_type,
            )
            return

        if message.chat_id != review_chat_id:
            logger.info(
                "Code Review 消息不在目标群，跳过: message_id=%s chat_id=%s expected_chat_id=%s",
                message.message_id,
                message.chat_id,
                review_chat_id,
            )
            return

        text = extract_text(message.content)
        if text is None:
            logger.info("Code Review 文本解析失败，跳过: message_id=%s", message.message_id)
            return

        # 反馈入口优先于 @ 检查：用户必须直接引用机器人发出的 Review 结果，
        # 因此无需再 @ 机器人，也不能仅凭文本猜测对应哪一条 Review。
        parent_message_id = str(getattr(message, "parent_id", "") or "")
        if parent_message_id:
            try:
                review_task = store.find_review_task_for_feedback(
                    review_message_id=parent_message_id,
                    chat_id=message.chat_id,
                )
            except pymysql.MySQLError:
                logger.exception(
                    "查询被引用 Review 失败: message_id=%s parent_message_id=%s",
                    message.message_id,
                    parent_message_id,
                )
                return
            if review_task is None:
                logger.info(
                    "引用的不是 Review 结果，跳过反馈处理: message_id=%s parent_message_id=%s",
                    message.message_id,
                    parent_message_id,
                )
                return
            try:
                feedback_id = store.create_feedback(
                    review_task_id=review_task.id,
                    feedback_message_id=message.message_id,
                    parent_message_id=parent_message_id,
                    chat_id=message.chat_id,
                    sender_open_id=_sender_open_id(data),
                    feedback_text=text,
                )
            except pymysql.MySQLError:
                logger.exception("创建 Code Review 反馈失败: message_id=%s", message.message_id)
                return
            if feedback_id is None:
                logger.info("重复 Code Review 反馈，跳过: message_id=%s", message.message_id)
                return
            logger.info(
                "已创建 Code Review 反馈: feedback_id=%s review_task_id=%s message_id=%s",
                feedback_id,
                review_task.id,
                message.message_id,
            )
            threading.Thread(
                target=_run_feedback_analysis,
                kwargs={
                    "feedback_id": feedback_id,
                    "feedback_message_id": message.message_id,
                    "review_text": review_task.review_text,
                    "feedback_text": text,
                    "organization_id": organization_id,
                    "repository_id": review_task.repository_id,
                    "local_id": review_task.local_id,
                    "original_comment_content": review_task.review_comment_content,
                    "store": store,
                    "lark_client": lark_client,
                },
                daemon=True,
            ).start()
            return

        mentioned_bot = any(
            getattr(getattr(item, "id", None), "open_id", None) == bot_open_id
            for item in message.mentions or []
        )
        if not mentioned_bot:
            logger.info(
                "Code Review 消息未 @当前机器人且不是引用回复，跳过: message_id=%s",
                message.message_id,
            )
            return
        if not is_review_message(
            chat_id=message.chat_id,
            text=text,
            mentions=message.mentions,
            bot_open_id=bot_open_id,
            review_chat_id=review_chat_id,
        ):
            # 上面的群和 @ 校验均已通过，此处仅会是 MR 链接格式或主机不合法。
            logger.info("Code Review 消息未包含合法 Yunxiao MR 链接，跳过: message_id=%s", message.message_id)
            return

        reference = extract_mr_reference(text)
        # is_review_message 已验证过，这里仅作类型收窄，防止未来规则变更时误启动任务。
        if reference is None:
            return
        mr_url, repository_id, local_id = reference
        try:
            task_id = store.create_task(
                trigger_message_id=message.message_id,
                chat_id=message.chat_id,
                mr_url=mr_url,
                repository_id=repository_id,
                local_id=local_id,
            )
        except pymysql.MySQLError:
            logger.exception("创建 Code Review 任务失败: message_id=%s", message.message_id)
            return
        if task_id is None:
            logger.info("重复 Code Review 任务，跳过: message_id=%s", message.message_id)
            return

        logger.info(
            "已创建 Code Review 任务: task_id=%s message_id=%s mr=%s",
            task_id,
            message.message_id,
            mr_url,
        )
        threading.Thread(
            target=_run_review_task,
            kwargs={
                "task_id": task_id,
                "repository_id": repository_id,
                "local_id": local_id,
                "mr_url": mr_url,
                "organization_id": organization_id,
                "store": store,
                "lark_client": lark_client,
                "chat_id": message.chat_id,
            },
            daemon=True,
        ).start()

    return on_message_receive


def run_code_review_ws_bot() -> int:
    """启动 Code Review 专用飞书长连接。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    review_chat_id = os.getenv("LARK_CODE_REVIEW_CHAT_ID", _DEFAULT_REVIEW_CHAT_ID)
    organization_id = os.getenv("YUNXIAO_ORG_ID", _DEFAULT_ORGANIZATION_ID)
    try:
        lark_client = LarkClient.from_env("LARK_CODE_REVIEW_")
        store = CodeReviewStore.from_env()
        bot_open_id = lark_client.get_bot_open_id()
    except KeyError as exc:
        logger.error("缺少 Code Review 机器人配置: %s", exc)
        return 1
    except (pymysql.MySQLError, requests.RequestException, RuntimeError) as exc:
        logger.error("Code Review 机器人启动失败: %s", exc)
        return 1

    registered_event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(
            build_review_message_receive_handler(
                lark_client=lark_client,
                store=store,
                bot_open_id=bot_open_id,
                review_chat_id=review_chat_id,
                organization_id=organization_id,
            )
        )
        .build()
    )
    event_handler = FilteringEventDispatcherHandler(
        registered_event_handler,
        {"p2.im.message.receive_v1"},
    )
    ws_client = lark.ws.Client(
        lark_client.app_id,
        lark_client.app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )
    logger.info("正在建立 Code Review 飞书长连接: chat_id=%s", review_chat_id)
    ws_client.start()
    return 0
