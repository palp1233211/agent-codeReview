"""按输入顺序逐张审核图片，并在找到合格证据后提前结束。"""

import base64
import json
import ipaddress
import logging
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import urlparse

import requests

from src.review_prompt_loader import load_review_prompt

from .config import DeliveryImageConfig
from .policy import (
    PHONE_EVIDENCE_TYPES,
    describe_conditions,
    get_rule,
    mark_unverifiable_conditions,
    verify_joint_review,
)
from .policy_matrix import EPOD_ONLY_TYPES, expected_policy_match
from .schemas import (
    ConditionCheck,
    DeliveryDecision,
    DeliveryImageConclusion,
    DeliveryImageRequest,
    DeliveryImageResponseData,
    DeliveryDirection,
    DeliveryImageResult,
    DeliveryJointReview,
    DeliveryPlatform,
    DeliveryReasonCode,
    EvidenceType,
    ImageReviewStatus,
    PolicyMatch,
)
from .epod import EPOD_TYPES, classify_epod
from .phone import phone_confirmed
from .waybill import TT_RETURN_FORWARD_PNO_TYPES, accepted_waybills, waybill_confirmed


# BITMAPCOREHEADER、INFOHEADER、V2~V5 头的字节数。
_BMP_DIB_HEADER_SIZES = frozenset({12, 40, 52, 56, 64, 108, 124})


class DeliveryImageAgent:
    """同步逐张审核图片；单张证据合格即可结束本单审核。"""

    MAX_IMAGE_BYTES = 15 * 1024 * 1024
    BMP_CONVERT_TIMEOUT_SECONDS = 20
    IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 20
    # 联合复核最多一次送入的候选图数；暂存的已下载候选图数量上限。
    MAX_JOINT_IMAGES = 5
    MAX_JOINT_STORED_IMAGES = 8

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
        joint_prompt: str | None = None,
    ) -> DeliveryImageResponseData:
        started = time.monotonic()
        # 每张图片都有独立模型超时预算，另留一次联合复核预算；整单预算按图片数扩展，
        # 避免前序图片耗尽后续图片的时间。
        deadline = started + self.config.timeout_seconds * (len(request.images) + 1) + 5
        try:
            image_results: list[DeliveryImageResult] = []
            reason_codes: list[DeliveryReasonCode] = []
            summaries: list[str] = []
            any_uncertain = False
            confidence_values: list[float] = []
            # 可能参与联合复核的图片已下载数据，避免联合复核时重复下载。
            stored_data_urls: dict[str, str] = {}

            for image_index, image in enumerate(request.images, start=1):
                one_image_request = request.model_copy(update={"images": [image]})
                try:
                    self._validate_image(image)
                    image_data_url = self._fetch_image_data_url(image)
                    raw_text = self._request_model(one_image_request, prompt, deadline, image_data_url)
                    image_review = self._parse_result(raw_text, one_image_request)
                    image_review = self._enforce_policy_context(image_review, one_image_request)
                    if (
                        len(stored_data_urls) < self.MAX_JOINT_STORED_IMAGES
                        and self._may_join_review(image_review.image_results[0])
                    ):
                        stored_data_urls[image.image_id] = image_data_url
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

            joint_review, joint_confidence, joint_summary = self._joint_review_if_needed(
                request, image_results, stored_data_urls, joint_prompt, deadline,
            )
            if joint_review is not None:
                summaries.append(joint_summary)
                if joint_review.passed:
                    decision = DeliveryDecision.CORRECT
                    confidence = joint_confidence
            return self._build_response(
                request=request,
                decision=decision,
                confidence=confidence,
                reason_codes=reason_codes,
                image_results=image_results,
                summary=" ".join(summaries),
                started=started,
                joint_review=joint_review,
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
        joint_review: DeliveryJointReview | None = None,
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
            joint_review=joint_review,
            summary=summary[:2000],
            model_version=self.config.model,
            prompt_version=self.config.prompt_version,
            duration_ms=self._duration_ms(started),
        )

    @staticmethod
    def _may_join_review(result: DeliveryImageResult) -> bool:
        """有效、规则可采纳或可作面单关联的图片，才可能参与联合复核。"""
        return (
            result.review_status == ImageReviewStatus.REVIEWED
            and result.conclusion != DeliveryImageConclusion.INVALID
            and (
                result.policy_match in {PolicyMatch.ACCEPTED, PolicyMatch.CONDITIONAL}
                or result.evidence_type == EvidenceType.WAYBILL_PHOTO
            )
        )

    def _joint_candidates(
        self,
        request: DeliveryImageRequest,
        image_results: list[DeliveryImageResult],
        stored_data_urls: dict[str, str],
    ) -> list[DeliveryImageResult]:
        """主证据（规则表允许联合核验的类型）优先，其余作为佐证，最多 MAX_JOINT_IMAGES 张。"""
        if request.platform is None or request.direction is None:
            return []

        def is_primary(result: DeliveryImageResult) -> bool:
            rule = get_rule(request.platform, request.direction, result.evidence_type)
            return rule is not None

        pool = [
            result for result in image_results
            if result.image_id in stored_data_urls and self._may_join_review(result)
        ]
        ordered = [r for r in pool if is_primary(r)] + [r for r in pool if not is_primary(r)]
        candidates = ordered[: self.MAX_JOINT_IMAGES]
        return candidates if any(is_primary(r) for r in candidates) else []

    def _joint_review_if_needed(
        self,
        request: DeliveryImageRequest,
        image_results: list[DeliveryImageResult],
        stored_data_urls: dict[str, str],
        joint_prompt: str | None,
        deadline: float,
    ) -> tuple[DeliveryJointReview | None, float, str]:
        candidates = self._joint_candidates(request, image_results, stored_data_urls)
        if not candidates:
            return None, 0.0, ""
        candidate_ids = [candidate.image_id for candidate in candidates]
        try:
            if joint_prompt is None:
                joint_prompt, _ = load_review_prompt("delivery_image_joint.yaml")
            review, confidence, model_summary = self._run_joint_review(
                request, candidates, stored_data_urls, joint_prompt, deadline,
            )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "delivery image joint review failed request_id=%s error_type=%s error=%s",
                request.request_id,
                type(exc).__name__,
                str(exc)[:500],
            )
            review = DeliveryJointReview(
                passed=False,
                candidate_image_ids=candidate_ids,
                reasons=[f"联合复核执行失败（{type(exc).__name__}），需人工复核。"],
            )
            return review, 0.0, "多图联合复核失败，需人工复核。"
        if review.passed:
            summary = f"多图联合复核通过：主证据 {review.primary_image_id}。{model_summary}"
        else:
            summary = f"多图联合复核未通过：{'；'.join(review.reasons)[:600]}"
        return review, confidence, summary

    def _run_joint_review(
        self,
        request: DeliveryImageRequest,
        candidates: list[DeliveryImageResult],
        stored_data_urls: dict[str, str],
        joint_prompt: str,
        deadline: float,
    ) -> tuple[DeliveryJointReview, float, str]:
        rules_text = "\n".join(
            f"- 以 {candidate.image_id}（{candidate.evidence_type.value}）为主证据：{describe_conditions(rule)}"
            for candidate in candidates
            if (rule := get_rule(request.platform, request.direction, candidate.evidence_type)) is not None
        )
        candidate_notes = "\n".join(
            f"- image_id={candidate.image_id}；证据类型={candidate.evidence_type.value}；"
            f"规则状态={candidate.policy_match.value}；单图结论={candidate.conclusion.value}；"
            f"缺失条件={'、'.join(candidate.missing_conditions) or '未记录'}；"
            f"可见事实：{' '.join(candidate.findings)[:800]}"
            for candidate in candidates
        )
        recipient_phone = request.recipient_phone or "未提供（不得据此确认手机号一致）"
        user_text = (
            f"当前国家：{request.country}\n"
            f"目标运单号 pno：{request.pno}\n"
            f"正向运单号 forward_pno：{request.forward_pno or '未提供'}\n"
            f"平台：{request.platform.value}\n"
            f"业务方向：{request.direction.value}\n"
            f"本单收件手机号 recipient_phone：{recipient_phone}\n"
            f"候选图片的单图审核记录：\n{candidate_notes}\n"
            f"{joint_prompt.replace('{policy_conditions}', rules_text)}"
        )
        raw_text = self._call_joint_model(
            user_text,
            [(candidate.image_id, stored_data_urls[candidate.image_id]) for candidate in candidates],
            deadline,
        )
        payload = json.loads(raw_text)
        if not isinstance(payload, dict):
            raise ValueError("联合复核结果必须是 JSON 对象")
        raw_checks = payload.get("condition_checks")
        if not isinstance(raw_checks, list):
            raise ValueError("联合复核结果缺少 condition_checks 数组")
        checks = [ConditionCheck.model_validate(item) for item in raw_checks]

        by_id = {candidate.image_id: candidate for candidate in candidates}
        primary_id = payload.get("primary_image_id")
        primary_id = str(primary_id) if primary_id is not None else None
        reasons: list[str] = []
        primary = by_id.get(primary_id or "")
        rule = (
            get_rule(request.platform, request.direction, primary.evidence_type)
            if primary is not None else None
        )
        if rule is not None:
            checks = mark_unverifiable_conditions(rule, checks)
        if primary is None:
            reasons.append("联合复核没有选出有效的主证据图片。")
        elif rule is None:
            reasons.append(f"图片 {primary.image_id} 的证据类型没有可联合核验的规则。")
        else:
            reasons.extend(verify_joint_review(
                rule=rule,
                primary_image_id=primary.image_id,
                checks=checks,
                images=by_id,
                recipient_phone_required=request.recipient_phone is not None,
                waybill_targets=lambda evidence_type: accepted_waybills(
                    pno=request.pno,
                    forward_pno=request.forward_pno,
                    platform=request.platform,
                    direction=request.direction,
                    evidence_type=evidence_type,
                ),
                phone_confirmed_by_code=lambda image: phone_confirmed(
                    image, request.recipient_phone, request.country,
                ),
            ))
        if payload.get("decision") != DeliveryDecision.CORRECT.value:
            reasons.append("联合复核的模型结论不是 correct_delivery。")

        try:
            confidence = min(max(float(payload.get("confidence", 0)), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if not reasons and confidence <= 0:
            reasons.append("联合复核通过时必须提供大于0的置信度。")
        review = DeliveryJointReview(
            passed=not reasons,
            primary_image_id=primary.image_id if primary is not None else None,
            candidate_image_ids=list(by_id),
            condition_checks=checks,
            reasons=reasons,
        )
        return review, confidence, str(payload.get("summary") or "")[:600]

    def _call_joint_model(
        self,
        user_text: str,
        images: list[tuple[str, str]],
        deadline: float,
    ) -> str:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise TimeoutError("妥投证据审核超过总超时限制")
        timeout_seconds = min(self.config.timeout_seconds, remaining_seconds)
        if self.config.api_mode == "responses":
            content: list[dict[str, Any]] = [{"type": "input_text", "text": user_text}]
            for image_id, data_url in images:
                content.append({"type": "input_text", "text": f"image_id={image_id}"})
                content.append({"type": "input_image", "image_url": data_url})
            response = self.client.responses.create(
                model=self.config.model,
                input=[{"role": "user", "content": content}],
                timeout=timeout_seconds,
            )
            return str(getattr(response, "output_text", "") or "")

        chat_content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for image_id, data_url in images:
            chat_content.append({"type": "text", "text": f"image_id={image_id}"})
            chat_content.append({"type": "image_url", "image_url": {"url": data_url}})
        response = self.client.chat.completions.create(
            model=self.config.model,
            messages=[{"role": "user", "content": chat_content}],
            timeout=timeout_seconds,
        )
        return str(response.choices[0].message.content or "")

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
                if self._looks_like_svg(bytes(image_bytes)):
                    raise ValueError("下载内容是 SVG 矢量图，当前不支持作为图片证据")
                content_type = response.headers.get("Content-Type", "unknown").split(";", 1)[0]
                raise ValueError(
                    f"下载内容不是受支持的 JPEG/PNG/GIF/WebP/BMP 图片，content_type={content_type}"
                )
            if mime_type == "image/bmp":
                # 视觉模型不接受 BMP，统一转成 PNG 后再发送。
                image_bytes = bytearray(self._convert_bmp_to_png(bytes(image_bytes)))
                mime_type = "image/png"

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
        # BMP 只有 "BM" 两字节魔数，容易误判；再校验紧随其后的 DIB 头大小。
        if len(image_bytes) >= 26 and image_bytes[:2] == b"BM":
            if struct.unpack_from("<I", image_bytes, 14)[0] in _BMP_DIB_HEADER_SIZES:
                return "image/bmp"
        return None

    @staticmethod
    def _looks_like_svg(image_bytes: bytes) -> bool:
        head = image_bytes[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head)

    def _convert_bmp_to_png(self, bmp_bytes: bytes) -> bytes:
        """用 ffmpeg 将 BMP 转为 PNG；限制输出尺寸与耗时，失败即拒绝，不把原图交给模型。"""
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            raise ValueError("BMP 转换失败：运行环境缺少 ffmpeg")
        with tempfile.TemporaryDirectory(prefix="delivery-image-") as directory:
            source = os.path.join(directory, "source.bmp")
            with open(source, "wb") as handle:
                handle.write(bmp_bytes)
            try:
                completed = subprocess.run(
                    [
                        ffmpeg_path, "-v", "error", "-nostdin", "-i", source, "-frames:v", "1",
                        "-vf", "scale='min(4096,iw)':-2", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
                    ],
                    capture_output=True,
                    timeout=self.BMP_CONVERT_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ValueError("BMP 转换失败：转换超时") from exc
        png_bytes = completed.stdout
        if completed.returncode != 0 or not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("BMP 转换失败：文件损坏或格式不受支持")
        if len(png_bytes) > self.MAX_IMAGE_BYTES:
            raise ValueError(f"BMP 转换后图片过大，超过 {self.MAX_IMAGE_BYTES} 字节限制")
        return png_bytes

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
            f"正向运单号 forward_pno：{request.forward_pno or '未提供'}\n"
            f"平台：{platform}\n"
            f"业务方向：{direction}\n"
            f"本单收件手机号 recipient_phone：{recipient_phone}\n"
            f"本图是否系统 EPOD 图片（非用户上传）：{'是' if request.images[0].is_epod else '否'}\n"
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
                    "missing_conditions": [
                        str(condition)
                        for condition in (item.get("missing_conditions") or [])
                        if isinstance(item.get("missing_conditions"), list)
                    ],
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
        epod_ids = {image.image_id for image in request.images if image.is_epod}
        result = DeliveryImageAgent._apply_epod_type(result, epod_ids, request)
        result = DeliveryImageAgent._apply_policy_matrix(result, epod_ids)
        if result.platform is None or result.direction is None:
            missing = []
            if result.platform is None:
                missing.append("平台")
            if result.direction is None:
                missing.append("正向/退件方向")
            notes.append(f"未提供{'和'.join(missing)}，无法匹配对应 DNR 举证规则。")

        phone_evidence_types = PHONE_EVIDENCE_TYPES
        if (
            request.platform == DeliveryPlatform.TT
            and request.direction == DeliveryDirection.RETURN
            and request.forward_pno is None
            and any(image.evidence_type in TT_RETURN_FORWARD_PNO_TYPES for image in result.image_results)
        ):
            notes.append("TT 退件要求按正向单号沟通，请求未提供 forward_pno，无法核对。")
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
            if result.decision == DeliveryDecision.CORRECT and not DeliveryImageAgent._waybill_confirmed(image, request):
                notes.append(
                    f"图片 {image.image_id} 抄录的运单号未能由代码核对为与目标运单号完整一致，不能单图通过。"
                )
            if result.decision == DeliveryDecision.CORRECT and phone_finding and "与 recipient_phone 比对：不一致" in phone_finding:
                notes.append(f"图片 {image.image_id} 识别出的手机号与 recipient_phone 不一致。")
            if (
                result.decision == DeliveryDecision.CORRECT
                and image.evidence_type in phone_evidence_types
                and not phone_confirmed(image, request.recipient_phone, request.country)
            ):
                notes.append(
                    f"图片 {image.image_id} 抄录的手机号未能由代码核对为与 recipient_phone 一致，不能单图通过。"
                )

        if result.decision == DeliveryDecision.CORRECT:
            # conditional 的条件无法由单图自述确认：只有 accepted 才能单图通过，
            # conditional 以及规则表要求多条件/多图配合的类型交给联合复核。
            if not result.image_results or any(
                image.conclusion != DeliveryImageConclusion.VALID
                or image.policy_match != PolicyMatch.ACCEPTED
                for image in result.image_results
            ):
                notes.append("模型整体结论与单图证据类型或平台规则状态不一致，条件类证据不能单图通过。")
            for image in result.image_results:
                rule = get_rule(request.platform, request.direction, image.evidence_type)
                if rule is not None and not rule.single_image_ok:
                    notes.append(
                        f"图片 {image.image_id} 的证据类型在当前平台/方向下需要逐项核对条件或多证据配合，不能单图通过。"
                    )

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

    @staticmethod
    def _apply_epod_type(
        result: DeliveryImageResponseData,
        epod_ids: set[str],
        request: DeliveryImageRequest,
    ) -> DeliveryImageResponseData:
        """系统 EPOD 图片是原合格 EPOD 还是补派照，由代码按水印拍照时间与 DNR 创建时间判定，不采信模型自判。"""
        updated: list[DeliveryImageResult] = []
        for image in result.image_results:
            if image.image_id not in epod_ids or image.evidence_type not in EPOD_TYPES:
                updated.append(image)
                continue
            kind = classify_epod(image, request.dnr_created_at)
            updated.append(image.model_copy(update={
                "evidence_type": kind.evidence_type,
                "findings": [*image.findings, f"EPOD 类型由代码判定：{kind.note}"],
            }))
        return result.model_copy(update={"image_results": updated})

    @staticmethod
    def _apply_policy_matrix(
        result: DeliveryImageResponseData,
        epod_ids: set[str],
    ) -> DeliveryImageResponseData:
        """规则状态以代码准入矩阵为准，模型自述的 policy_match 与之不符时校正并留痕。"""
        corrected: list[DeliveryImageResult] = []
        for image in result.image_results:
            is_epod = image.image_id in epod_ids
            expected = expected_policy_match(
                result.platform, result.direction, image.evidence_type, is_epod=is_epod,
            )
            if expected is None or expected == image.policy_match:
                corrected.append(image)
                continue
            findings = [
                *image.findings,
                f"规则状态由代码按平台/方向准入矩阵校正：模型判为 {image.policy_match.value}，"
                f"矩阵结果为 {expected.value}。",
            ]
            if image.evidence_type in EPOD_ONLY_TYPES and not is_epod:
                findings.append("原合格 EPOD、补派照只适用于系统 EPOD 图片（请求中 is_epod=true），用户上传的图片不适用。")
            corrected.append(image.model_copy(update={"policy_match": expected, "findings": findings}))
        return result.model_copy(update={"image_results": corrected})

    @staticmethod
    def _waybill_confirmed(image: DeliveryImageResult, request: DeliveryImageRequest) -> bool:
        return waybill_confirmed(
            image,
            accepted_waybills(
                pno=request.pno,
                forward_pno=request.forward_pno,
                platform=request.platform,
                direction=request.direction,
                evidence_type=image.evidence_type,
            ),
        )

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
