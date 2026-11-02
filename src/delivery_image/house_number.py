"""收件地址门牌号的核对结果：地址里的门牌号由模型识别、图片上的门牌号由模型抄录并比对，这里只解析并把关。

地址写法太多，不用规则从地址里抠门牌号；模型在 findings 里按固定格式写出：
“门牌号识别：地址门牌号「<地址里的门牌号/无>」；图片原文「<图片上可见的门牌号原文/图片未见>」；比对：一致/不一致/部分一致/图片未见/地址无门牌号/无法确定。”
门牌号只有图片里有的时候才作为条件：图片没有门牌号（或模型没写这一条）不能作为不合格的依据。
代码只做两件事：按“比对”结论分类；模型说“一致”但它自己写出的两个号码明显对不上时，不采信。
"""

import re
import unicodedata
from enum import Enum

from .schemas import DeliveryImageResult

HOUSE_FINDING_PREFIX = "门牌号识别："
_VERDICT_PATTERN = re.compile(r"比对[：:]\s*([^。；;]*)")


class HouseNumberStatus(str, Enum):
    NOT_REQUIRED = "not_required"  # 请求没给地址，或模型判断地址里没有 / 无法确定门牌号：不核对
    MATCHED = "matched"
    MISMATCH = "mismatch"  # 图片上是另一个门牌号（拍在别家门口）
    PARTIAL = "partial"  # 只读到门牌号的一部分，不能判定一致，也不判定不一致
    UNSEEN = "unseen"  # 图片里没有读到门牌号，或没有可解析的门牌号记录：没有依据，不作为不合格的理由


def house_number_finding(image: DeliveryImageResult) -> str | None:
    return next((item for item in image.findings if item.startswith(HOUSE_FINDING_PREFIX)), None)


def _quoted(finding: str, label: str) -> str:
    match = re.search(rf"{label}「(.*?)」", finding)
    return match.group(1).strip() if match else ""


def _digits_form(text: str) -> str:
    """泰文数字转阿拉伯数字，去掉空白，斜杠和连字符保留；只用来核对模型自己写出的两个号码。"""
    converted = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in text)
    return re.sub(r"\s+", "", converted).lower()


def _claims_match_but_numbers_differ(address_number: str, photo_text: str) -> bool:
    """模型写“一致”，但它抄的图片原文里找不到它自己写的地址门牌号：自述不可信。"""
    wanted, seen = _digits_form(address_number), _digits_form(photo_text)
    if not wanted or not seen or not re.search(r"\d", wanted):
        return False
    return re.search(rf"(?<![\d/\-]){re.escape(wanted)}(?![\d/\-])", seen) is None


def house_number_status(image: DeliveryImageResult, address_provided: bool) -> HouseNumberStatus:
    """只有图片里读到了门牌号且与地址不同才是 MISMATCH；图片没有门牌号、没写记录、格式解析不了，都只是 UNSEEN，不能据此判不合格。

    address_provided=False 表示请求没带收件地址，不核对。
    """
    if not address_provided:
        return HouseNumberStatus.NOT_REQUIRED
    finding = house_number_finding(image)
    if finding is None:
        return HouseNumberStatus.UNSEEN
    match = _VERDICT_PATTERN.search(finding)
    verdict = match.group(1).strip() if match else ""
    if "地址无门牌号" in verdict or "无法确定" in verdict:
        return HouseNumberStatus.NOT_REQUIRED
    if "图片未见" in verdict:
        return HouseNumberStatus.UNSEEN
    if "不一致" in verdict:
        return HouseNumberStatus.MISMATCH
    if "部分" in verdict:
        return HouseNumberStatus.PARTIAL
    if "一致" in verdict:
        if _claims_match_but_numbers_differ(_quoted(finding, "地址门牌号"), _quoted(finding, "图片原文")):
            return HouseNumberStatus.MISMATCH
        return HouseNumberStatus.MATCHED
    return HouseNumberStatus.UNSEEN
