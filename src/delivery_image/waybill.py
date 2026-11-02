"""图片中运单号的代码核对：模型只抄录原文，是否与目标单号一致由这里裁决。"""

import re

from .schemas import DeliveryDirection, DeliveryImageResult, DeliveryPlatform, EvidenceType

# TT 退件下这些证据要求"按正向单号沟通"，核对号码须使用 forward_pno。
# 录音/视频不走图片规则，对应的正向单号校验在音视频接口里。
TT_RETURN_FORWARD_PNO_TYPES = frozenset({
    EvidenceType.CUSTOMER_CONFIRMATION_SMS,
    EvidenceType.CUSTOMER_CONFIRMATION_WHATSAPP,
})

# 对话截图类证据：运单号必须是快递员发给客户的聊天文字，不能靠识别对话里的照片（面单缩略图、包裹照片）来关联本单
CHAT_CONFIRMATION_TYPES = frozenset({
    EvidenceType.CUSTOMER_CONFIRMATION_SMS,
    EvidenceType.CUSTOMER_CONFIRMATION_LINE_WECHAT,
    EvidenceType.CUSTOMER_CONFIRMATION_WHATSAPP,
})
COURIER_TEXT_SOURCE = "快递员发送的聊天文字"

# 照片类证据（含系统 EPOD 图）：运单号必须是图片中包裹面单上的，不能用水印——
# 水印是系统打的标记，一定和系统里的单号一致，不能证明拍的包裹就是本单。
PHOTO_EVIDENCE_TYPES = frozenset({
    EvidenceType.PARCEL_SCENE_PHOTO,
    EvidenceType.WAYBILL_PHOTO,
    EvidenceType.REDISPATCH_DELIVERY_PHOTO,
})
LABEL_SOURCE = "包裹面单"

# 证据类型 -> 运单号来源必须是什么；不在表里的类型不要求来源
REQUIRED_WAYBILL_SOURCES = {
    **{evidence_type: COURIER_TEXT_SOURCE for evidence_type in CHAT_CONFIRMATION_TYPES},
    **{evidence_type: LABEL_SOURCE for evidence_type in PHOTO_EVIDENCE_TYPES},
}

_WAYBILL_FINDING_PREFIX = "运单号识别："
_WAYBILL_SOURCE_PREFIX = "运单号来源："
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


def waybill_source(image: DeliveryImageResult) -> str | None:
    """取模型在「运单号来源」里写的来源（如：快递员发送的聊天文字、聊天中的图片）。"""
    finding = next((item for item in image.findings if item.startswith(_WAYBILL_SOURCE_PREFIX)), None)
    if finding is None:
        return None
    return finding[len(_WAYBILL_SOURCE_PREFIX):].strip().rstrip("。 ").strip("「」") or None


def required_waybill_source(evidence_type: EvidenceType) -> str | None:
    """该证据类型下运单号必须来自哪里；None 表示不要求。"""
    return REQUIRED_WAYBILL_SOURCES.get(evidence_type)


def waybill_confirmed(image: DeliveryImageResult, targets: tuple[str, ...]) -> bool:
    """抄录原文必须完整包含某个目标单号；部分尾号、带 ? 的残缺号码、图片未见都不算。

    还要求来源可信：
    - 对话截图：必须是快递员发送给客户的聊天文字。对话里的面单照片多是低分辨率缩略图，模型读不清时容易顺着目标单号补全。
    - 照片类证据：必须是包裹面单上的运单号，不能是水印。
    没写来源同样不算。
    """
    raw = waybill_raw_text(image)
    if not raw or not targets:
        return False
    required = required_waybill_source(image.evidence_type)
    if required is not None and waybill_source(image) != required:
        return False
    return any(_contains_identifier(raw, target) for target in targets)


def _contains_identifier(text: str, target: str) -> bool:
    normalized = re.sub(r"[^A-Z0-9]", "", target.upper())
    if not normalized:
        return False
    body = r"[^A-Za-z0-9]*".join(re.escape(character) for character in normalized)
    return re.search(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", text, flags=re.IGNORECASE) is not None
