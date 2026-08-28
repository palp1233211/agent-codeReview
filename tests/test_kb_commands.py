"""飞书 /kb 指令解析与分发测试。"""
from unittest.mock import MagicMock

from src.knowledge.commands import KbCommands, parse_command
from src.storage.knowledge_gap_store import KnowledgeGap


def _gap(gap_id: int = 12, status: str = "pending") -> KnowledgeGap:
    return KnowledgeGap(
        id=gap_id,
        user_id="ou_1",
        chat_id="oc_1",
        original_query="处罚人是什么字段",
        task_name="",
        status=status,
        doc_path="",
        draft_summary="",
        fail_reason="",
        content_sha256="",
        dify_document_id="",
    )


# ---------- parse_command ----------


def test_parses_list():
    cmd = parse_command("/kb list")
    assert cmd.action == "list" and cmd.gap_id is None


def test_parses_action_with_id():
    for action in ("fill", "approve", "sync", "delete"):
        cmd = parse_command(f"/kb {action} 12")
        assert cmd.action == action and cmd.gap_id == 12


def test_is_case_and_whitespace_tolerant():
    cmd = parse_command("  /KB   Fill   12  ")
    assert cmd.action == "fill" and cmd.gap_id == 12


def test_bare_kb_is_help():
    assert parse_command("/kb").action == "help"


def test_unknown_action_falls_back_to_help():
    assert parse_command("/kb frobnicate 3").action == "help"


def test_missing_id_is_parsed_but_id_is_none():
    cmd = parse_command("/kb fill")
    assert cmd.action == "fill" and cmd.gap_id is None


def test_non_numeric_id_is_rejected():
    cmd = parse_command("/kb fill abc")
    assert cmd.action == "fill" and cmd.gap_id is None


def test_fill_id_with_trailing_hint_text():
    """FBI_REPO_PATH 下可能挂了多个项目，编号后面可以再跟一段项目/方法线索。"""
    cmd = parse_command("/kb fill 12 fbi 项目 PunishBLL::getList")
    assert cmd.action == "fill" and cmd.gap_id == 12
    assert cmd.hint == "fbi 项目 PunishBLL::getList"


def test_fill_without_hint_has_empty_hint():
    assert parse_command("/kb fill 12").hint == ""


def test_other_actions_ignore_trailing_text_as_hint():
    """approve/sync/delete 目前不消费额外文本，但也不该因为多打了字就解析失败。"""
    cmd = parse_command("/kb approve 12 无关文字")
    assert cmd.action == "approve" and cmd.gap_id == 12


def test_returns_none_for_ordinary_message():
    """普通提问必须放行到 Dify，不能被指令层吃掉。"""
    assert parse_command("客诉平台的处罚金额怎么算") is None
    assert parse_command("/help") is None
    assert parse_command("") is None


# ---------- KbCommands ----------


def _commands(gap_store=None, fill_gap=None, sync_gap=None):
    return KbCommands(
        gap_store=gap_store or MagicMock(),
        fill_gap=fill_gap or MagicMock(),
        sync_gap=sync_gap or MagicMock(),
        notify=MagicMock(),
        executor=lambda fn: fn(),  # 测试里同步执行，不起线程
    )


def test_handle_returns_none_for_ordinary_message():
    assert _commands().handle("你好", chat_id="oc_1", user_id="ou_1") is None


def test_list_shows_pending_gaps():
    store = MagicMock()
    store.list_by_status.return_value = [_gap(12)]

    reply = _commands(gap_store=store).handle("/kb list", chat_id="oc_1", user_id="ou_1")

    assert "12" in reply and "处罚人是什么字段" in reply


def test_list_reports_empty_queue():
    store = MagicMock()
    store.list_by_status.return_value = []

    reply = _commands(gap_store=store).handle("/kb list", chat_id="oc_1", user_id="ou_1")

    assert reply.strip()


def test_fill_claims_row_before_running():
    store = MagicMock()
    store.get.return_value = _gap(12)
    store.try_claim.return_value = True
    fill = MagicMock()

    reply = _commands(gap_store=store, fill_gap=fill).handle(
        "/kb fill 12", chat_id="oc_1", user_id="ou_9"
    )

    store.try_claim.assert_called_once_with(
        12, from_status="pending", to_status="filling", operator_id="ou_9"
    )
    fill.assert_called_once()
    assert "12" in reply


def test_fill_passes_hint_through_to_fill_gap():
    store = MagicMock()
    store.get.return_value = _gap(12)
    store.try_claim.return_value = True
    fill = MagicMock()

    reply = _commands(gap_store=store, fill_gap=fill).handle(
        "/kb fill 12 fbi 项目 PunishBLL::getList", chat_id="oc_1", user_id="ou_9"
    )

    assert fill.call_args.kwargs["hint"] == "fbi 项目 PunishBLL::getList"
    assert "PunishBLL::getList" in reply


def test_fill_without_hint_calls_fill_gap_with_empty_hint():
    store = MagicMock()
    store.get.return_value = _gap(12)
    store.try_claim.return_value = True
    fill = MagicMock()

    _commands(gap_store=store, fill_gap=fill).handle(
        "/kb fill 12", chat_id="oc_1", user_id="ou_9"
    )

    assert fill.call_args.kwargs["hint"] == ""


def test_fill_refuses_when_row_already_claimed():
    """并发抢占失败时不能重复跑 agent。"""
    store = MagicMock()
    store.get.return_value = _gap(12)
    store.try_claim.return_value = False
    fill = MagicMock()

    reply = _commands(gap_store=store, fill_gap=fill).handle(
        "/kb fill 12", chat_id="oc_1", user_id="ou_9"
    )

    fill.assert_not_called()
    assert "处理" in reply or "已" in reply


def test_fill_reports_missing_gap():
    store = MagicMock()
    store.get.return_value = None
    fill = MagicMock()

    reply = _commands(gap_store=store, fill_gap=fill).handle(
        "/kb fill 999", chat_id="oc_1", user_id="ou_9"
    )

    fill.assert_not_called()
    assert "999" in reply


def test_fill_without_id_replies_usage():
    fill = MagicMock()

    reply = _commands(fill_gap=fill).handle("/kb fill", chat_id="oc_1", user_id="ou_1")

    fill.assert_not_called()
    assert "/kb fill" in reply


def test_approve_requires_drafted_status():
    """只有起草完成的条目才能推 Dify，防止把空文档推上线。"""
    store = MagicMock()
    store.get.return_value = _gap(12, status="pending")
    store.try_claim.return_value = False
    sync = MagicMock()

    reply = _commands(gap_store=store, sync_gap=sync).handle(
        "/kb approve 12", chat_id="oc_1", user_id="ou_9"
    )

    sync.assert_not_called()
    assert reply.strip()


def test_approve_syncs_when_drafted():
    store = MagicMock()
    store.get.return_value = _gap(12, status="drafted")
    store.try_claim.return_value = True
    sync = MagicMock()

    _commands(gap_store=store, sync_gap=sync).handle(
        "/kb approve 12", chat_id="oc_1", user_id="ou_9"
    )

    store.try_claim.assert_called_once_with(
        12, from_status="drafted", to_status="syncing", operator_id="ou_9"
    )
    sync.assert_called_once()


def test_help_lists_available_commands():
    reply = _commands().handle("/kb", chat_id="oc_1", user_id="ou_1")

    for action in ("list", "fill", "approve", "delete"):
        assert action in reply


# ---------- delete ----------


def test_delete_removes_row():
    store = MagicMock()
    store.get.return_value = _gap(12, status="pending")
    store.delete.return_value = True

    reply = _commands(gap_store=store).handle(
        "/kb delete 12", chat_id="oc_1", user_id="ou_9"
    )

    store.delete.assert_called_once_with(12)
    assert "12" in reply


def test_delete_reports_missing_gap():
    store = MagicMock()
    store.get.return_value = None

    reply = _commands(gap_store=store).handle(
        "/kb delete 999", chat_id="oc_1", user_id="ou_9"
    )

    store.delete.assert_not_called()
    assert "999" in reply


def test_delete_without_id_replies_usage():
    reply = _commands().handle("/kb delete", chat_id="oc_1", user_id="ou_1")

    assert "/kb delete" in reply


def test_delete_refuses_when_gap_is_being_processed():
    """行正在被后台线程处理，删掉会导致它写完时找不到行——先别让删。"""
    for status in ("filling", "syncing"):
        store = MagicMock()
        store.get.return_value = _gap(12, status=status)

        reply = _commands(gap_store=store).handle(
            "/kb delete 12", chat_id="oc_1", user_id="ou_9"
        )

        store.delete.assert_not_called()
        assert "12" in reply


def test_delete_reports_when_row_already_gone():
    """get() 和 delete() 之间行被并发删掉——不能报"删除成功"糊弄用户。"""
    store = MagicMock()
    store.get.return_value = _gap(12, status="pending")
    store.delete.return_value = False

    reply = _commands(gap_store=store).handle(
        "/kb delete 12", chat_id="oc_1", user_id="ou_9"
    )

    assert "12" in reply


# ---------- 落库失败时的兜底 ----------


def test_fill_failure_still_notifies_when_mark_failed_also_raises():
    """行已经不存在时，mark_failed 同样会抛 GapRowMissing。
    错误处理自己再炸一次的话，用户什么通知都收不到，任务像凭空消失。"""
    store = MagicMock()
    store.get.return_value = _gap(12)
    store.try_claim.return_value = True
    store.mark_failed.side_effect = RuntimeError("行不存在")
    notify = MagicMock()

    commands = KbCommands(
        gap_store=store,
        fill_gap=MagicMock(side_effect=RuntimeError("补全炸了")),
        sync_gap=MagicMock(),
        notify=notify,
        executor=lambda fn: fn(),
    )
    commands.handle("/kb fill 12", chat_id="oc_1", user_id="ou_9")

    notify.assert_called_once()
    assert "12" in notify.call_args[0][1]


def test_sync_failure_still_notifies_when_mark_failed_also_raises():
    store = MagicMock()
    store.get.return_value = _gap(12, status="drafted")
    store.try_claim.return_value = True
    store.mark_failed.side_effect = RuntimeError("行不存在")
    notify = MagicMock()

    commands = KbCommands(
        gap_store=store,
        fill_gap=MagicMock(),
        sync_gap=MagicMock(side_effect=RuntimeError("推送炸了")),
        notify=notify,
        executor=lambda fn: fn(),
    )
    commands.handle("/kb approve 12", chat_id="oc_1", user_id="ou_9")

    notify.assert_called_once()
    assert "12" in notify.call_args[0][1]
