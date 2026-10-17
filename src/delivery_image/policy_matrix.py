"""《DNR举证要求》1.5 的图片证据准入矩阵（泰国）。

模型只负责识别证据类型；该类型在某平台/方向下是否被采纳，由这里的代码决定，
不信任模型自述的 policy_match。LAZADA 与其他平台一样，只按对应方向的要求判断，不区分新旧版本。
图片接口不走录音/视频的规则：客服回访录音、快递员电话视频、电话音频的原件是媒体文件，
「客户回复已收到」只能由音视频接口核验，图片（截图）一律 unsupported，不能通过，也不参与联合复核。
"""

from .schemas import DeliveryDirection, DeliveryPlatform, EvidenceType, PolicyMatch

_P = DeliveryPlatform
_ALL_PLATFORMS = frozenset(DeliveryPlatform)


def _row(**by_match: set[DeliveryPlatform]) -> dict[DeliveryPlatform, PolicyMatch]:
    """按规则状态逐个列出平台，五个平台必须全部出现，避免漏配。"""
    row: dict[DeliveryPlatform, PolicyMatch] = {}
    for match_name, platforms in by_match.items():
        for platform in platforms:
            row[platform] = PolicyMatch(match_name)
    missing = _ALL_PLATFORMS - row.keys()
    if missing:
        raise ValueError(f"准入矩阵缺少平台：{sorted(item.value for item in missing)}")
    return row


def _row_rest(rest: PolicyMatch, **by_match: set[DeliveryPlatform]) -> dict[DeliveryPlatform, PolicyMatch]:
    row = {platform: rest for platform in _ALL_PLATFORMS}
    for match_name, platforms in by_match.items():
        for platform in platforms:
            row[platform] = PolicyMatch(match_name)
    return row


_FORWARD: dict[EvidenceType, dict[DeliveryPlatform, PolicyMatch]] = {
    # SHOPEE 已由业务确认认可，规则同 SHEIN、TMALL：AWB 加背景，不看 GPS。
    EvidenceType.PARCEL_SCENE_PHOTO: _row(
        rejected={_P.TT}, conditional={_P.SHOPEE, _P.SHEIN, _P.TMALL, _P.LAZADA}),
    EvidenceType.REDISPATCH_DELIVERY_PHOTO: _row(
        rejected={_P.TT, _P.LAZADA}, accepted={_P.SHOPEE, _P.SHEIN, _P.TMALL}),
    EvidenceType.CUSTOMER_CONFIRMATION_SMS: _row_rest(PolicyMatch.ACCEPTED, rejected={_P.LAZADA}),
    EvidenceType.CUSTOMER_CONFIRMATION_LINE_WECHAT: _row(
        accepted={_P.SHEIN, _P.TMALL}, conditional={_P.SHOPEE}, rejected={_P.TT, _P.LAZADA}),
    EvidenceType.CUSTOMER_CONFIRMATION_WHATSAPP: _row_rest(PolicyMatch.ACCEPTED, rejected={_P.LAZADA}),
}

_RETURN: dict[EvidenceType, dict[DeliveryPlatform, PolicyMatch]] = {
    # 退件方向的现场照指"原合格的退件签收照片"。
    EvidenceType.PARCEL_SCENE_PHOTO: _row_rest(PolicyMatch.ACCEPTED, rejected={_P.TT}),
    EvidenceType.REDISPATCH_DELIVERY_PHOTO: _row_rest(PolicyMatch.ACCEPTED, rejected={_P.TT}),
    EvidenceType.CUSTOMER_CONFIRMATION_SMS: _row_rest(PolicyMatch.ACCEPTED),
    EvidenceType.CUSTOMER_CONFIRMATION_LINE_WECHAT: _row(
        accepted={_P.SHEIN, _P.TMALL}, conditional={_P.SHOPEE}, rejected={_P.TT, _P.LAZADA}),
    EvidenceType.CUSTOMER_CONFIRMATION_WHATSAPP: _row_rest(PolicyMatch.ACCEPTED, rejected={_P.LAZADA}),
}

# 只适用于系统 EPOD 图片的类型：用户上传的图片不适用，无法按 1.5 第 3、4 行判定。
EPOD_ONLY_TYPES = frozenset({EvidenceType.PARCEL_SCENE_PHOTO, EvidenceType.REDISPATCH_DELIVERY_PHOTO})

# 原件是录音/视频文件的类型：图片接口不核验，所有平台、方向一律 unsupported。
_MEDIA_ONLY_TYPES = frozenset({
    EvidenceType.CS_CALLBACK_RECORDING,
    EvidenceType.COURIER_CALL_VIDEO,
    EvidenceType.COURIER_CALL_AUDIO,
})

# 本身不是妥投证据（面单只证明运单身份）或无法归类的类型，规则状态一律 unknown。
_NEVER_ACCEPTED_TYPES = frozenset({EvidenceType.WAYBILL_PHOTO, EvidenceType.OTHER, EvidenceType.UNKNOWN})


def expected_policy_match(
    platform: DeliveryPlatform | None,
    direction: DeliveryDirection | None,
    evidence_type: EvidenceType,
    *,
    is_epod: bool = True,
) -> PolicyMatch | None:
    """代码给出的规则状态；None 表示平台或方向缺失，无法匹配规则。

    is_epod 表示该图片是否系统 EPOD 图片；原合格 EPOD、补派照两类只对 EPOD 图片适用。
    """
    if evidence_type in _MEDIA_ONLY_TYPES:
        return PolicyMatch.UNSUPPORTED
    if evidence_type in EPOD_ONLY_TYPES and not is_epod:
        return PolicyMatch.UNSUPPORTED
    if platform is None or direction is None:
        return None
    if evidence_type in _NEVER_ACCEPTED_TYPES:
        return PolicyMatch.UNKNOWN
    matrix = _RETURN if direction == DeliveryDirection.RETURN else _FORWARD
    return matrix[evidence_type][platform]
