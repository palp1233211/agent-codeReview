"""飞书机器人长连接（WebSocket 模式）：接收消息事件 -> 转发给 Dify -> 回传 answer。"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque

import lark_oapi as lark
import pymysql
import requests
from lark_oapi.event.context import EventContext

from ..dify.client import DifyClient
from ..dify.intent import parse_code_question
from ..knowledge.code_qa import CodeQADrafter
from ..knowledge.code_qa_service import CodeQaService
from ..knowledge.commands import KbCommands
from ..knowledge.docs_repo import DocsRepo
from ..knowledge.factory import build_kb_commands
from ..knowledge.gap_recorder import render_answer
from ..storage.conversation_store import ConversationStore
from ..storage.knowledge_gap_store import KnowledgeGapStore
from .client import LarkClient

logger = logging.getLogger(__name__)

_user_name_cache: dict[str, str] = {}
_user_name_cache_lock = threading.Lock()


class FilteringEventDispatcherHandler:
    """只把白名单事件交给 SDK dispatcher，其他事件静默确认。"""

    def __init__(self, handler: lark.EventDispatcherHandler, allowed_event_keys: set[str]) -> None:
        self._handler = handler
        self._allowed_event_keys = allowed_event_keys

    def _do_without_validation(self, payload: bytes):
        # WS 模式下 SDK 会直接调用这个私有入口；先解密并读取事件类型，
        # 否则未注册事件会在 SDK 内部打印 ``processor not found``。
        plaintext = self._handler._decrypt(payload)
        context = lark.JSON.unmarshal(plaintext, EventContext)
        if context.schema:
            event_key = f"p2.{context.header.event_type}"
        else:
            event_key = f"p1.{context.event.get('type')}"

        if event_key not in self._allowed_event_keys:
            logger.debug("忽略未启用的飞书事件: %s", event_key)
            return None

        return self._handler._do_without_validation(payload)


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


def _bot_is_mentioned(mentions: list | None, bot_open_id: str | None) -> bool:
    """判断消息里 @ 的是不是机器人自己（而不是群里其他成员）。"""
    if not bot_open_id:
        return False
    for mention in mentions or []:
        mention_id = getattr(mention, "id", None)
        if getattr(mention_id, "open_id", None) == bot_open_id:
            return True
    return False


def _get_cached_user_name(lark_client: LarkClient, user_id: str) -> str:
    with _user_name_cache_lock:
        if user_id in _user_name_cache:
            return _user_name_cache[user_id]
    try:
        user_name = lark_client.get_user_name(user_id)
    except (requests.RequestException, RuntimeError):
        logger.exception("获取飞书用户姓名失败: user_id=%s", user_id)
        return ""
    with _user_name_cache_lock:
        _user_name_cache[user_id] = user_name
    return user_name


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


def _send_answer(lark_client: LarkClient, chat_id: str, answer: str) -> None:
    """优先发 Markdown 卡片（加粗、行内代码、表格才能渲染）；卡片发不出去就降级为纯文本。

    降级的情况：飞书接口拒绝（RuntimeError）、卡片超过请求体上限（ValueError）。
    纯文本也失败时，RuntimeError 交给调用方记日志。
    """
    try:
        lark_client.send_markdown_message(chat_id, answer)
        return
    except (RuntimeError, ValueError):
        logger.warning("Markdown 卡片发送失败，降级为纯文本: chat_id=%s", chat_id, exc_info=True)
    lark_client.send_text_message(chat_id, answer)


_CODE_QA_DISABLED = "代码问答暂未开通，这个问题我先回答不了。"
_CODE_USAGE = "用法：`/code <问题>`，例如 `/code 包裹丢失处罚的查询 SQL 是什么`。"
_CODE_COMMAND_RE = re.compile(r"^/code(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)


def _log_conversation(
    conversation_store: ConversationStore,
    *,
    user_id: str,
    user_name: str,
    chat_id: str,
    message_id: str,
    conversation_id: str,
    question: str,
    answer: str,
) -> None:
    try:
        conversation_store.log(
            user_id=user_id,
            user_name=user_name,
            chat_id=chat_id,
            message_id=message_id,
            conversation_id=conversation_id,
            question=question,
            answer=answer,
        )
    except pymysql.MySQLError:
        logger.exception("记录对话到数据库失败: message_id=%s", message_id)


def _hand_to_code_qa(
    code_qa: CodeQaService | None,
    lark_client: LarkClient,
    conversation_store: ConversationStore,
    *,
    question: str,
    original_text: str,
    conversation_id: str,
    user_id: str,
    user_name: str,
    chat_id: str,
    message_id: str,
) -> None:
    """把读代码才能答的问题交给代码问答服务；对话日志等它出结果后再记（记最终答案）。"""
    if code_qa is None:
        _send_answer_safely(lark_client, chat_id, _CODE_QA_DISABLED)
        _log_conversation(
            conversation_store, user_id=user_id, user_name=user_name, chat_id=chat_id,
            message_id=message_id, conversation_id=conversation_id,
            question=original_text, answer=_CODE_QA_DISABLED,
        )
        return

    code_qa.submit(
        question,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
        on_done=lambda final: _log_conversation(
            conversation_store, user_id=user_id, user_name=user_name, chat_id=chat_id,
            message_id=message_id, conversation_id=conversation_id,
            question=original_text, answer=final,
        ),
    )


def _send_answer_safely(lark_client: LarkClient, chat_id: str, answer: str) -> None:
    try:
        _send_answer(lark_client, chat_id, answer)
    except RuntimeError:
        logger.exception("回复飞书消息失败: chat_id=%s", chat_id)


def _reply_via_dify(
    lark_client: LarkClient,
    dify_client: DifyClient,
    conversation_store: ConversationStore,
    gap_store: KnowledgeGapStore,
    user_id: str,
    chat_id: str,
    message_id: str,
    text: str,
    user_name: str = "",
    code_qa: CodeQaService | None = None,
) -> None:
    """调用 Dify 拿 answer、回传飞书、记录这轮问答；跑在独立线程里，不阻塞事件回调。"""
    try:
        reply = dify_client.chat(user_id, text)
    except (requests.RequestException, RuntimeError):
        logger.exception("调用 Dify 失败: user_id=%s", user_id)
        return

    # Chatflow 判定「得读代码才能答」时返回 code_question 信封：转给代码问答，不走知识盲区
    code_question = parse_code_question(reply.answer)
    if code_question is not None:
        _hand_to_code_qa(
            code_qa, lark_client, conversation_store,
            question=code_question, original_text=text,
            conversation_id=reply.conversation_id,
            user_id=user_id, user_name=user_name, chat_id=chat_id, message_id=message_id,
        )
        return

    # Dify 命中不了知识库时返回的是意图信封而非人话，这里换成文案并记录知识盲区
    answer = render_answer(
        reply.answer,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
        gap_store=gap_store,
    )

    _send_answer_safely(lark_client, chat_id, answer)
    _log_conversation(
        conversation_store, user_id=user_id, user_name=user_name, chat_id=chat_id,
        message_id=message_id, conversation_id=reply.conversation_id,
        question=text, answer=answer,
    )


def _process_message(
    lark_client: LarkClient,
    dify_client: DifyClient,
    conversation_store: ConversationStore,
    gap_store: KnowledgeGapStore,
    kb_commands: KbCommands | None,
    user_id: str,
    chat_id: str,
    message_id: str,
    text: str,
    user_name: str = "",
    code_qa: CodeQaService | None = None,
) -> None:
    """先看是不是 /kb、/code 指令，不是才转发 Dify。跑在独立线程里。

    指令必须在转发之前拦截，否则会被当成普通提问送进 Chatflow。
    """
    if kb_commands is not None:
        reply = kb_commands.handle(text, chat_id=chat_id, user_id=user_id)
        if reply is not None:
            try:
                lark_client.send_text_message(chat_id, reply)
            except RuntimeError:
                logger.exception("回复 /kb 指令失败: chat_id=%s", chat_id)
            return

    code_command = _CODE_COMMAND_RE.match(text.strip())
    if code_command is not None:
        _handle_code_command(
            code_command.group(1), code_qa, lark_client, conversation_store,
            original_text=text, user_id=user_id, user_name=user_name,
            chat_id=chat_id, message_id=message_id,
        )
        return

    _reply_via_dify(
        lark_client,
        dify_client,
        conversation_store,
        gap_store,
        user_id,
        chat_id,
        message_id,
        text,
        user_name,
        code_qa,
    )


def _handle_code_command(
    question: str | None,
    code_qa: CodeQaService | None,
    lark_client: LarkClient,
    conversation_store: ConversationStore,
    *,
    original_text: str,
    user_id: str,
    user_name: str,
    chat_id: str,
    message_id: str,
) -> None:
    """`/code <问题>`：绕过 Dify 分类，直接读代码回答（分类误判时的兜底，也方便排查）。"""
    question = (question or "").strip()
    if not question:
        _send_text_safely(lark_client, chat_id, _CODE_USAGE)
        return
    if code_qa is None:
        _send_text_safely(lark_client, chat_id, _CODE_QA_DISABLED)
        return
    _hand_to_code_qa(
        code_qa, lark_client, conversation_store,
        question=question, original_text=original_text, conversation_id="",
        user_id=user_id, user_name=user_name, chat_id=chat_id, message_id=message_id,
    )


def _send_text_safely(lark_client: LarkClient, chat_id: str, text: str) -> None:
    try:
        lark_client.send_text_message(chat_id, text)
    except RuntimeError:
        logger.exception("回复飞书消息失败: chat_id=%s", chat_id)


def build_message_receive_handler(
    lark_client: LarkClient,
    dify_client: DifyClient,
    conversation_store: ConversationStore,
    gap_store: KnowledgeGapStore,
    kb_commands: KbCommands | None = None,
    bot_open_id: str | None = None,
    code_qa: CodeQaService | None = None,
):
    """构造 im.message.receive_v1 事件回调：文本消息 -> Dify -> 回传 answer 到同一会话。

    回调本身只做去重判断和转发到后台线程，立即返回，避免阻塞长连接的事件循环。
    群聊里只有 @机器人 才处理，避免把群里所有闲聊都转发给 Dify；私聊不受此限制。
    """
    recent_message_ids = _RecentMessageIds()

    def on_message_receive(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        message = data.event.message
        logger.info(
            "收到消息: message_id=%s chat_id=%s chat_type=%s msg_type=%s content=%s",
            message.message_id,
            message.chat_id,
            message.chat_type,
            message.message_type,
            message.content,
        )

        if not recent_message_ids.add_if_new(message.message_id):
            logger.info("重复事件（飞书重推），跳过: message_id=%s", message.message_id)
            return

        if message.message_type != "text":
            logger.info("非文本消息，跳过: msg_type=%s", message.message_type)
            return

        if message.chat_type == "group" and not _bot_is_mentioned(message.mentions, bot_open_id):
            logger.info("群聊消息未 @机器人，跳过: message_id=%s", message.message_id)
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
            target=_process_message,
            args=(
                lark_client,
                dify_client,
                conversation_store,
                gap_store,
                kb_commands,
                user_id,
                message.chat_id,
                message.message_id,
                text,
                _get_cached_user_name(lark_client, user_id),
                code_qa,
            ),
            daemon=True,
        ).start()

    return on_message_receive


def on_message_read(data: lark.im.v1.P2ImMessageMessageReadV1) -> None:
    """消息已读回执：飞书控制台若订阅了该事件就会推送，这里只记录，不处理。"""
    logger.debug("消息已读事件: %s", lark.JSON.marshal(data))


def _build_code_qa(lark_client: LarkClient, gap_store: KnowledgeGapStore) -> CodeQaService | None:
    """装配代码问答；知识草稿依赖知识库目录，没配就只回答、不沉淀。"""
    try:
        drafter = CodeQADrafter(gap_store=gap_store, docs_repo=DocsRepo.from_env())
    except KeyError:
        logger.warning("未配置 KNOWLEDGE_DOCS_DIR，代码问答的答案不会沉淀成知识草稿。")
        drafter = None

    try:
        service = CodeQaService.from_env(
            send=lambda chat_id, text: _send_answer(lark_client, chat_id, text),
            drafter=drafter,
        )
    except (KeyError, ValueError) as exc:
        logger.warning("代码问答未启用（%s），/code 与代码逻辑类问题不可用。", exc)
        return None

    available = service.qa.available_projects
    if not available:
        logger.warning("代码问答已启用，但没有位于 master 分支的开放项目，提问都会被告知暂不回答。")
    else:
        logger.info("代码问答已启用：项目=%s 版本=%s", available, service.qa.versions())
    return service


def run_ws_bot() -> int:
    """启动飞书长连接，阻塞直到进程被中断。返回值可直接用作进程退出码。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        # Dify 问答只能使用 Dify 机器人自己的应用凭证；Code Review 机器人由
        # 独立进程和 LARK_CODE_REVIEW_APP_* 凭证处理。
        lark_client = LarkClient.from_env("LARK_DIFY_")
        dify_client = DifyClient.from_env()
        conversation_store = ConversationStore.from_env()
        gap_store = KnowledgeGapStore.from_env()
    except KeyError as exc:
        logger.error("缺少必填环境变量: %s，请参考 .env.example 配置后再启动。", exc)
        return 1
    except pymysql.MySQLError as exc:
        logger.error("连接数据库失败，请检查 DB_* 环境变量: %s", exc)
        return 1

    # 知识补全流水线要额外的配置（Dify 知识库密钥、代码库路径）。没配好只是少了
    # /kb 指令，不该拖着整个问答机器人起不来，所以这里降级而不是退出。
    try:
        kb_commands = build_kb_commands(lark_client=lark_client, gap_store=gap_store)
    except (KeyError, ValueError) as exc:
        logger.warning("知识补全流水线未启用（%s），/kb 指令不可用。", exc)
        kb_commands = None

    # 代码问答同理：缺配置（项目目录、模型）只是少了这个能力，不该拖垮问答机器人
    code_qa = _build_code_qa(lark_client, gap_store)

    # 群聊过滤依赖机器人自己的 open_id 来判断"是不是 @ 了我"；取不到就没法安全放行
    # 群聊消息，只能降级为仅处理私聊（而不是退回到"处理群里所有消息"的旧行为）。
    try:
        bot_open_id = lark_client.get_bot_open_id()
    except (requests.RequestException, RuntimeError) as exc:
        logger.warning("获取机器人 open_id 失败（%s），群聊消息将被忽略，仅私聊可用。", exc)
        bot_open_id = None

    registered_event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(
            build_message_receive_handler(
                lark_client,
                dify_client,
                conversation_store,
                gap_store,
                kb_commands,
                bot_open_id,
                code_qa,
            )
        )
        .register_p2_im_message_message_read_v1(on_message_read)
        .build()
    )
    event_handler = FilteringEventDispatcherHandler(
        registered_event_handler,
        {
            "p2.im.message.receive_v1",
            "p2.im.message.message_read_v1",
        },
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
