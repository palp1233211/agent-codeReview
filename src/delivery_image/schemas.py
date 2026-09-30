"""妥投图片审核 Agent 的输入输出契约。"""

from enum import Enum
from typing import Annotated
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DeliveryDecision(str, Enum):
    CORRECT = "correct_delivery"
    INCORRECT = "incorrect_delivery"
    UNCERTAIN = "uncertain"


class DeliveryReasonCode(str, Enum):
    PARCEL_VISIBLE = "PARCEL_VISIBLE"
    WAYBILL_MATCHED = "WAYBILL_MATCHED"
    WAYBILL_MISMATCH = "WAYBILL_MISMATCH"
    VALID_DELIVERY_SCENE = "VALID_DELIVERY_SCENE"
    VEHICLE_SCENE = "VEHICLE_SCENE"
    DELIVERY_STATION_SCENE = "DELIVERY_STATION_SCENE"
    NO_PARCEL = "NO_PARCEL"
    BLURRY_IMAGE = "BLURRY_IMAGE"
    LABEL_UNREADABLE = "LABEL_UNREADABLE"
    IRRELEVANT_IMAGE = "IRRELEVANT_IMAGE"
    MODEL_ERROR = "MODEL_ERROR"


class DeliveryImageConclusion(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNCERTAIN = "uncertain"


class DeliveryPlatform(str, Enum):
    TT = "TT"
    SHOPEE = "SHOPEE"
    LAZADA = "LAZADA"
    SHEIN = "SHEIN"
    TMALL = "TMALL"


class DeliveryDirection(str, Enum):
    FORWARD = "forward"
    RETURN = "return"


class EvidenceType(str, Enum):
    WAYBILL_PHOTO = "waybill_photo"
    PARCEL_SCENE_PHOTO = "parcel_scene_photo"
    CUSTOMER_CONFIRMATION_SMS = "customer_confirmation_sms"
    CUSTOMER_CONFIRMATION_LINE_WECHAT = "customer_confirmation_line_wechat"
    CUSTOMER_CONFIRMATION_WHATSAPP = "customer_confirmation_whatsapp"
    CS_CALLBACK_RECORDING = "cs_callback_recording"
    COURIER_CALL_VIDEO = "courier_call_video"
    COURIER_CALL_AUDIO = "courier_call_audio"
    REDISPATCH_DELIVERY_PHOTO = "redispatch_delivery_photo"
    OTHER = "other"
    UNKNOWN = "unknown"


class PolicyMatch(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CONDITIONAL = "conditional"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class ImageReviewStatus(str, Enum):
    REVIEWED = "reviewed"
    SKIPPED = "skipped"


class DeliveryImage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_id: Annotated[str, Field(min_length=1, max_length=128)]
    url: Annotated[str, Field(min_length=1, max_length=2048)]

    @field_validator("url")
    @classmethod
    def validate_https_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("图片 URL 必须使用 HTTPS 且包含主机名")
        return value


class DeliveryImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: Annotated[str, Field(min_length=1, max_length=128)]
    pno: Annotated[str, Field(min_length=1, max_length=128)]
    country: Annotated[str, Field(min_length=2, max_length=8)]
    platform: DeliveryPlatform | None = None
    direction: DeliveryDirection | None = None
    # 收件手机号由业务调用方提供，供模型从聊天截图中识别后进行身份核对。
    recipient_phone: Annotated[str | None, Field(min_length=5, max_length=32)] = None
    # 图片数量不设上限；Agent 会按顺序逐张审核，满足条件后提前停止。
    images: Annotated[list[DeliveryImage], Field(min_length=1)]

    @field_validator("platform", mode="before")
    @classmethod
    def normalize_platform_case(cls, value: str | None) -> str | None:
        """平台代码大小写不敏感，解析后统一使用枚举中的大写值。"""
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("images")
    @classmethod
    def validate_unique_image_ids(cls, value: list[DeliveryImage]) -> list[DeliveryImage]:
        image_ids = [image.image_id for image in value]
        if len(image_ids) != len(set(image_ids)):
            raise ValueError("image_id 必须唯一")
        return value


class DeliveryImageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_id: str
    evidence_type: EvidenceType = EvidenceType.UNKNOWN
    policy_match: PolicyMatch = PolicyMatch.UNKNOWN
    review_status: ImageReviewStatus = ImageReviewStatus.REVIEWED
    conclusion: DeliveryImageConclusion
    findings: list[str] = Field(default_factory=list)


class DeliveryImageResponseData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    pno: str
    platform: DeliveryPlatform | None = None
    direction: DeliveryDirection | None = None
    decision: DeliveryDecision
    confidence: Annotated[float, Field(ge=0, le=1)]
    reason_codes: list[DeliveryReasonCode] = Field(default_factory=list)
    image_results: list[DeliveryImageResult] = Field(default_factory=list)
    summary: Annotated[str, Field(max_length=2000)] = ""
    model_version: Annotated[str, Field(min_length=1, max_length=128)]
    prompt_version: Annotated[str, Field(min_length=1, max_length=128)]
    duration_ms: Annotated[int, Field(ge=0)]


class DeliveryImageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: int = 0
    message: str = "success"
    data: DeliveryImageResponseData
