"""手机号归一化与图片中手机号的代码核对：模型只抄录原文，是否与 recipient_phone 一致由这里裁决。"""

import re
import unicodedata

from .schemas import DeliveryImageResult

_PHONE_FINDING_PREFIX = "手机号识别："
_RAW_TEXT_PATTERN = re.compile(r"原文「(.*?)」")
# 抄录原文里的号码片段：数字开头结尾，中间允许空格、连字符、括号；至少 8 位数字。
_PHONE_CANDIDATE_PATTERN = re.compile(r"\+?\d[\d\s()\-]{6,}\d")


def normalize_phone(raw: str, country: str) -> str | None:
    """不补全遮挡数字；泰国本地号码转换为国际格式，其他国家仅支持完整国际号码。"""
    value = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in raw)
    value = re.sub(r"[\s()\-]", "", value)
    if value.startswith("00"):
        value = "+" + value[2:]
    if country.upper() == "TH":
        if re.fullmatch(r"0[1-9][0-9]{8}", value):
            return "+66" + value[1:]
        if re.fullmatch(r"\+?66[1-9][0-9]{8}", value):
            return "+" + value.lstrip("+")
        return None
    return value if re.fullmatch(r"\+[1-9][0-9]{7,14}", value) else None


def phone_raw_text(image: DeliveryImageResult) -> str | None:
    finding = next((item for item in image.findings if item.startswith(_PHONE_FINDING_PREFIX)), None)
    if finding is None:
        return None
    match = _RAW_TEXT_PATTERN.search(finding)
    return match.group(1).strip() if match else None


def phone_confirmed(image: DeliveryImageResult, recipient_phone: str | None, country: str) -> bool:
    """抄录原文里必须有一个完整可读的号码，归一化后与 recipient_phone 相同；残缺、遮挡、图片未见都不算。"""
    target = normalize_phone(recipient_phone, country) if recipient_phone else None
    raw = phone_raw_text(image)
    if target is None or not raw:
        return False
    return any(
        normalize_phone(candidate, country) == target
        for candidate in _PHONE_CANDIDATE_PATTERN.findall(raw)
    )
