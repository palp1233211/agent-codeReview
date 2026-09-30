"""按输入顺序逐张审核图片，并在找到合格证据后提前结束。"""

import base64
import json
import ipaddress
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import requests

from .config import DeliveryImageConfig
from .schemas import (
    DeliveryDecision,
    DeliveryImageConclusion,
    DeliveryImageRequest,
    DeliveryImageResponseData,
    DeliveryImageResult,
    DeliveryReasonCode,
    EvidenceType,
    ImageReviewStatus,
    PolicyMatch,
)


class DeliveryImageAgent:
    """同步逐张审核图片；单张证据合格即可结束本单审核。"""

    MAX_IMAGE_BYTES = 15 * 1024 * 1024
    IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 20

    def __init__(self, config: DeliveryImageConfig, client: Any | None = None) -> None:
        self.config = config
        if client is not None:
            self.client = client
        else:
            from openai import OpenAI

            self.client = OpenAI(api_key=config.api_token, base_url=config.base_url, max_retries=0)

    def evaluate(
        self,
        request: DeliveryImageRequest,
        prompt: str,
    ) -> DeliveryImageResponseData:
        started = time.monotonic()
        # 每张图片都有独立模型超时预算；整单预算按图片数扩展，避免前序图片耗尽后续图片的时间。
        deadline = started + self.config.timeout_seconds * len(request.images) + 5
        try:
            image_results: list[DeliveryImageResult] = []
            reason_codes: list[DeliveryReasonCode] = []
            summaries: list[str] = []
            any_uncertain = False
            confidence_values: list[float] = []

            for image_index, image in enumerate(request.images, start=1):
                one_image_request = request.model_copy(update={"images": [image]})
                try:
                    self._validate_image(image)
                    image_data_url = self._fetch_image_data_url(image)
                    raw_text = self._request_model(one_image_request, prompt, deadline, image_data_url)
                    image_review = self._parse_result(raw_text, one_image_request)
                    image_review = self._enforce_policy_context(image_review, one_image_request)
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "delivery image evaluation failed request_id=%s image_id=%s error_type=%s error=%s",
                        request.request_id,
                        image.image_id,
                        type(exc).__name__,
                        str(exc)[:500],
                    )
                    image_review = self._uncertain_result(
                        one_image_request,
                        self._duration_ms(started),
                    )

                image_results.extend(image_review.image_results)
                summaries.append(f"图片{image_index}（{image.image_id}）：{image_review.summary}")
                confidence_values.append(image_review.confidence)
                for reason_code in image_review.reason_codes:
                    if reason_code not in reason_codes:
                        reason_codes.append(reason_code)

                if image_review.decision == DeliveryDecision.CORRECT:
                    remaining_images = request.images[image_index:]
                    for skipped_image in remaining_images:
                        image_results.append(
                            DeliveryImageResult(
                                image_id=skipped_image.image_id,
                                evidence_type="unknown",
                                policy_match="unknown",
                                review_status=ImageReviewStatus.SKIPPED,
                                conclusion="uncertain",
                                findings=["前序图片已单独满足本单举证要求，依规则提前通过；本图片未审核。"],
                            )
                        )
                    summaries.append(
                        f"图片{image_index}已满足对应举证要求，整单判定妥投合格；其余图片未继续审核。"
                    )
                    return self._build_response(
                        request=request,
                        decision=DeliveryDecision.CORRECT,
                        confidence=image_review.confidence,
                        reason_codes=image_review.reason_codes,
                        image_results=image_results,
                        summary=" ".join(summaries),
                        started=started,
                    )

                if image_review.decision == DeliveryDecision.UNCERTAIN:
                    any_uncertain = True

            decision = DeliveryDecision.UNCERTAIN if any_uncertain else DeliveryDecision.INCORRECT
            confidence = max(confidence_values, default=0)
            if decision == DeliveryDecision.UNCERTAIN:
                confidence = min(confidence, 0.5)
            return self._build_response(
                request=request,
                decision=decision,
                confidence=confidence,
                reason_codes=reason_codes,
                image_results=image_results,
                summary=" ".join(summaries),
                started=started,
            )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "delivery image evaluation failed request_id=%s error_type=%s error=%s",
                request.request_id,
                type(exc).__name__,
                str(exc)[:500],
            )
            return self._uncertain_result(request, self._duration_ms(started))

    def _build_response(
        self,
        request: DeliveryImageRequest,
        decision: DeliveryDecision,
        confidence: float,
        reason_codes: list[DeliveryReasonCode],
        image_results: list[DeliveryImageResult],
        summary: str,
        started: float,
    ) -> DeliveryImageResponseData:
        return DeliveryImageResponseData(
            request_id=request.request_id,
            pno=request.pno,
            platform=request.platform,
            direction=request.direction,
            decision=decision,
            confidence=confidence,
            reason_codes=reason_codes,
            image_results=image_results,
            summary=summary[:2000],
            model_version=self.config.model,
            prompt_version=self.config.prompt_version,
            duration_ms=self._duration_ms(started),
        )

    def _validate_image(self, image: Any) -> None:
        parsed = urlparse(image.url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.username or parsed.password or parsed.port:
            raise ValueError("图片 URL 不得包含用户信息或端口")
        if hostname not in self.config.allowed_hosts:
            raise ValueError("图片 URL 主机不在允许列表")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None:
            raise ValueError("图片 URL 不得直接使用 IP 地址")

    def _fetch_image_data_url(self, image: Any) -> str:
        """由 Agent 本机下载并校验图片，再以 data URL 发送给模型，避免模型侧抓取失败。"""
        response = None
        try:
            response = requests.get(
                image.url,
                timeout=self.IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
                stream=True,
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise ValueError(f"图片下载失败，HTTP 状态码={response.status_code}")

            content_length = response.headers.get("Content-Length", "")
            if content_length.isdigit() and int(content_length) > self.MAX_IMAGE_BYTES:
                raise ValueError(f"图片文件过大，超过 {self.MAX_IMAGE_BYTES} 字节限制")

            image_bytes = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                image_bytes.extend(chunk)
                if len(image_bytes) > self.MAX_IMAGE_BYTES:
                    raise ValueError(f"图片文件过大，超过 {self.MAX_IMAGE_BYTES} 字节限制")

            mime_type = self._detect_image_mime(bytes(image_bytes))
            if mime_type is None:
                content_type = response.headers.get("Content-Type", "unknown").split(";", 1)[0]
                raise ValueError(
                    f"下载内容不是受支持的 JPEG/PNG/GIF/WebP 图片，content_type={content_type}"
                )

            logging.getLogger(__name__).info(
                "delivery image downloaded image_id=%s mime_type=%s bytes=%s",
                image.image_id,
                mime_type,
                len(image_bytes),
            )
            encoded = base64.b64encode(image_bytes).decode("ascii")
            return f"data:{mime_type};base64,{encoded}"
        except requests.RequestException as exc:
            # requests 异常可能包含完整签名 URL，只记录异常类型，不泄露查询参数。
            raise ValueError(f"图片下载失败，错误类型={type(exc).__name__}") from exc
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _detect_image_mime(image_bytes: bytes) -> str | None:
        if image_bytes.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if image_bytes.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
            return "image/webp"
        return None

    def _request_model(
        self,
        request: DeliveryImageRequest,
        prompt: str,
        deadline: float | None = None,
        image_data_url: str | None = None,
    ) -> str:
        recipient_phone = request.recipient_phone or "未提供（不得据此确认手机号一致）"
        platform = request.platform.value if request.platform else "未提供"
        direction = request.direction.value if request.direction else "未提供"
        user_text = (
            f"当前国家：{request.country}\n"
            f"目标运单号 pno：{request.pno}\n"
            f"平台：{platform}\n"
            f"业务方向：{direction}\n"
            f"本单收件手机号 recipient_phone：{recipient_phone}\n"
            f"{prompt}"
        )
        return self._call_model(request, user_text, deadline, image_data_url)

    def _call_model(
        self,
        request: DeliveryImageRequest,
        user_text: str,
        deadline: float | None = None,
        image_data_url: str | None = None,
    ) -> str:
        timeout_seconds = self.config.timeout_seconds
        if deadline is not None:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise TimeoutError("妥投证据审核超过总超时限制")
            timeout_seconds = min(timeout_seconds, remaining_seconds)
        if image_data_url is None:
            raise ValueError("缺少已下载并校验的图片数据")
        if self.config.api_mode == "responses":
            response = self.client.responses.create(
                model=self.config.model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": user_text},
                            {"type": "input_image", "image_url": image_data_url},
                        ],
                    }
                ],
                timeout=timeout_seconds,
            )
            return str(getattr(response, "output_text", "") or "")

        response = self.client.chat.completions.create(
            model=self.config.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                }
            ],
            timeout=timeout_seconds,
        )
        return str(response.choices[0].message.content or "")

    def _parse_result(
        self,
        raw_text: str,
        request: DeliveryImageRequest,
    ) -> DeliveryImageResponseData:
        payload = json.loads(raw_text)
        if not isinstance(payload, dict):
            raise ValueError("模型结果必须是 JSON 对象")
        payload = self._normalize_payload(payload, request)
        payload.update(
            {
                "model_version": self.config.model,
                "prompt_version": self.config.prompt_version,
                "duration_ms": 0,
                "request_id": request.request_id,
                "pno": request.pno,
                "platform": request.platform,
                "direction": request.direction,
            }
        )
        result = DeliveryImageResponseData.model_validate(payload)
        if DeliveryReasonCode.MODEL_ERROR in result.reason_codes:
            raise ValueError("模型错误不能与确定性结论同时返回")
        if result.decision != DeliveryDecision.UNCERTAIN and result.confidence <= 0:
            raise ValueError("确定性结论必须提供大于0的置信度")
        input_ids = {image.image_id for image in request.images}
        result_ids = [image.image_id for image in result.image_results]
        if set(result_ids) != input_ids or len(result_ids) != len(input_ids):
            raise ValueError("模型逐图结果必须恰好覆盖每张输入图片")
        return result

    @staticmethod
    def _normalize_payload(payload: dict[str, Any], request: DeliveryImageRequest) -> dict[str, Any]:
        """把模型可能返回的逐图索引字段归一为内部证据契约。"""
        raw_results = payload.get("image_results")
        if not isinstance(raw_results, list):
            raise ValueError("模型结果缺少 image_results 数组")
        normalized_results: list[dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                raise ValueError("逐图结果必须是对象")
            image_id = item.get("image_id")
            image_index = item.get("image_index")
            if image_index is not None:
                if isinstance(image_index, bool):
                    raise ValueError("image_index 必须是正整数")
                if isinstance(image_index, int):
                    normalized_index = image_index
                elif isinstance(image_index, str) and re.fullmatch(r"[1-9][0-9]*", image_index):
                    normalized_index = int(image_index)
                else:
                    raise ValueError("image_index 必须是正整数")
                image_index = normalized_index
                if image_index <= 0:
                    raise ValueError("image_index 必须从1开始")
                index = image_index - 1
                if index >= len(request.images):
                    raise ValueError("image_index 超出输入图片范围")
                indexed_image_id = request.images[index].image_id
                if image_id and str(image_id) != indexed_image_id:
                    raise ValueError("image_id 与 image_index 指向不同图片")
                image_id = image_id or indexed_image_id
            if not image_id:
                raise ValueError("逐图结果缺少可关联的 image_id/image_index")
            conclusion = item.get("conclusion")
            if conclusion not in {member.value for member in DeliveryImageConclusion}:
                conclusion = DeliveryImageConclusion.UNCERTAIN.value
            findings = item.get("findings")
            if not isinstance(findings, list):
                findings = [
                    str(item[key])
                    for key in ("reason", "finding", "description")
                    if item.get(key)
                ]
            if not findings:
                raise ValueError("逐图结果 findings 不能为空")
            normalized_results.append(
                {
                    "image_id": str(image_id),
                    "evidence_type": item.get("evidence_type", "unknown"),
                    "policy_match": item.get("policy_match", "unknown"),
                    "conclusion": conclusion,
                    "findings": [str(finding) for finding in findings],
                }
            )
        payload["image_results"] = normalized_results
        return payload

    @staticmethod
    def _enforce_policy_context(
        result: DeliveryImageResponseData,
        request: DeliveryImageRequest,
    ) -> DeliveryImageResponseData:
        # 缺少规则上下文或单图结果不满足结构化通过条件时，不允许短路整单。
        notes: list[str] = []
        if result.platform is None or result.direction is None:
            missing = []
            if result.platform is None:
                missing.append("平台")
            if result.direction is None:
                missing.append("正向/退件方向")
            notes.append(f"未提供{'和'.join(missing)}，无法匹配对应 DNR 举证规则。")

        phone_evidence_types = {
            EvidenceType.CUSTOMER_CONFIRMATION_SMS,
            EvidenceType.CUSTOMER_CONFIRMATION_LINE_WECHAT,
            EvidenceType.CUSTOMER_CONFIRMATION_WHATSAPP,
            EvidenceType.CS_CALLBACK_RECORDING,
            EvidenceType.COURIER_CALL_VIDEO,
            EvidenceType.COURIER_CALL_AUDIO,
        }
        if request.recipient_phone is None and any(
            image.evidence_type in phone_evidence_types for image in result.image_results
        ):
            notes.append("客户确认类证据缺少 recipient_phone，无法核对收件人身份。")

        for image in result.image_results:
            waybill_finding = next(
                (finding for finding in image.findings if finding.startswith("运单号识别：")),
                None,
            )
            phone_finding = next(
                (finding for finding in image.findings if finding.startswith("手机号识别：")),
                None,
            )
            if not waybill_finding or not phone_finding:
                notes.append(f"图片 {image.image_id} 缺少运单号/手机号识别及比对记录。")
                continue

            if result.decision == DeliveryDecision.CORRECT and waybill_finding and "与目标 pno 比对：不一致" in waybill_finding:
                notes.append(f"图片 {image.image_id} 识别出的运单号与目标 pno 不一致。")
            if result.decision == DeliveryDecision.CORRECT and phone_finding and "与 recipient_phone 比对：不一致" in phone_finding:
                notes.append(f"图片 {image.image_id} 识别出的手机号与 recipient_phone 不一致。")
            if result.decision == DeliveryDecision.CORRECT and image.evidence_type in phone_evidence_types and phone_finding and not any(
                marker in phone_finding for marker in ("与 recipient_phone 比对：一致",)
            ):
                notes.append(f"图片 {image.image_id} 的客户确认手机号没有与 recipient_phone 明确核对一致。")

        if result.decision == DeliveryDecision.CORRECT:
            if not result.image_results or any(
                image.conclusion != DeliveryImageConclusion.VALID
                or image.policy_match not in {PolicyMatch.ACCEPTED, PolicyMatch.CONDITIONAL}
                for image in result.image_results
            ):
                notes.append("模型整体结论与单图证据类型或平台规则状态不一致。")

        if notes:
            summary = f"{result.summary.rstrip()} {' '.join(notes)} 已转待复核。".strip()
            return result.model_copy(
                update={
                    "decision": DeliveryDecision.UNCERTAIN,
                    "confidence": min(result.confidence, 0.5),
                    "summary": summary[:2000],
                    "image_results": result.image_results,
                }
            )
        return result

    def _uncertain_result(self, request: DeliveryImageRequest, duration_ms: int) -> DeliveryImageResponseData:
        return DeliveryImageResponseData(
            request_id=request.request_id,
            pno=request.pno,
            platform=request.platform,
            direction=request.direction,
            decision=DeliveryDecision.UNCERTAIN,
            confidence=0,
            reason_codes=[DeliveryReasonCode.MODEL_ERROR],
            image_results=[
                DeliveryImageResult(
                    image_id=image.image_id,
                    evidence_type="unknown",
                    policy_match="unknown",
                    conclusion="uncertain",
                )
                for image in request.images
            ],
            summary="模型调用或结果解析失败，需人工复核。",
            model_version=self.config.model,
            prompt_version=self.config.prompt_version,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _duration_ms(started: float) -> int:
        return max(0, int((time.monotonic() - started) * 1000))
