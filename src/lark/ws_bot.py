"""飞书机器人长连接（WebSocket 模式）：接收消息事件 -> 转发给 Dify -> 回传 answer。"""
from __future__ import annotations

import json
import logging
import threading
from collections import deque

import lark_oapi as lark
import pymysql
import requests

from ..dify.client import DifyClient
from ..storage.conversation_store import ConversationStore
from .client import LarkClient

logger = logging.getLogger(__name__)


def extract_text(message_content: str) -> str | None:
    """从消息 content(JSON 字符串) 中提取纯文本；非文本消息或解析失败返回 None。"""
    try:
        payload = json.loads(message_content)
    except (TypeError, ValueError):
        logger.warning("消息 content 不是合法 JSON: %r", message_content)
        return None

    text = payload.get("text")
    return text.strip() if isinstance(text, str) else None


def strip_mentions(text: str, mentions: list | None) -> str:
    """去掉 @机器人 等 mention 占位符（如 "@_user_1"），并压缩多余空白。"""
    for mention in mentions or []:
        key = getattr(mention, "key", None)
        if key:
            text = text.replace(key, "")
    return " ".join(text.split())


def _sender_user_id(event: lark.im.v1.P2ImMessageReceiveV1Data, fallback: str) -> str:
    """取发送者 open_id 作为 Dify 会话隔离键；取不到就退回 chat_id（群聊场景不常见）。"""
    sender_id = getattr(event.sender, "sender_id", None) if event.sender else None
    open_id = getattr(sender_id, "open_id", None) if sender_id else None
    return open_id or fallback


class _RecentMessageIds:
    """线程安全的最近处理过 message_id 缓存。

    飞书长连接的事件回调必须尽快返回并发 ACK；如果回调里同步做慢请求（比如等 Dify
    响应），ACK 会被延迟，飞书服务端会判定超时并重推同一个事件，导致重复处理。
    这里用 message_id 做幂等去重，兜底任何原因导致的重复投递。
    """

    def __init__(self, max_size: int = 1000):
        self._max_size = max_size
        self._ids: deque[str] = deque()
        self._id_set: set[str] = set()
        self._lock = threading.Lock()

    def add_if_new(self, message_id: str) -> bool:
        """返回 True 表示第一次见到该 message_id（应处理）；False 表示重复（应跳过）。"""
        with self._lock:
            if message_id in self._id_set:
                return False
            if len(self._ids) >= self._max_size:
                oldest = self._ids.popleft()
                self._id_set.discard(oldest)
            self._ids.append(message_id)
            self._id_set.add(message_id)
            return True


def _reply_via_dify(
    lark_client: LarkClient,
    dify_client: DifyClient,
    conversation_store: ConversationStore,
    user_id: str,
    chat_id: str,
    message_id: str,
    text: str,
) -> None:
    """调用 Dify 拿 answer、回传飞书、记录这轮问答；跑在独立线程里，不阻塞事件回调。"""
    try:
        reply = dify_client.chat(user_id, text)
    except (requests.RequestException, RuntimeError):
        logger.exception("调用 Dify 失败: user_id=%s", user_id)
        return

    try:
        lark_client.send_text_message(chat_id, reply.answer)
    except RuntimeError:
        logger.exception("回复飞书消息失败: chat_id=%s", chat_id)

    try:
        conversation_store.log(
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            conversation_id=reply.conversation_id,
            question=text,
            answer=reply.answer,
        )
    except pymysql.MySQLError:
        logger.exception("记录对话到数据库失败: message_id=%s", message_id)


def build_message_receive_handler(
    lark_client: LarkClient,
    dify_client: DifyClient,
    conversation_store: ConversationStore,
):
    """构造 im.message.receive_v1 事件回调：文本消息 -> Dify -> 回传 answer 到同一会话。

    回调本身只做去重判断和转发到后台线程，立即返回，避免阻塞长连接的事件循环。
    """
    recent_message_ids = _RecentMessageIds()

    def on_message_receive(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        message = data.event.message
        logger.info(
            "收到消息: message_id=%s chat_id=%s msg_type=%s content=%s",
            message.message_id,
            message.chat_id,
            message.message_type,
            message.content,
        )

        if not recent_message_ids.add_if_new(message.message_id):
            logger.info("重复事件（飞书重推），跳过: message_id=%s", message.message_id)
            return

        if message.message_type != "text":
            logger.info("非文本消息，跳过: msg_type=%s", message.message_type)
            return

        text = extract_text(message.content)
        if text is None:
            return

        text = strip_mentions(text, message.mentions)
        if not text:
            logger.info("消息去除 mention 后为空，跳过: message_id=%s", message.message_id)
            return

        user_id = _sender_user_id(data.event, fallback=message.chat_id)

        # Dify 调用是同步阻塞 HTTP 请求，这里在这条独立线程里做，事件回调立刻返回。
        threading.Thread(
            target=_reply_via_dify,
            args=(
                lark_client,
                dify_client,
                conversation_store,
                user_id,
                message.chat_id,
                message.message_id,
                text,
            ),
            daemon=True,
        ).start()

    return on_message_receive


def on_message_read(data: lark.im.v1.P2ImMessageMessageReadV1) -> None:
    """消息已读回执：飞书控制台若订阅了该事件就会推送，这里只记录，不处理。"""
    logger.debug("消息已读事件: %s", lark.JSON.marshal(data))


def run_ws_bot() -> int:
    """启动飞书长连接，阻塞直到进程被中断。返回值可直接用作进程退出码。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        lark_client = LarkClient.from_env()
        dify_client = DifyClient.from_env()
        conversation_store = ConversationStore.from_env()
    except KeyError as exc:
        logger.error("缺少必填环境变量: %s，请参考 .env.example 配置后再启动。", exc)
        return 1
    except pymysql.MySQLError as exc:
        logger.error("连接数据库失败，请检查 DB_* 环境变量: %s", exc)
        return 1

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(
            build_message_receive_handler(lark_client, dify_client, conversation_store)
        )
        .register_p2_im_message_message_read_v1(on_message_read)
        .build()
    )

    # WS client：长连接接收事件，免去公网回调地址和签名校验
    ws_client = lark.ws.Client(
        lark_client.app_id,
        lark_client.app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )

    logger.info("正在建立飞书长连接...")
    ws_client.start()
    return 0
