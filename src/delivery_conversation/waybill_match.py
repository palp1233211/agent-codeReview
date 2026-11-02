"""转写运单号与目标运单号的容错比对：只允许快递员「漏念」字符，不允许念错或多念。

按整体相似度（编辑距离）比对会把「念错一位」也当成相似，而念错恰好是另一个包裹（连号）的特征；
只容许漏字时，听到的内容必须是目标运单号删去若干字符后的样子（其余字符顺序不变），
等长的另一个运单号无论差几位都不会通过。

另外两件事（都只在号码主体完全一致时才生效，不会放过不同的号码）：
- 前缀（TH/THT 等开头的字母）转写成口语变体（“T Head”“ที เอช”），或者快递员干脆没念前缀；
- 第二份转写（不带目标号码、不分说话人）是自由文本，把口语转成字母数字流后再找目标号码。
"""

import re
import unicodedata

MAX_DROPPED_CHARS = 2
MIN_KEPT_RATIO = 0.8
# 前缀可以整个不念，但号码主体至少要有这么多字符，才能保证主体本身足够唯一
MIN_BODY_LENGTH = 8

_THAI_DIGIT_WORDS = {
    "ศูนย์": "0", "หนึ่ง": "1", "สอง": "2", "สาม": "3", "สี่": "4",
    "ห้า": "5", "หก": "6", "เจ็ด": "7", "แปด": "8", "เก้า": "9",
}
# 前缀字母的口语写法：T 可能被转成泰语字母名“ที”，H 可能被转成“Head”“เอช”“เฮด”
_PREFIX_LETTER_SPOKEN = {"T": r"(?:T|ที)", "H": r"(?:Head|H|เอช|เฮด|เฮ็ด)"}
_SEPARATOR = r"[\s\-]*"


def _alnum(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _split_prefix(value: str) -> tuple[str, str]:
    """（开头的字母前缀, 其余主体）。"""
    prefix = re.match(r"[A-Z]*", value).group(0)
    return prefix, value[len(prefix):]


def _is_subsequence(part: str, whole: str) -> bool:
    remaining = iter(whole)
    return all(char in remaining for char in part)


def dropped_chars(heard: str, target: str) -> int | None:
    """heard 是 target 删去几个字符后的结果就返回删去的个数（0 为完全一致），否则返回 None。

    两个参数都应已归一化（只含大写字母和数字）。漏字个数超过 MAX_DROPPED_CHARS，
    或保留的字符不足目标长度的 MIN_KEPT_RATIO 时返回 None。
    """
    if not heard or not target or len(heard) > len(target):
        return None
    dropped = len(target) - len(heard)
    if dropped > MAX_DROPPED_CHARS or len(heard) / len(target) < MIN_KEPT_RATIO:
        return None
    # `char in remaining` 会消耗迭代器直到找到该字符，从而保证 heard 的字符在 target 里按顺序出现
    if _is_subsequence(heard, target):
        return dropped
    return None


def match_waybill(heard: str, target: str) -> tuple[str, int] | None:
    """heard 与 target 的核对结果：("exact", 0)、("dropped", 漏的个数)、("prefix_omitted", 0)；对不上返回 None。

    prefix_omitted：前缀没念全（或整个没念），但号码主体完全一致，且主体不少于 MIN_BODY_LENGTH 个字符。
    听到的前缀必须是目标前缀的子序列（如 THT 里的 TH、T），换成别的字母不算。
    """
    heard, target = _alnum(heard), _alnum(target)
    if not heard or not target:
        return None
    if heard == target:
        return "exact", 0
    dropped = dropped_chars(heard, target)
    if dropped is not None:
        return "dropped", dropped
    target_prefix, target_body = _split_prefix(target)
    heard_prefix, heard_body = _split_prefix(heard)
    if (
        len(target_body) >= MIN_BODY_LENGTH
        and heard_body == target_body
        and _is_subsequence(heard_prefix, target_prefix)
    ):
        return "prefix_omitted", 0
    return None


def normalize_prefix_variants(text: str, target: str) -> str:
    """把转写里前缀的口语变体（“T Head 000248020”“ที เอช ที 01075…”）规范成目标运单号。

    只在前缀后面的号码主体与目标完全一致时才改写；其他任何情况原样返回，不会把别的号码改成目标号码。
    目前只处理由 T、H 组成的前缀（TH、THT）。
    """
    expected = _alnum(target)
    prefix, body = _split_prefix(expected)
    if not prefix or not body or any(letter not in _PREFIX_LETTER_SPOKEN for letter in prefix):
        return text
    prefix_pattern = _SEPARATOR.join(_PREFIX_LETTER_SPOKEN[letter] for letter in prefix)
    body_pattern = _SEPARATOR.join(re.escape(char) for char in body)
    pattern = re.compile(rf"(?<![A-Za-z0-9]){prefix_pattern}{_SEPARATOR}{body_pattern}(?![A-Za-z0-9])", re.IGNORECASE)
    return pattern.sub(target, text)


def spoken_alnum(text: str) -> str:
    """把口语转写变成字母数字流：泰语数字词和泰文数字转成 0-9，其余非字母数字（含泰语文字）全部丢掉。"""
    converted = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in text)
    for word, digit in sorted(_THAI_DIGIT_WORDS.items(), key=lambda item: -len(item[0])):
        converted = converted.replace(word, digit)
    return _alnum(converted)


def find_in_spoken_text(text: str, target: str) -> tuple[str, str] | None:
    """在第二份转写（自由文本）里找目标运单号：整串一致，或前缀没念但号码主体一致。返回 (种类, 命中的片段)。"""
    stream = spoken_alnum(text)
    expected = _alnum(target)
    if not stream or not expected:
        return None
    if expected in stream:
        return "exact", expected
    _, body = _split_prefix(expected)
    if len(body) >= MIN_BODY_LENGTH and body in stream:
        return "prefix_omitted", body
    return None
