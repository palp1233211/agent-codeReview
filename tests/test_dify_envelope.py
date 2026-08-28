"""Chatflow 信封解析：{content, unknown} 形状。

这个形状比早期的 {intent: unknown} 好在能表达"部分答上来"——content 有内容、
unknown 也有内容时，既要把答案发给用户，又要把没覆盖的部分记进补全队列。
"""
import json

from src.dify.intent import parse_envelope

def _env(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


# ---------- {content, unknown} 形状 ----------


def test_full_answer_has_no_gap():
    env = parse_envelope(_env(content="punish_category 是 21。", unknown=""))

    assert env is not None
    assert env.content == "punish_category 是 21。"
    assert not env.has_gap


def test_pure_gap_when_content_empty():
    env = parse_envelope(_env(content="", unknown="客诉的处罚类别号是多少"))

    assert env.has_gap
    assert env.gap_query == "客诉的处罚类别号是多少"
    assert env.content == ""


def test_partial_answer_carries_both():
    """答上来一半的情况：答案要发，没答上的部分要入队。"""
    env = parse_envelope(
        _env(content="处罚大类是 21。", unknown="审核状态 state 有哪些取值")
    )

    assert env.content == "处罚大类是 21。"
    assert env.has_gap
    assert env.gap_query == "审核状态 state 有哪些取值"


def test_null_unknown_is_not_a_gap():
    assert not parse_envelope(_env(content="答案", unknown=None)).has_gap


def test_missing_unknown_key_is_not_a_gap():
    assert not parse_envelope(_env(content="答案")).has_gap


def test_unknown_as_list_is_joined():
    env = parse_envelope(_env(content="", unknown=["问题一", "问题二"]))

    assert env.has_gap
    assert "问题一" in env.gap_query and "问题二" in env.gap_query


def test_whitespace_only_unknown_is_not_a_gap():
    assert not parse_envelope(_env(content="答案", unknown="   ")).has_gap


# ---------- 兼容早期的 {intent: unknown} 形状 ----------


def test_still_recognizes_legacy_intent_envelope():
    env = parse_envelope(_env(intent="unknown", original_query="处罚人是什么字段"))

    assert env is not None
    assert env.has_gap
    assert env.gap_query == "处罚人是什么字段"
    assert env.content == ""


def test_legacy_known_intent_is_envelope_but_not_gap():
    """是信封但不该入队；关键是仍被识别为信封，绝不能把 JSON 发给用户。"""
    env = parse_envelope(_env(intent="punish_query", original_query="x"))

    assert env is not None
    assert not env.has_gap


# ---------- 非信封一律透传 ----------


def test_plain_prose_is_not_an_envelope():
    assert parse_envelope("客户投诉的处罚大类是 21。") is None


def test_json_without_known_keys_is_not_an_envelope():
    assert parse_envelope(_env(foo="bar")) is None


def test_json_array_is_not_an_envelope():
    assert parse_envelope(json.dumps([{"content": "x"}])) is None


def test_empty_input_is_not_an_envelope():
    assert parse_envelope("") is None
    assert parse_envelope("   ") is None


def test_parses_envelope_inside_markdown_fence():
    raw = "```json\n" + _env(content="答案", unknown="") + "\n```"

    assert parse_envelope(raw).content == "答案"


def test_keeps_raw_json_for_persistence():
    raw = _env(content="答案", unknown="没答上的")

    assert json.loads(parse_envelope(raw).raw_json)["unknown"] == "没答上的"
