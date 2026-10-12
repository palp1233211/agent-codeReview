"""妥投通话录音与录屏的请求和结果契约。"""

from enum import Enum
from typing import Annotated
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from src.delivery_image.schemas import DeliveryDirection, DeliveryPlatform, EvidenceType, PolicyMatch


class ConversationMediaType(str, Enum):
    AUDIO = "audio"
    VIDEO = "video"


class ConversationDecision(str, Enum):
    SUPPORTS_DELIVERY = "supports_delivery"
    NOT_CONFIRMED = "not_confirmed"
    UNCERTAIN = "uncertain"


class WaybillMatch(str, Enum):
    MATCHED = "matched"
    MISMATCH = "mismatch"
    NOT_FOUND = "not_found"
    UNCERTAIN = "uncertain"


class CustomerReceiptStatus(str, Enum):
    CONFIRMED = "confirmed"
    DENIED = "denied"
    UNCLEAR = "unclear"


class ConversationMedia(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_id: Annotated[str, Field(min_length=1, max_length=128)]
    media_type: ConversationMediaType
    url: Annotated[str, Field(min_length=1, max_length=2048)]

    @field_validator("url")
    @classmethod
    def validate_https_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("音视频 URL 必须使用 HTTPS 且包含主机名")
        return value


class ConversationEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: Annotated[str, Field(min_length=1, max_length=128)]
    pno: Annotated[str, Field(min_length=1, max_length=128)]
    country: Annotated[str, Field(min_length=2, max_length=8)]
    platform: DeliveryPlatform | None = None
    # 与图片审核一致：缺失时无法匹配正向/退件规则，结论降级为 uncertain。
    direction: DeliveryDirection | None = None
    # 退件单与客户沟通时可能使用正向运单号（TT 退件要求按正向单号沟通），作为辅助核对号码。
    forward_pno: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    recipient_phone: Annotated[str | None, Field(min_length=5, max_length=32)] = None
    # 单个同步请求最多20段媒体，避免一次调用无限占用API worker。
    media_files: Annotated[list[ConversationMedia], Field(min_length=1, max_length=20)]

    @field_validator("platform", mode="before")
    @classmethod
    def normalize_platform_case(cls, value: str | None) -> str | None:
        """平台代码大小写不敏感，与图片审核接口保持一致。"""
        if isinstance(value, str):
            return value.strip().upper()
        return value

    def accepted_waybills(self) -> list[str]:
        """通话中可作为本单运单号核对的号码：退件可用正向单号；TT 退件只认正向单号。"""
        if self.direction != DeliveryDirection.RETURN:
            return [self.pno]
        if self.platform == DeliveryPlatform.TT:
            return [self.forward_pno] if self.forward_pno else []
        return [self.pno, *([self.forward_pno] if self.forward_pno else [])]

    @field_validator("media_files")
    @classmethod
    def validate_unique_media_ids(cls, value: list[ConversationMedia]) -> list[ConversationMedia]:
        media_ids = [media.media_id for media in value]
        if len(media_ids) != len(set(media_ids)):
            raise ValueError("media_id 必须唯一")
        return value


class SpeakerRole(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker_id: str
    role: Annotated[str, Field(pattern="^(customer|courier|agent|unknown)$")]


class PhoneMatch(str, Enum):
    MATCHED = "matched"
    MISMATCH = "mismatch"
    UNCERTAIN = "uncertain"
    NOT_APPLICABLE = "not_applicable"


class PhoneObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_index: int = Field(ge=1)
    time_seconds: float = Field(ge=0)
    raw_phone: str = Field(min_length=1, max_length=64)
    is_call_counterparty: StrictBool
    readable: StrictBool


class ConversationMediaResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_id: str
    media_type: ConversationMediaType
    # 与图片结果对齐：证据类型由媒体类型决定，policy_match 由代码按平台/方向准入矩阵给出，非模型输出。
    evidence_type: EvidenceType = EvidenceType.UNKNOWN
    policy_match: PolicyMatch = PolicyMatch.UNKNOWN
    transcription: Annotated[str, Field(max_length=20000)] = ""
    phone_match: PhoneMatch = PhoneMatch.UNCERTAIN
    detected_phone_numbers: list[str] = Field(default_factory=list)
    phone_observations: list[PhoneObservation] = Field(default_factory=list)
    phone_sample_times: list[float] = Field(default_factory=list)
    conversation_analysis_skipped: bool = False
    detected_waybill_number: str | None = None
    waybill_match: WaybillMatch
    customer_receipt_status: CustomerReceiptStatus
    receipt_speaker_role: Annotated[str, Field(pattern="^(customer|courier|agent|unknown)$")] = "unknown"
    receipt_quote: Annotated[str, Field(max_length=1000)] = ""
    receipt_time_seconds: float | None = Field(default=None, ge=0)
    confidence: Annotated[float, Field(ge=0, le=1)] = 0
    findings: list[str] = Field(default_factory=list)


class ConversationEvidenceData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    pno: str
    decision: ConversationDecision
    confidence: Annotated[float, Field(ge=0, le=1)]
    media_results: list[ConversationMediaResult] = Field(default_factory=list)
    summary: Annotated[str, Field(max_length=2000)] = ""
    transcription_model: Annotated[str, Field(min_length=1, max_length=128)]
    analysis_model: Annotated[str, Field(min_length=1, max_length=128)]
    prompt_version: Annotated[str, Field(min_length=1, max_length=128)]
    duration_ms: Annotated[int, Field(ge=0)]


class ConversationEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: int = 0
    message: str = "success"
    data: ConversationEvidenceData
