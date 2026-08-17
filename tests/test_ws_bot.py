"""飞书长连接 Bot 消息解析与去重测试"""
from types import SimpleNamespace

from src.lark.ws_bot import _RecentMessageIds, extract_text, strip_mentions


def test_extract_text_returns_stripped_text():
    assert extract_text('{"text": "  hello  "}') == "hello"


def test_extract_text_returns_none_for_invalid_json():
    assert extract_text("not json") is None


def test_extract_text_returns_none_when_text_field_missing():
    assert extract_text('{"image_key": "img_xxx"}') is None


def test_recent_message_ids_flags_duplicate():
    cache = _RecentMessageIds()

    assert cache.add_if_new("om_1") is True
    assert cache.add_if_new("om_1") is False
    assert cache.add_if_new("om_2") is True


def test_recent_message_ids_evicts_oldest_beyond_max_size():
    cache = _RecentMessageIds(max_size=2)

    cache.add_if_new("om_1")
    cache.add_if_new("om_2")
    cache.add_if_new("om_3")  # 超出容量，应该把 om_1 挤出去

    assert cache.add_if_new("om_1") is True  # 已被淘汰，视为新消息
    assert cache.add_if_new("om_3") is False  # 仍在缓存里


def test_strip_mentions_removes_placeholder_and_trims():
    mentions = [SimpleNamespace(key="@_user_1")]
    assert strip_mentions("@_user_1  帮我查一下天气", mentions) == "帮我查一下天气"


def test_strip_mentions_handles_no_mentions():
    assert strip_mentions("  普通消息  ", None) == "普通消息"


def test_strip_mentions_collapses_internal_whitespace():
    mentions = [SimpleNamespace(key="@_user_1"), SimpleNamespace(key="@_user_2")]
    assert strip_mentions("@_user_1 你好 @_user_2 在吗", mentions) == "你好 在吗"
