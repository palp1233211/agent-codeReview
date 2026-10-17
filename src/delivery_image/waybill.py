"""图片中运单号的代码核对：模型只抄录原文，是否与目标单号一致由这里裁决。"""

import re

from .schemas import DeliveryDirection, DeliveryImageResult, DeliveryPlatform, EvidenceType

# TT 退件下这些证据要求"按正向单号沟通"，核对号码须使用 forward_pno。
# 录音/视频不走图片规则，对应的正向单号校验在音视频接口里。
TT_RETURN_FORWARD_PNO_TYPES = frozenset({
    EvidenceType.CUSTOMER_CONFIRMATION_SMS,
    EvidenceType.CUSTOMER_CONFIRMATION_WHATSAPP,
})

_WAYBILL_FINDING_PREFIX = "运单号识别："
_RAW_TEXT_PATTERN = re.compile(r"原文「(.*?)」")


def accepted_waybills(
    *,
    pno: str,
    forward_pno: str | None,
    platform: DeliveryPlatform | None,
    direction: DeliveryDirection | None,
    evidence_type: EvidenceType,
) -> tuple[str, ...]:
    """该证据类型下可作为"本单运单号"的号码；空元组表示没有可核对的号码。

    1.5 只有 TT 退件要求"按正向单号沟通"，此时只认 forward_pno；其他平台和方向都是
    "单号跟反馈单号一致"，即 pno。
    """
    if (
        direction == DeliveryDirection.RETURN
        and platform == DeliveryPlatform.TT
        and evidence_type in TT_RETURN_FORWARD_PNO_TYPES
    ):
        return (forward_pno,) if forward_pno else ()
    return (pno,)


def waybill_raw_text(image: DeliveryImageResult) -> str | None:
    """取模型在「运单号识别」里抄录的图片原文。"""
    finding = next((item for item in image.findings if item.startswith(_WAYBILL_FINDING_PREFIX)), None)
    if finding is None:
        return None
    match = _RAW_TEXT_PATTERN.search(finding)
    return match.group(1).strip() if match else None


def waybill_confirmed(image: DeliveryImageResult, targets: tuple[str, ...]) -> bool:
    """抄录原文必须完整包含某个目标单号；部分尾号、带 ? 的残缺号码、图片未见都不算。"""
    raw = waybill_raw_text(image)
    if not raw or not targets:
        return False
    return any(_contains_identifier(raw, target) for target in targets)


def _contains_identifier(text: str, target: str) -> bool:
    normalized = re.sub(r"[^A-Z0-9]", "", target.upper())
    if not normalized:
        return False
    body = r"[^A-Za-z0-9]*".join(re.escape(character) for character in normalized)
    return re.search(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", text, flags=re.IGNORECASE) is not None
