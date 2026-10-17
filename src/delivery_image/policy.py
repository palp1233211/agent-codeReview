"""妥投举证规则表与联合复核校验。

模型只负责读图并给出条件核对结果；哪些(平台, 方向, 证据类型)需要多条件或多张图配合、
阈值是多少、引用的证据是否站得住，全部由这里的代码裁决，不信任模型自述。
"""

from collections.abc import Callable
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
from .waybill import waybill_confirmed

AWB_CONDITION = "awb_matches_pno"

# 需要核对客户手机号的证据类型（客户确认截图）。录音/视频类图片不走图片规则，见 policy_matrix。
PHONE_EVIDENCE_TYPES = frozenset({
    EvidenceType.CUSTOMER_CONFIRMATION_SMS,
    EvidenceType.CUSTOMER_CONFIRMATION_LINE_WECHAT,
    EvidenceType.CUSTOMER_CONFIRMATION_WHATSAPP,
})

GPS_CONDITION = "gps_within_1km"

CONDITION_DESCRIPTIONS = {
    AWB_CONDITION: "AWB：图片内包裹上的运单号（pno）与目标 pno 完整一致；多张图片中任一张能判定一致即可",
    "background_matches": "背景：包裹所在背景能体现收件人最终交付地点（如收件人门牌号、居住环境）",
    GPS_CONDITION: "照片 GPS/位置显示在收件人地址 1 公里内",
    "customer_reply_received": "客户在聊天中明确回复已收到包裹（同一张截图里已包含该回复也算满足）",
}

# 缺少核验输入、当前一律不判定的条件：不计入已满足项，只在复核结果里标记待核验。
# GPS 是否在收件人 1 公里内将由业务方后续以请求参数提供，目前请求里没有收件人地址/坐标。
UNVERIFIABLE_CONDITIONS = {
    GPS_CONDITION: "GPS 与收件人 1 公里的核验暂未开通，待业务方提供参数，当前不计入已满足条件",
}


@dataclass(frozen=True)
class EvidenceRule:
    """某类证据在特定平台/方向下的多条件要求。"""

    conditions: tuple[str, ...] = ()
    min_met: int = 0
    # 单张图是否可以独立通过整单。False 表示必须经联合复核逐项核对条件。
    single_image_ok: bool = True


_LINK_RULE = EvidenceRule(conditions=(AWB_CONDITION,), min_met=1)

_REPLY_PHOTO_RULE = EvidenceRule(
    conditions=("customer_reply_received",),
    min_met=1,
    single_image_ok=False,
)

# SHOPEE、SHEIN、TMALL 与 LAZADA 的规则相同，只是不看 GPS：AWB 与背景两项都要满足。
_AWB_AND_BACKGROUND_RULE = EvidenceRule(
    conditions=(AWB_CONDITION, "background_matches"),
    min_met=2,
    single_image_ok=False,
)

_FORWARD_RULES: dict[tuple[DeliveryPlatform, EvidenceType], EvidenceRule] = {
    # LAZADA：AWB、背景、收件人 1 公里内 GPS 三项满足其中两项。
    # GPS 暂不核验（UNVERIFIABLE_CONDITIONS），实际需要 AWB 与背景同时满足。
    (DeliveryPlatform.LAZADA, EvidenceType.PARCEL_SCENE_PHOTO): EvidenceRule(
        conditions=(AWB_CONDITION, "background_matches", GPS_CONDITION),
        min_met=2,
        single_image_ok=False,
    ),
    # SHOPEE：LINE/微信还需客户明确回复已收到。
    (DeliveryPlatform.SHOPEE, EvidenceType.CUSTOMER_CONFIRMATION_LINE_WECHAT): _REPLY_PHOTO_RULE,
    (DeliveryPlatform.SHOPEE, EvidenceType.PARCEL_SCENE_PHOTO): _AWB_AND_BACKGROUND_RULE,
    (DeliveryPlatform.SHEIN, EvidenceType.PARCEL_SCENE_PHOTO): _AWB_AND_BACKGROUND_RULE,
    (DeliveryPlatform.TMALL, EvidenceType.PARCEL_SCENE_PHOTO): _AWB_AND_BACKGROUND_RULE,
    # 补派照：运单号可由另一张图中包裹上的单号关联。
    (DeliveryPlatform.SHOPEE, EvidenceType.REDISPATCH_DELIVERY_PHOTO): _LINK_RULE,
    (DeliveryPlatform.SHEIN, EvidenceType.REDISPATCH_DELIVERY_PHOTO): _LINK_RULE,
    (DeliveryPlatform.TMALL, EvidenceType.REDISPATCH_DELIVERY_PHOTO): _LINK_RULE,
}


_RETURN_RULES: dict[tuple[DeliveryPlatform, EvidenceType], EvidenceRule] = {
    # SHOPEE 退件 LINE/微信同样需要另一张客户回复已收到的照片。
    (DeliveryPlatform.SHOPEE, EvidenceType.CUSTOMER_CONFIRMATION_LINE_WECHAT): _REPLY_PHOTO_RULE,
}


def get_rule(
    platform: DeliveryPlatform | None,
    direction: DeliveryDirection | None,
    evidence_type: EvidenceType,
) -> EvidenceRule | None:
    """返回特殊规则；None 表示没有多条件要求（按单图 accepted 处理，也不作为联合复核主证据）。"""
    if platform is None or direction is None:
        return None
    if direction == DeliveryDirection.RETURN:
        # 退件使用退件表，不继承正向的三选二等条件。
        return _RETURN_RULES.get((platform, evidence_type))
    return _FORWARD_RULES.get((platform, evidence_type))


def describe_conditions(rule: EvidenceRule) -> str:
    def describe(name: str) -> str:
        text = CONDITION_DESCRIPTIONS.get(name, name)
        if name in UNVERIFIABLE_CONDITIONS:
            text += f"；{UNVERIFIABLE_CONDITIONS[name]}，met 一律填 false"
        return f"{name}（{text}）"

    names = "；".join(describe(name) for name in rule.conditions)
    return f"{names}；至少满足 {rule.min_met} 项"


def mark_unverifiable_conditions(rule: EvidenceRule, checks: list[ConditionCheck]) -> list[ConditionCheck]:
    """返回新的核对清单：待核验条件强制 met=False 并写明原因，规则里有但模型漏报的补一条。"""
    marked: list[ConditionCheck] = []
    for check in checks:
        if check.name in UNVERIFIABLE_CONDITIONS:
            marked.append(check.model_copy(update={
                "met": False,
                "evidence_image_id": None,
                "note": UNVERIFIABLE_CONDITIONS[check.name],
            }))
        else:
            marked.append(check)
    present = {check.name for check in marked}
    for name, note in UNVERIFIABLE_CONDITIONS.items():
        if name in rule.conditions and name not in present:
            marked.append(ConditionCheck(name=name, met=False, evidence_image_id=None, note=note))
    return marked


def _finding_value(image: DeliveryImageResult, prefix: str, marker: str) -> str | None:
    finding = next((item for item in image.findings if item.startswith(prefix)), None)
    if finding is None or marker not in finding:
        return None
    return finding.split(marker, 1)[1].lstrip("：:").strip("。 ")


def _waybill_status(image: DeliveryImageResult) -> str | None:
    return _finding_value(image, "运单号识别：", "与目标 pno 比对")


def _phone_status(image: DeliveryImageResult) -> str | None:
    return _finding_value(image, "手机号识别：", "与 recipient_phone 比对")


def _awb_confirmed(
    image: DeliveryImageResult,
    waybill_targets: Callable[[EvidenceType], tuple[str, ...]] | None,
) -> bool:
    if waybill_targets is None:
        return _waybill_status(image) == "一致"
    return waybill_confirmed(image, waybill_targets(image.evidence_type))


def _phone_confirmed(
    image: DeliveryImageResult,
    phone_confirmed_by_code: Callable[[DeliveryImageResult], bool] | None,
) -> bool:
    if phone_confirmed_by_code is None:
        return _phone_status(image) == "一致"
    return phone_confirmed_by_code(image)


def verify_joint_review(
    *,
    rule: EvidenceRule,
    primary_image_id: str | None,
    checks: list[ConditionCheck],
    images: dict[str, DeliveryImageResult],
    recipient_phone_required: bool,
    waybill_targets: Callable[[EvidenceType], tuple[str, ...]] | None = None,
    phone_confirmed_by_code: Callable[[DeliveryImageResult], bool] | None = None,
) -> list[str]:
    """返回未通过原因；空列表表示联合复核通过。images 为本次联合复核的候选图。

    waybill_targets 按证据类型给出可核对的目标单号；提供时 AWB 条件由代码比对图片抄录原文，
    不再采信模型自述的“一致”。phone_confirmed_by_code 同理用于主证据的手机号核对。
    """
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
        elif not _phone_confirmed(primary, phone_confirmed_by_code):
            reasons.append(f"图片 {primary.image_id} 的手机号没有与 recipient_phone 明确核对一致。")

    # 同名条件只算一次；规则之外的条件名忽略；待核验条件（如 GPS）不计入已满足项。
    met_by_name: dict[str, ConditionCheck] = {}
    for check in checks:
        if (
            check.name in rule.conditions
            and check.name not in UNVERIFIABLE_CONDITIONS
            and check.met
            and check.name not in met_by_name
        ):
            met_by_name[check.name] = check

    involved = {primary.image_id}
    verified = 0
    for name, check in met_by_name.items():
        evidence = images.get(check.evidence_image_id or "")
        if evidence is None or evidence.review_status != ImageReviewStatus.REVIEWED:
            reasons.append(f"条件 {name} 引用的证据图片 {check.evidence_image_id!r} 不在候选图中。")
            continue
        if name == AWB_CONDITION and not _awb_confirmed(evidence, waybill_targets):
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
