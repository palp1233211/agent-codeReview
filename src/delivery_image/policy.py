"""妥投举证规则表与联合复核校验。

模型只负责读图并给出条件核对结果；哪些(平台, 方向, 证据类型)需要多条件或多张图配合、
阈值是多少、引用的证据是否站得住，全部由这里的代码裁决，不信任模型自述。
"""

from dataclasses import dataclass

from .schemas import (
    ConditionCheck,
    DeliveryDirection,
    DeliveryImageConclusion,
    DeliveryImageResult,
    DeliveryPlatform,
    EvidenceType,
    ImageReviewStatus,
    PolicyMatch,
)

AWB_CONDITION = "awb_matches_pno"

# 需要核对客户手机号的证据类型（客户确认、客服回访、快递员通话）。
PHONE_EVIDENCE_TYPES = frozenset({
    EvidenceType.CUSTOMER_CONFIRMATION_SMS,
    EvidenceType.CUSTOMER_CONFIRMATION_LINE_WECHAT,
    EvidenceType.CUSTOMER_CONFIRMATION_WHATSAPP,
    EvidenceType.CS_CALLBACK_RECORDING,
    EvidenceType.COURIER_CALL_VIDEO,
    EvidenceType.COURIER_CALL_AUDIO,
})

CONDITION_DESCRIPTIONS = {
    AWB_CONDITION: "面单/AWB 的运单号与目标 pno 完整一致（可以由同一订单的另一张面单照片提供）",
    "background_matches": "包裹所在背景与订单交付场景一致",
    "gps_within_1km": "照片 GPS/位置显示在收件人地址 1 公里内",
    "customer_reply_received": "客户在聊天中明确回复已收到包裹",
}


@dataclass(frozen=True)
class EvidenceRule:
    """某类证据在特定平台/方向下的多条件要求。"""

    conditions: tuple[str, ...] = ()
    min_met: int = 0
    # 单张图是否可以独立通过整单。False 表示必须经联合复核逐项核对条件。
    single_image_ok: bool = True
    # 能否通过图片组合满足。录屏加录音这类要求图片接口无法提供，永远为 False。
    joint_possible: bool = True


_LINK_RULE = EvidenceRule(conditions=(AWB_CONDITION,), min_met=1)
_IMAGES_CANNOT_SATISFY = EvidenceRule(single_image_ok=False, joint_possible=False)

_REPLY_PHOTO_RULE = EvidenceRule(
    conditions=("customer_reply_received",),
    min_met=1,
    single_image_ok=False,
)

_FORWARD_RULES: dict[tuple[DeliveryPlatform, EvidenceType], EvidenceRule] = {
    # LAZADA：AWB、背景、收件人 1 公里内 GPS 三项满足其中两项。
    (DeliveryPlatform.LAZADA, EvidenceType.PARCEL_SCENE_PHOTO): EvidenceRule(
        conditions=(AWB_CONDITION, "background_matches", "gps_within_1km"),
        min_met=2,
        single_image_ok=False,
    ),
    # SHOPEE：LINE/微信还需客户明确回复已收到。
    (DeliveryPlatform.SHOPEE, EvidenceType.CUSTOMER_CONFIRMATION_LINE_WECHAT): _REPLY_PHOTO_RULE,
    # TT：客服回访须有录屏加录音，图片只是可见凭证。
    (DeliveryPlatform.TT, EvidenceType.CS_CALLBACK_RECORDING): _IMAGES_CANNOT_SATISFY,
    # 平台直接认可的照片类证据：面单可由另一张面单照片关联。
    (DeliveryPlatform.SHEIN, EvidenceType.PARCEL_SCENE_PHOTO): _LINK_RULE,
    (DeliveryPlatform.TMALL, EvidenceType.PARCEL_SCENE_PHOTO): _LINK_RULE,
    (DeliveryPlatform.SHOPEE, EvidenceType.REDISPATCH_DELIVERY_PHOTO): _LINK_RULE,
    (DeliveryPlatform.SHEIN, EvidenceType.REDISPATCH_DELIVERY_PHOTO): _LINK_RULE,
    (DeliveryPlatform.TMALL, EvidenceType.REDISPATCH_DELIVERY_PHOTO): _LINK_RULE,
}


_RETURN_RULES: dict[tuple[DeliveryPlatform, EvidenceType], EvidenceRule] = {
    # TT 退件客服回访：须有录屏加录音，且按正向单号沟通，图片无法满足。
    (DeliveryPlatform.TT, EvidenceType.CS_CALLBACK_RECORDING): _IMAGES_CANNOT_SATISFY,
    # SHOPEE 退件 LINE/微信同样需要另一张客户回复已收到的照片。
    (DeliveryPlatform.SHOPEE, EvidenceType.CUSTOMER_CONFIRMATION_LINE_WECHAT): _REPLY_PHOTO_RULE,
}

# TT 退件下这些证据要求"按正向单号沟通"，核对号码须使用 forward_pno。
TT_RETURN_FORWARD_PNO_TYPES = frozenset({
    EvidenceType.CUSTOMER_CONFIRMATION_SMS,
    EvidenceType.CUSTOMER_CONFIRMATION_WHATSAPP,
    EvidenceType.COURIER_CALL_VIDEO,
    EvidenceType.CS_CALLBACK_RECORDING,
})


def get_rule(
    platform: DeliveryPlatform | None,
    direction: DeliveryDirection | None,
    evidence_type: EvidenceType,
) -> EvidenceRule | None:
    """返回特殊规则；None 表示没有多条件要求（按单图 accepted 处理，也不作为联合复核主证据）。"""
    if platform is None or direction is None:
        return None
    if direction == DeliveryDirection.RETURN:
        # 退件沿用 LAZADA 原规则，不继承正向的 0910 三选二等条件。
        return _RETURN_RULES.get((platform, evidence_type))
    return _FORWARD_RULES.get((platform, evidence_type))


def describe_conditions(rule: EvidenceRule) -> str:
    names = "；".join(f"{name}（{CONDITION_DESCRIPTIONS.get(name, name)}）" for name in rule.conditions)
    return f"{names}；至少满足 {rule.min_met} 项"


def _finding_value(image: DeliveryImageResult, prefix: str, marker: str) -> str | None:
    finding = next((item for item in image.findings if item.startswith(prefix)), None)
    if finding is None or marker not in finding:
        return None
    return finding.split(marker, 1)[1].lstrip("：:").strip("。 ")


def _waybill_status(image: DeliveryImageResult) -> str | None:
    return _finding_value(image, "运单号识别：", "与目标 pno 比对")


def _phone_status(image: DeliveryImageResult) -> str | None:
    return _finding_value(image, "手机号识别：", "与 recipient_phone 比对")


def verify_joint_review(
    *,
    rule: EvidenceRule,
    primary_image_id: str | None,
    checks: list[ConditionCheck],
    images: dict[str, DeliveryImageResult],
    recipient_phone_required: bool,
) -> list[str]:
    """返回未通过原因；空列表表示联合复核通过。images 为本次联合复核的候选图。"""
    if not rule.joint_possible:
        return ["该证据类型要求图片以外的材料（如录屏加录音），不能通过图片组合满足。"]
    primary = images.get(primary_image_id or "")
    if primary is None:
        return [f"主证据图片 {primary_image_id!r} 不在联合复核候选图中。"]

    reasons: list[str] = []
    if primary.conclusion != DeliveryImageConclusion.VALID or primary.policy_match not in {
        PolicyMatch.ACCEPTED, PolicyMatch.CONDITIONAL,
    }:
        reasons.append(f"主证据图片 {primary.image_id} 不是平台规则可采纳的有效证据。")
    if primary.evidence_type in PHONE_EVIDENCE_TYPES:
        if not recipient_phone_required:
            reasons.append("客户确认类证据缺少 recipient_phone，无法核对收件人身份。")
        elif _phone_status(primary) != "一致":
            reasons.append(f"图片 {primary.image_id} 的手机号没有与 recipient_phone 明确核对一致。")

    # 同名条件只算一次；规则之外的条件名忽略。
    met_by_name: dict[str, ConditionCheck] = {}
    for check in checks:
        if check.name in rule.conditions and check.met and check.name not in met_by_name:
            met_by_name[check.name] = check

    involved = {primary.image_id}
    verified = 0
    for name, check in met_by_name.items():
        evidence = images.get(check.evidence_image_id or "")
        if evidence is None or evidence.review_status != ImageReviewStatus.REVIEWED:
            reasons.append(f"条件 {name} 引用的证据图片 {check.evidence_image_id!r} 不在候选图中。")
            continue
        if name == AWB_CONDITION and _waybill_status(evidence) != "一致":
            reasons.append(
                f"条件 {name} 的证据图片 {evidence.image_id} 没有明确核对到一致的运单号"
                f"（比对结果：{_waybill_status(evidence) or '无记录'}）。"
            )
            continue
        involved.add(evidence.image_id)
        verified += 1

    if verified < rule.min_met:
        reasons.append(f"经核验的条件仅 {verified} 项，规则至少需要 {rule.min_met} 项。")

    for image_id in involved:
        image = images[image_id]
        if _waybill_status(image) == "不一致":
            reasons.append(f"图片 {image_id} 识别出的运单号与目标 pno 不一致。")
        if _phone_status(image) == "不一致":
            reasons.append(f"图片 {image_id} 识别出的手机号与 recipient_phone 不一致。")
    return reasons
