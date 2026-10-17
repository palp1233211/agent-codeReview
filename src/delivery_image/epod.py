"""系统 EPOD 图片：按拍照时间水印与 DNR 创建时间，区分原合格 EPOD 与 DNR 投诉后的补派照。

1.5 第 3、4 行（原合格 EPOD 包裹照片、投诉后再次派送的合格包裹照片）看的是系统里的 EPOD 图片，
不是用户上传的图片，调用方用 is_epod 标记。模型只抄录水印时间，先后顺序由代码比较，不让模型猜。
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .schemas import DeliveryImageResult, EvidenceType

# 水印时间和未带时区的 dnr_created_at 都按泰国当地时间处理。
THAILAND_TZ = timezone(timedelta(hours=7))

EPOD_TYPES = frozenset({EvidenceType.PARCEL_SCENE_PHOTO, EvidenceType.REDISPATCH_DELIVERY_PHOTO})

_TIME_FINDING_PREFIX = "拍照时间识别："
_NORMALIZED_PATTERN = re.compile(r"标准化「(.*?)」")
_DATE_ONLY_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class EpodKind:
    """代码判定的 EPOD 类型，note 写入 findings 留痕。"""

    evidence_type: EvidenceType
    note: str


def photo_time_value(image: DeliveryImageResult) -> str | None:
    """取模型在「拍照时间识别」里给出的标准化时间；没有水印或无法确定返回 None。"""
    finding = next((item for item in image.findings if item.startswith(_TIME_FINDING_PREFIX)), None)
    if finding is None:
        return None
    match = _NORMALIZED_PATTERN.search(finding)
    value = match.group(1).strip() if match else ""
    return value if value and value != "无" else None


def _parse(value: str) -> datetime | date | None:
    if _DATE_ONLY_PATTERN.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=THAILAND_TZ)


def classify_epod(image: DeliveryImageResult, dnr_created_at: datetime | None) -> EpodKind:
    """水印拍照时间晚于 DNR 创建时间才是补派照；其余（含无法比较）一律按原合格 EPOD 处理。"""
    original = EvidenceType.PARCEL_SCENE_PHOTO
    if dnr_created_at is None:
        return EpodKind(original, "请求未提供 dnr_created_at，无法判断是否补派，按原合格 EPOD 处理。")
    dnr = dnr_created_at if dnr_created_at.tzinfo else dnr_created_at.replace(tzinfo=THAILAND_TZ)

    raw = photo_time_value(image)
    if raw is None:
        return EpodKind(original, "图片未见拍照时间水印，无法判断是否补派，按原合格 EPOD 处理。")
    photo = _parse(raw)
    if photo is None:
        return EpodKind(original, f"拍照时间「{raw}」无法解析，按原合格 EPOD 处理。")

    dnr_text = dnr.astimezone(THAILAND_TZ).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(photo, datetime):
        if photo > dnr:
            return EpodKind(
                EvidenceType.REDISPATCH_DELIVERY_PHOTO,
                f"拍照时间 {raw} 晚于 DNR 创建时间 {dnr_text}，判定为 DNR 投诉后的补派照。",
            )
        return EpodKind(original, f"拍照时间 {raw} 不晚于 DNR 创建时间 {dnr_text}，判定为原合格 EPOD。")

    dnr_day = dnr.astimezone(THAILAND_TZ).date()
    if photo > dnr_day:
        return EpodKind(EvidenceType.REDISPATCH_DELIVERY_PHOTO, f"拍照日期 {raw} 晚于 DNR 创建日 {dnr_day}，判定为补派照。")
    if photo < dnr_day:
        return EpodKind(original, f"拍照日期 {raw} 早于 DNR 创建日 {dnr_day}，判定为原合格 EPOD。")
    return EpodKind(original, f"水印只有日期且与 DNR 创建日同一天，无法比较先后，按原合格 EPOD 处理。")
