"""转写录音/录屏对话，核对运单号并验证客户收货表达。"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import traceback
import tempfile
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from src.delivery_image.log_context import log_context, set_log_context
from src.delivery_image.usage_log import log_usage
from src.delivery_image.schemas import DeliveryDirection, DeliveryPlatform
from .receipt_reply import is_acknowledgement_only, is_bare_affirmation, question_precedes_reply
from .waybill_match import find_in_spoken_text, match_waybill, normalize_prefix_variants
from .video_phone import MediaCommandError, NoVideoStreamError, PhoneReviewInputError, VideoPhoneReader, compare_phones

from src.delivery_image.schemas import EvidenceType, PolicyMatch
from .schemas import (
    ConversationDecision,
    ConversationEvidenceData,
    ConversationEvidenceRequest,
    ConversationMediaResult,
    ConversationMediaType,
    CustomerReceiptStatus,
    PhoneMatch,
    WaybillMatch,
)


def exception_origin(exc: BaseException) -> str:
    """异常是在哪个文件哪一行抛的，例如 video_phone.py:151 read。只含代码位置，不含任何数据，可以写日志。"""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return "-"
    project = [frame for frame in frames if f"{os.sep}src{os.sep}" in frame.filename]
    frame = (project or frames)[-1]
    return f"{os.path.basename(frame.filename)}:{frame.lineno} {frame.name}"


def validation_summary(exc: BaseException) -> str:
    """校验错误出在哪些字段、什么类型的错（缺字段、多字段、类型不对），不含字段的值（可能是手机号）。"""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return ""
    try:
        items = errors(include_input=False, include_url=False, include_context=False)
    except TypeError:
        return ""
    parts = [".".join(str(part) for part in item.get("loc", ())) + ":" + str(item.get("type", "?")) for item in items[:5]]
    return "；".join(parts)


class Transcript(list):
    """带说话人的转写句子列表；second_opinion 是第二份转写（不带目标号码、不分说话人）的纯文本，只用来核对运单号。"""

    second_opinion: str | None = None


class ConversationEvidenceAgent:
    """逐个处理通话媒体；必须同时核对目标运单和客户本人收货表达。"""

    # 源视频/音频下载上限：与 DNR 业务的文件提交上限一致（ETL 的视频压缩目标是 150 MiB），超过的本来就进不了系统。
    # 内存占用约等于文件大小，所以同一个请求里最多同时处理 MAX_PARALLEL_MEDIA 个视频，其余排队。
    MAX_MEDIA_BYTES = 150 * 1024 * 1024
    MAX_PARALLEL_MEDIA = 3
    # 对话分析超时/连接中断时最多再试一次，剩余总时限不足这个值就不再试
    ANALYSIS_RETRY_MIN_SECONDS = 10.0
    _TRANSIENT_MODEL_ERRORS = frozenset({"APITimeoutError", "APIConnectionError"})
    # 抽出来送给 Qwen 转写的单声道 WAV 上限（接口载荷限制）；5 分钟 16 kHz 单声道约 10 MB，远低于这个值
    MAX_AUDIO_BYTES = 24 * 1024 * 1024
    # 第二份转写最多等这么久：它只是辅助核对运单号，不能拖慢也不能拖垮主流程
    SECOND_OPINION_MAX_SECONDS = 90.0

    def __init__(
        self,
        config: Any,
        asr_client: Any | None = None,
        model_client: Any | None = None,
        vision_client: Any | None = None,
    ) -> None:
        self.config = config
        self.asr_client = asr_client
        self.phone_reader = VideoPhoneReader(config, self._run_media_command, vision_client)
        if model_client is None:
            from openai import AsyncOpenAI

            model_client = AsyncOpenAI(
                api_key=config.api_token,
                base_url=config.base_url,
                max_retries=0,
            )
        self.model_client = model_client

    async def evaluate_async(
        self,
        request: ConversationEvidenceRequest,
        prompt_template: str,
    ) -> ConversationEvidenceData:
        started = time.monotonic()
        set_log_context(request.request_id, request.pno)
        deadline = started + self.config.conversation_total_timeout_seconds
        results: list[ConversationMediaResult] = []
        try:
            slots = asyncio.Semaphore(self.MAX_PARALLEL_MEDIA)
            # 本次请求里已经（或正在）审核的视频：同一个 URL 或同一份内容只审一次，后面的直接复用结果
            seen: dict[str, asyncio.Future] = {}
            # gather 按传入顺序返回，results 的顺序与 media_files 一致；单个媒体的失败在 _review_media 内部消化成 uncertain 结果
            results = list(await asyncio.gather(*(
                self._review_media(media, request, prompt_template, deadline, slots, seen)
                for media in request.media_files
            )))

            for result in results:
                result.evidence_type, result.policy_match = self._policy_fields(request, result.media_type)

            supported = [result for result in results if self._is_delivery_supported(result, request)]
            if supported:
                decision = ConversationDecision.SUPPORTS_DELIVERY
                confidence = max(result.confidence for result in supported)
                if any(result.media_type == ConversationMediaType.VIDEO for result in supported):
                    summary = "至少一段视频证据满足平台准入，运单号、客户本人收货表达及画面手机号均已核验匹配。"
                else:
                    summary = "至少一段音频证据满足平台准入，运单号与客户本人收货表达均已核验。"
            elif (
                request.platform is None
                or request.direction is None
                or (
                    self._requires_forward_waybill(request)
                    and not request.forward_pno
                    and any(result.media_type == ConversationMediaType.VIDEO for result in results)
                )
                or any(self._requires_manual_review(result) for result in results)
            ):
                decision = ConversationDecision.UNCERTAIN
                confidence = min((result.confidence for result in results), default=0.0)
                summary = self._manual_review_summary(request, results)
            else:
                decision = ConversationDecision.NOT_CONFIRMED
                confidence = max((result.confidence for result in results), default=0.0)
                if all(result.media_type == ConversationMediaType.AUDIO for result in results):
                    summary = "纯音频无法核验询问客户电话号码，不作为妥投证据，未能证明客户收到包裹。"
                else:
                    summary = "音视频对话未能证明客户收到与目标运单号对应的包裹。" + self._progress_text(results)

            return ConversationEvidenceData(
                request_id=request.request_id,
                pno=request.pno,
                decision=decision,
                confidence=confidence,
                media_results=results,
                summary=summary,
                transcription_model=self.config.audio_model,
                analysis_model=self.config.model,
                prompt_version=self.config.conversation_prompt_version,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "delivery conversation request failed request_id=%s",
                request.request_id,
            )
            raise

    async def _review_media(
        self,
        media: Any,
        request: ConversationEvidenceRequest,
        prompt_template: str,
        deadline: float,
        slots: asyncio.Semaphore,
        seen: dict[str, asyncio.Future],
    ) -> ConversationMediaResult:
        if media.media_type == ConversationMediaType.AUDIO:
            # 纯音频一律不采纳：不下载、不转写、不调用模型，只记录原因。
            return self._skipped_audio_result(media)
        # 同时处理的视频数量有上限：每个视频都会把源文件整个读进内存，还要各起 ffmpeg 进程
        async with slots:
            return await self._process_video(media, request, prompt_template, deadline, seen)

    async def _process_video(
        self,
        media: Any,
        request: ConversationEvidenceRequest,
        prompt_template: str,
        deadline: float,
        seen: dict[str, asyncio.Future],
    ) -> ConversationMediaResult:
        """同一请求里 URL 相同或内容相同的视频只审一次：第一个负责审核，其余等它的结果并复用。"""
        owned: list[asyncio.Future] = []

        async def reuse_or_claim(key: str) -> ConversationMediaResult | None:
            existing = seen.get(key)
            if existing is not None:
                return await existing
            future = asyncio.get_running_loop().create_future()
            seen[key] = future
            owned.append(future)
            return None

        try:
            result = await self._review_video(media, request, prompt_template, deadline, reuse_or_claim)
        except BaseException:
            for future in owned:
                if not future.done():
                    future.cancel()
            raise
        for future in owned:
            if not future.done():
                future.set_result(result)
        return result

    def _reused_result(self, original: ConversationMediaResult, media: Any, reason: str, request_id: str) -> ConversationMediaResult:
        """后一个重复的视频直接用前一个的结论，说明里写明是复用，避免读结果的人以为审了两遍。"""
        logging.getLogger(__name__).info(
            "delivery conversation media duplicate request_id=%s media_id=%s same_as=%s reason=%s",
            request_id, media.media_id, original.media_id, reason,
        )
        copy = original.model_copy(deep=True, update={"media_id": media.media_id})
        copy.findings = [
            f"与 {original.media_id} 的{reason}，已直接复用其审核结果，未重复下载、转写和分析。",
            *copy.findings,
        ]
        return copy

    async def _review_video(
        self,
        media: Any,
        request: ConversationEvidenceRequest,
        prompt_template: str,
        deadline: float,
        reuse_or_claim: Any,
    ) -> ConversationMediaResult:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            return self._uncertain_media_result(media, "TotalTimeout", "total_timeout")
        # 同一个 URL 不用再下载一次
        reused = await reuse_or_claim("url:" + media.url)
        if reused is not None:
            return self._reused_result(reused, media, "视频地址相同", request.request_id)
        stage = "download"
        transcript = ""
        stage_started = time.monotonic()
        downloaded_bytes = 0
        media_bytes = None
        suffix = ""
        phone_result = None
        try:
            self._validate_media_url(media.url)
            media_bytes, suffix = await asyncio.wait_for(
                self._download_media_async(
                    media,
                    timeout=min(
                        self.config.media_download_timeout_seconds,
                        remaining_seconds,
                    ),
                ),
                timeout=remaining_seconds,
            )
            downloaded_bytes = len(media_bytes)
            logging.getLogger(__name__).info(
                "delivery conversation media downloaded request_id=%s media_id=%s duration_ms=%s bytes=%s",
                request.request_id,
                media.media_id,
                int((time.monotonic() - stage_started) * 1000),
                downloaded_bytes,
            )
            # 地址不同但内容完全相同（同一个视频提交了两次）：只审一次
            reused = await reuse_or_claim("sha256:" + hashlib.sha256(media_bytes).hexdigest())
            if reused is not None:
                return self._reused_result(reused, media, "视频内容完全相同", request.request_id)
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise TimeoutError("音视频审核超过总超时限制")
            if media.media_type == ConversationMediaType.VIDEO:
                stage = "phone_review"
                phone_result = ConversationMediaResult(
                    media_id=media.media_id,
                    media_type=media.media_type,
                    waybill_match=WaybillMatch.UNCERTAIN,
                    customer_receipt_status=CustomerReceiptStatus.UNCLEAR,
                )
                await self._append_phone_review(
                    phone_result, request, media_bytes, suffix, deadline,
                )
                if phone_result.phone_match != PhoneMatch.MATCHED:
                    phone_result.conversation_analysis_skipped = True
                    if phone_result.phone_match == PhoneMatch.MISMATCH:
                        phone_result.findings.append(
                            "视频通话手机号与 recipient_phone 不匹配，已跳过视频语音转写和对话分析。"
                        )
                    else:
                        phone_result.findings.append(
                            "视频通话手机号未能核验匹配，已跳过视频语音转写和对话分析。"
                        )
                    return phone_result

                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    raise TimeoutError("手机号核验后剩余时限不足，未执行视频语音转写")
            stage = "transcription"
            segments = await asyncio.wait_for(
                self._transcribe(
                    media_bytes,
                    suffix,
                    request_id=request.request_id,
                    media_id=media.media_id,
                    pno=request.pno,
                    forward_pno=self._extra_waybill(request),
                    timeout=min(self.config.audio_timeout_seconds, remaining_seconds),
                ),
                timeout=min(self.config.audio_timeout_seconds, remaining_seconds),
            )
            transcript = self._render_transcript(segments)
            if not transcript.strip():
                raise ValueError("转写结果为空")
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise TimeoutError("音视频审核超过总超时限制")
            stage = "analysis"
            analysis = await self._analyze_with_retry(
                request, prompt_template, transcript, media.media_id, deadline,
            )
            if time.monotonic() >= deadline:
                raise TimeoutError("音视频审核超过总超时限制")
            result = self._build_media_result(media, request, transcript, segments, analysis)
            if phone_result is not None:
                self._merge_phone_review_result(result, phone_result)
            return result
        except Exception as exc:
            # 不写日志异常正文，避免第三方错误中包含签名 URL、转写内容或鉴权信息。
            cause_chain = self._exception_cause_chain(exc)
            logging.getLogger(__name__).warning(
                "delivery conversation evaluation failed request_id=%s media_id=%s stage=%s stage_duration_ms=%s downloaded_bytes=%s error_type=%s cause_chain=%s",
                request.request_id,
                media.media_id,
                stage,
                int((time.monotonic() - stage_started) * 1000),
                downloaded_bytes,
                type(exc).__name__,
                cause_chain,
            )
            result = self._uncertain_media_result(
                media,
                type(exc).__name__,
                stage,
                transcript,
                cause_chain,
            )
            if phone_result is not None:
                self._merge_phone_review_result(result, phone_result)
            return result

    @staticmethod
    def _skipped_audio_result(media: Any) -> ConversationMediaResult:
        """纯音频没有画面，无法核验询问客户电话号码，业务确认一律不采纳，因此跳过全部处理。"""
        return ConversationMediaResult(
            media_id=media.media_id,
            media_type=media.media_type,
            waybill_match=WaybillMatch.UNCERTAIN,
            customer_receipt_status=CustomerReceiptStatus.UNCLEAR,
            phone_match=PhoneMatch.NOT_APPLICABLE,
            conversation_analysis_skipped=True,
            findings=["纯音频没有画面，无法核验询问客户电话号码，不作为妥投证据，已跳过下载与转写。"],
        )

    async def _append_phone_review(
        self, result: ConversationMediaResult, request: ConversationEvidenceRequest,
        media_bytes: bytes, suffix: str, deadline: float,
    ) -> None:
        """单独记录画面识别失败，避免抹去已经成功得到的转写与客户收货原话。"""
        started = time.monotonic()
        budget = min(self.config.timeout_seconds, deadline - started)
        try:
            if budget <= 0:
                raise TimeoutError("无剩余手机号识别时限")
            observations, times = await asyncio.wait_for(
                self.phone_reader.read(media_bytes, suffix, budget), timeout=budget,
            )
            result.phone_observations = observations
            result.phone_sample_times = times
            result.detected_phone_numbers = list(dict.fromkeys(item.raw_phone for item in observations))
            result.phone_match = compare_phones(observations, request.recipient_phone, request.country)
            if result.phone_match != PhoneMatch.MATCHED:
                result.confidence = min(result.confidence, 0.5)
            result.findings.append(
                "手机号核验仅依据采样画面，未见号码不代表整段视频不存在号码；"
                f"与 recipient_phone 比对：{result.phone_match.value}。"
            )
            for item in observations:
                result.findings.append(f"画面 {item.time_seconds:g}s 手机号原文「{item.raw_phone}」；通话对方号码：{item.is_call_counterparty}；完整可读：{item.readable}。")
            if not request.recipient_phone:
                result.findings.append("未提供 recipient_phone，保留识别原文但无法核对本单收件手机号。")
        except NoVideoStreamError as exc:
            # 视频类型的文件里只有音轨：这是证据本身的情况，不是处理故障，重试也不会变。
            # 纯音频无法核验画面手机号，按规则不采纳；用 not_applicable 表示，整单不会因此当成「待复核」反复重审。
            result.phone_match = PhoneMatch.NOT_APPLICABLE
            result.confidence = min(result.confidence, 0.5)
            result.findings.append("该文件没有视频画面，实际是纯音频，无法核验画面手机号；纯音频按规则不采纳。")
            logging.getLogger(__name__).info(
                "delivery conversation no_video_stream request_id=%s media_id=%s", request.request_id, result.media_id
            )
        except Exception as exc:
            result.phone_match = PhoneMatch.UNCERTAIN
            result.confidence = min(result.confidence, 0.5)
            cause_chain = self._exception_cause_chain(exc)
            # 只有视频本身的问题（时长、抽帧）才附带原因：这类消息是固定文字，不含地址和号码；其他异常消息可能含用户数据，不写
            reason = f"原因：{exc}；" if isinstance(exc, (PhoneReviewInputError, MediaCommandError)) else ""
            # 其他异常的消息可能含用户数据，只记代码位置和校验出错的字段名/类型
            origin = exception_origin(exc)
            fields = validation_summary(exc)
            result.findings.append(
                f"视频画面手机号识别失败（{type(exc).__name__}；{reason}位置：{origin}"
                f"{'；校验：' + fields if fields else ''}；底层原因链：{cause_chain}），无法核验手机号。"
            )
            logging.getLogger(__name__).warning(
                "delivery conversation phone failed request_id=%s media_id=%s stage=phone_review "
                "duration_ms=%s timeout_seconds=%.2f error_type=%s reason=%s origin=%s validation=%s cause_chain=%s",
                request.request_id,
                result.media_id,
                int((time.monotonic() - started) * 1000),
                max(0.0, budget),
                type(exc).__name__,
                str(exc) if isinstance(exc, (PhoneReviewInputError, MediaCommandError)) else "-",
                origin,
                fields or "-",
                cause_chain,
            )
        finally:
            logging.getLogger(__name__).info(
                "delivery conversation phone request_id=%s media_id=%s duration_ms=%s frames=%s match=%s",
                request.request_id, result.media_id, int((time.monotonic() - started) * 1000),
                len(result.phone_sample_times), result.phone_match.value,
            )

    @staticmethod
    def _merge_phone_review_result(
        result: ConversationMediaResult,
        phone_result: ConversationMediaResult,
    ) -> None:
        """将前置视频手机号核验结果合并到后续转写/分析结果中。"""
        result.phone_match = phone_result.phone_match
        result.detected_phone_numbers = phone_result.detected_phone_numbers
        result.phone_observations = phone_result.phone_observations
        result.phone_sample_times = phone_result.phone_sample_times
        result.findings.extend(phone_result.findings)

    @staticmethod
    def _requires_manual_review(result: ConversationMediaResult) -> bool:
        """手机号不匹配导致跳过分析时，不把未执行的分析误算成信息不足；纯音频不采纳，补充信息也不会改变结论。"""
        if result.media_type == ConversationMediaType.AUDIO:
            return False
        if (
            result.conversation_analysis_skipped
            and result.media_type == ConversationMediaType.VIDEO
            # 手机号不匹配，或文件其实是纯音频：都是确定的结论，不是「信息不足」
            and result.phone_match in (PhoneMatch.MISMATCH, PhoneMatch.NOT_APPLICABLE)
        ):
            return False
        return (
            result.waybill_match in {WaybillMatch.NOT_FOUND, WaybillMatch.UNCERTAIN}
            or result.customer_receipt_status == CustomerReceiptStatus.UNCLEAR
            or (
                result.media_type == ConversationMediaType.VIDEO
                and result.phone_match == PhoneMatch.UNCERTAIN
            )
        )

    def _validate_media_url(self, url: str) -> None:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.username or parsed.password or parsed.port:
            logging.getLogger(__name__).warning(
                "delivery media url rejected reason=userinfo_or_port %s host=%s", log_context(), hostname or "(empty)"
            )
            raise ValueError("音视频 URL 不得包含用户信息或端口")
        if hostname not in self.config.allowed_hosts:
            # 只记录域名，不记录完整地址（可能带签名参数）
            logging.getLogger(__name__).warning(
                "delivery media url rejected reason=host_not_allowed %s host=%s", log_context(), hostname or "(empty)"
            )
            raise ValueError("音视频 URL 主机不在允许列表")
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            return
        raise ValueError("音视频 URL 不得直接使用 IP 地址")

    async def _download_media_async(self, media: Any, timeout: float) -> tuple[bytes, str]:
        """网络瞬时故障最多重试两次；重试及退避共用同一个下载截止时间。"""
        deadline = time.monotonic() + timeout
        for attempt in range(1, 4):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("音视频下载超过总超时限制")
            try:
                # 每次重新下载使用独立缓冲区，不能将中断前的数据拼到下一次响应。
                return await asyncio.wait_for(
                    self._download_media_once(media, remaining), timeout=remaining,
                )
            except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                remaining = deadline - time.monotonic()
                delay = 0.5 * attempt
                if attempt == 3 or remaining <= delay:
                    raise
                logging.getLogger(__name__).warning(
                    "delivery media download retry media_id=%s attempt=%s error_type=%s remaining_seconds=%.2f",
                    media.media_id, attempt, type(exc).__name__, remaining,
                )
                await asyncio.sleep(delay)
        raise RuntimeError("媒体下载未完成")

    async def _download_media_once(self, media: Any, timeout: float) -> tuple[bytes, str]:
        request_timeout = httpx.Timeout(
            connect=min(10.0, timeout),
            read=min(30.0, timeout),
            write=min(5.0, timeout),
            pool=min(5.0, timeout),
        )
        async with httpx.AsyncClient(timeout=request_timeout, follow_redirects=False) as client:
            async with client.stream("GET", media.url) as response:
                # 异常类型（ValueError）看不出具体原因，这里单独记一行：只含状态码、大小、内容类型，不含地址（可能带签名）
                log_fields = (
                    f"media_id={media.media_id} http_status={response.status_code} "
                    f"content_type={response.headers.get('Content-Type', 'unknown').split(';', 1)[0]} "
                    f"content_length={response.headers.get('Content-Length', 'unknown')}"
                )
                if response.status_code != 200:
                    logging.getLogger(__name__).warning("delivery media download rejected reason=http_status %s %s", log_context(), log_fields)
                    raise ValueError(f"媒体下载失败，HTTP 状态码={response.status_code}")
                content_length = response.headers.get("Content-Length", "")
                if content_length.isdigit() and int(content_length) > self.MAX_MEDIA_BYTES:
                    logging.getLogger(__name__).warning("delivery media download rejected reason=too_large %s %s", log_context(), log_fields)
                    raise ValueError(f"媒体文件超过 {self.MAX_MEDIA_BYTES} 字节限制")

                content = bytearray()
                download_started = time.monotonic()
                download_deadline = download_started + timeout
                try:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        if time.monotonic() >= download_deadline:
                            raise TimeoutError("音视频下载超过总超时限制")
                        if chunk:
                            content.extend(chunk)
                            if len(content) > self.MAX_MEDIA_BYTES:
                                logging.getLogger(__name__).warning("delivery media download rejected reason=too_large %s %s", log_context(), log_fields)
                                raise ValueError(f"媒体文件超过 {self.MAX_MEDIA_BYTES} 字节限制")
                except (TimeoutError, asyncio.CancelledError, httpx.TimeoutException) as exc:
                    # 下载被超时打断：记录已经下了多少、平均速度，用来判断是网速慢还是文件太大
                    # （外层日志的 downloaded_bytes 只在下载成功后才有值，超时时恒为 0，容易误解成一个字节都没收到）
                    elapsed = max(time.monotonic() - download_started, 0.001)
                    logging.getLogger(__name__).warning(
                        "delivery media download interrupted reason=timeout %s %s downloaded_bytes=%d elapsed_s=%.1f avg_speed_kb_s=%.0f timeout_s=%.0f",
                        log_context(), log_fields, len(content), elapsed, len(content) / 1024 / elapsed, timeout,
                    )
                    raise

                suffix = self._detect_supported_suffix(bytes(content), media.media_type)
                if suffix is None:
                    media_type = response.headers.get("Content-Type", "unknown").split(";", 1)[0]
                    logging.getLogger(__name__).warning(
                        "delivery media download rejected reason=unsupported_format %s %s downloaded_bytes=%d", log_context(), log_fields, len(content)
                    )
                    raise ValueError(f"下载内容不是支持的音频/视频格式，content_type={media_type}")
                return bytes(content), suffix

    @staticmethod
    def _detect_supported_suffix(content: bytes, media_type: ConversationMediaType) -> str | None:
        if len(content) >= 12 and content[4:8] == b"ftyp":
            return ".mp4"
        if content.startswith(b"\x1aE\xdf\xa3"):
            return ".webm"
        if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"AVI ":
            return ".avi"
        if media_type == ConversationMediaType.AUDIO:
            if content.startswith(b"RIFF") and content[8:12] == b"WAVE":
                return ".wav"
            # AMR-NB 与 AMR-WB 的文件头；ffmpeg 按内容识别两者。
            if content.startswith((b"#!AMR\n", b"#!AMR-WB\n")):
                return ".amr"
            if content.startswith(b"fLaC"):
                return ".flac"
            if content.startswith(b"OggS"):
                return ".ogg"
            if content.startswith(b"ID3") or (len(content) > 1 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0):
                return ".mp3"
        return None

    async def _transcribe(
        self,
        media_bytes: bytes,
        suffix: str,
        request_id: str | None = None,
        media_id: str | None = None,
        pno: str | None = None,
        timeout: float | None = None,
        forward_pno: str | None = None,
    ) -> list[dict[str, Any]]:
        """提取16 kHz单声道音轨，调用与demo一致的同步说话人分离接口。"""
        timeout = timeout or self.config.audio_timeout_seconds
        ffmpeg_path = shutil.which("ffmpeg")
        ffprobe_path = shutil.which("ffprobe")
        if not ffmpeg_path or not ffprobe_path:
            raise RuntimeError("Qwen 说话人分离需要运行环境安装 ffmpeg 和 ffprobe")

        with tempfile.TemporaryDirectory(prefix="delivery-conversation-") as temp_dir:
            source_path = os.path.join(temp_dir, f"source{suffix}")
            mono_path = os.path.join(temp_dir, "mono.wav")
            with open(source_path, "wb") as media_file:
                media_file.write(media_bytes)

            duration_result = await self._run_media_command(
                [
                    ffprobe_path,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    source_path,
                ],
                timeout=timeout,
            )
            try:
                media_duration = float(duration_result.decode("ascii").strip())
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError("无法读取音视频时长") from exc
            if media_duration > 300:
                raise ValueError("qwen-audio-3.1-asr-flash 说话人分离仅支持 5 分钟以内音频")

            await self._run_media_command(
                [
                    ffmpeg_path,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    source_path,
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    mono_path,
                ],
                timeout=timeout,
            )
            if not os.path.isfile(mono_path) or os.path.getsize(mono_path) == 0:
                raise ValueError("音视频没有可用音轨")
            if os.path.getsize(mono_path) > self.MAX_AUDIO_BYTES:
                raise ValueError(f"单声道音频超过 {self.MAX_AUDIO_BYTES} 字节限制")
            second_task = asyncio.ensure_future(self._second_opinion_safely(mono_path, request_id, media_id, timeout))
            try:
                segments = await self._transcribe_with_diarization(
                    mono_path,
                    request_id=request_id,
                    media_id=media_id,
                    pno=pno,
                    forward_pno=forward_pno,
                    timeout=timeout,
                )
            except BaseException:
                second_task.cancel()
                raise
            transcript = Transcript(segments)
            transcript.second_opinion = await second_task
            return transcript

    async def _second_opinion_safely(
        self, audio_path: str, request_id: str | None, media_id: str | None, timeout: float,
    ) -> str | None:
        """第二份转写失败（超时、接口报错）只记日志、返回 None，不影响主转写和整单审核。"""
        try:
            return await asyncio.wait_for(
                self._second_opinion_transcript(audio_path, min(timeout, self.SECOND_OPINION_MAX_SECONDS)),
                timeout=min(timeout, self.SECOND_OPINION_MAX_SECONDS),
            )
        except Exception as exc:  # noqa: BLE001 - 只是辅助核对，任何失败都不能影响主流程
            logging.getLogger(__name__).warning(
                "delivery conversation second_opinion failed request_id=%s media_id=%s error_type=%s",
                request_id, media_id, type(exc).__name__,
            )
            return None

    async def _second_opinion_transcript(self, audio_path: str, timeout: float) -> str | None:
        """第二份转写：另一个模型（默认 qwen3-asr-flash），不带目标运单热词，不分说话人。

        不给目标号码，所以它的输出不会被目标号码带偏：它转出了目标号码，就是音频里真的有这个号码。
        没有配置第二个模型、没有 Key、或接口返回非 200 都返回 None。
        """
        model = getattr(self.config, "audio_second_model", None)
        api_token = self.config.audio_api_token
        if not model or not api_token:
            return None
        with open(audio_path, "rb") as audio_file:
            audio_data = base64.b64encode(audio_file.read()).decode("ascii")
        body = {
            "model": model,
            "input": {"messages": [{"role": "user", "content": [{"audio": "data:audio/wav;base64," + audio_data}]}]},
            "parameters": {"asr_options": {"language": "th"}},
        }
        url = self.config.audio_base_url.rstrip("/") + "/services/aigc/multimodal-generation/generation"
        headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json", "X-DashScope-SSE": "disable"}
        if self.asr_client is None:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.post(url, headers=headers, json=body, timeout=timeout)
        else:
            response = await self.asr_client.post(url, headers=headers, json=body, timeout=timeout)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"第二份转写失败 HTTP {response.status_code}")
        choices = ((response.json().get("output") or {}).get("choices")) or []
        content = ((choices[0].get("message") or {}).get("content")) if choices else None
        if isinstance(content, list):
            text = " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        else:
            text = str(content or "")
        return text.strip() or None

    async def _transcribe_with_diarization(
        self, audio_path: str, *, request_id: str | None, media_id: str | None,
        pno: str | None, timeout: float, forward_pno: str | None = None,
    ) -> list[dict[str, Any]]:
        """使用同步ASR专用参数，保留供应商的句子/词级说话人标签。"""
        # 转写只用 Qwen 这一套的 Key，不回退到 GPT 的 Key：两家的 Key 不通用，回退只会得到令人困惑的 401
        api_token = self.config.audio_api_token
        if not api_token:
            raise RuntimeError("缺少 Qwen ASR API Key（DELIVERY_QWEN_API_KEY）")
        with open(audio_path, "rb") as audio_file:
            audio_data = base64.b64encode(audio_file.read()).decode("ascii")
        parameters = {
            "format": "wav",
            "sample_rate": 16000,
            "speaker_diarization_enabled": True,
        }
        hot_words = {number: 50 for number in (pno, forward_pno) if number}
        if hot_words:
            parameters["vocabulary"] = hot_words
        body = {
            "model": self.config.audio_model,
            "input": {"messages": [{"role": "user", "content": [{
                "type": "input_audio",
                "input_audio": {"data": "data:audio/wav;base64," + audio_data},
            }]}]},
            "parameters": parameters,
        }
        url = self.config.audio_base_url.rstrip("/") + "/services/aigc/multimodal-generation/generation"
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "X-DashScope-SSE": "disable",
        }
        started = time.monotonic()
        if self.asr_client is None:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.post(url, headers=headers, json=body, timeout=timeout)
        else:
            response = await self.asr_client.post(url, headers=headers, json=body, timeout=timeout)
        if not 200 <= response.status_code < 300:
            error = RuntimeError(f"Qwen 同步转写失败 HTTP {response.status_code}")
            # 日志只记异常类型和这两项受限字段：HTTP 状态码、供应商错误码（如 InvalidApiKey），不记响应正文
            error.status_code = response.status_code  # type: ignore[attr-defined]
            error.code = self._provider_error_code_from_response(response)  # type: ignore[attr-defined]
            raise error
        payload = response.json()
        output = payload.get("output") if isinstance(payload, dict) else None
        if not isinstance(output, dict):
            raise ValueError("Qwen 同步转写响应缺少 output")
        sentences = output.get("sentences")
        if not isinstance(sentences, list):
            sentence = output.get("sentence")
            sentences = [sentence] if isinstance(sentence, dict) else []
        segments = self._normalize_sentences(sentences, pno, forward_pno)
        logger = logging.getLogger(__name__)
        speaker_count = len({segment["speaker_id"] for segment in segments})
        logger.info(
            "delivery conversation sync_asr request_id=%s media_id=%s duration_ms=%s segments=%s speakers=%s",
            request_id, media_id, int((time.monotonic() - started) * 1000), len(segments), speaker_count,
        )
        if speaker_count < 2:
            logger.warning("delivery conversation single_speaker request_id=%s media_id=%s", request_id, media_id)
        return segments

    @staticmethod
    def _provider_error_code_from_response(response: Any) -> str | None:
        """从供应商错误响应里取 code 字段；只接受格式受限的短字符串，其他一律不取。"""
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - 响应不是 JSON 时没有错误码可取
            return None
        code = body.get("code") if isinstance(body, dict) else None
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
            return code
        return None

    @classmethod
    def _normalize_sentences(
        cls,
        sentences: list[Any],
        pno: str | None,
        forward_pno: str | None = None,
    ) -> list[dict[str, Any]]:
        """同步转写返回的句子转成 [{speaker_id, start, end, text}]；句内词级说话人不止一个时按词拆开。"""
        normalized: list[dict[str, Any]] = []
        for sentence in sentences:
            if not isinstance(sentence, dict):
                continue
            words = sentence.get("words")
            if isinstance(words, list):
                word_segments = cls._split_word_speakers(sentence, words, pno, forward_pno)
                if word_segments:
                    normalized.extend(word_segments)
                    continue
            text = str(sentence.get("text") or "").strip()
            speaker_id = sentence.get("speaker_id")
            if text and speaker_id is not None:
                text = cls._normalize_thai_waybills(text, pno, forward_pno)
                normalized.append(
                    {
                        "speaker_id": str(speaker_id),
                        "start": cls._milliseconds_to_seconds(sentence.get("begin_time")),
                        "end": cls._milliseconds_to_seconds(sentence.get("end_time")),
                        "text": text,
                    }
                )
        if not normalized:
            raise ValueError("Qwen 没有返回带说话人标签的可用句子")
        return normalized

    @classmethod
    def _split_word_speakers(
        cls,
        sentence: dict[str, Any],
        words: list[Any],
        pno: str | None,
        forward_pno: str | None = None,
    ) -> list[dict[str, Any]]:
        valid_words = [
            word for word in words
            if isinstance(word, dict) and isinstance(word.get("speaker_id"), int)
        ]
        if len({word["speaker_id"] for word in valid_words}) < 2 or len(valid_words) != len(words):
            return []
        segments: list[dict[str, Any]] = []
        for word in valid_words:
            text = str(word.get("text") or "") + str(word.get("punctuation") or "")
            speaker_id = str(word["speaker_id"])
            if segments and segments[-1]["speaker_id"] == speaker_id:
                segments[-1]["text"] += text
                segments[-1]["end"] = cls._milliseconds_to_seconds(word.get("end_time"))
                continue
            segments.append(
                {
                    "speaker_id": speaker_id,
                    "start": cls._milliseconds_to_seconds(word.get("begin_time")),
                    "end": cls._milliseconds_to_seconds(word.get("end_time")),
                    "text": text,
                }
            )
        for segment in segments:
            segment["text"] = cls._normalize_thai_waybills(segment["text"], pno, forward_pno).strip()
        return segments

    @classmethod
    def _normalize_thai_waybills(cls, text: str, *numbers: str | None) -> str:
        for number in numbers:
            text = cls._normalize_thai_waybill(text, number)
        return text

    @staticmethod
    def _closest_match(raw: str, targets: list[str]) -> tuple[str, str, int] | None:
        """模型抄录的号码与候选目标的核对：先把前缀的口语变体（“T Head”）规范成目标，再按完全一致、漏字、前缀没念全的规则比对。

        返回 (目标, 种类, 漏字数)，漏字最少的优先；都对不上返回 None。
        """
        best: tuple[str, str, int] | None = None
        for target in targets:
            result = match_waybill(re.sub(r"[^A-Z0-9]", "", normalize_prefix_variants(raw, target).upper()), target)
            if result is not None and (best is None or result[1] < best[2]):
                best = (target, result[0], result[1])
        return best

    @staticmethod
    def _normalize_thai_waybill(text: str, pno: str | None) -> str:
        """前缀的口语变体（“T Head”“ที เอช ที”）转成目标号码；仅在后面的号码主体与目标完全一致时才改写。"""
        return normalize_prefix_variants(text, pno) if pno else text

    @staticmethod
    async def _run_media_command(command: list[str], *, timeout: float) -> bytes:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise TimeoutError("音视频预处理超时") from exc
        if process.returncode != 0:
            raise MediaCommandError(
                f"音视频预处理失败（{os.path.basename(str(command[0]))} 退出码 {process.returncode}）"
                f"：{ConversationEvidenceAgent._stderr_summary(_stderr)}"
            )
        return stdout

    @staticmethod
    def _stderr_summary(stderr: bytes | None) -> str:
        """取 stderr 第一行，去掉临时文件路径并截断：说明是「没有视频流」「文件损坏」还是别的，不含用户地址。"""
        text = (stderr or b"").decode("utf-8", errors="replace")
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        first = re.sub(r"/[^\s:]*delivery-[a-z]+-[^/\s:]*/", "", first)
        return first[:160] or "（无输出）"

    @staticmethod
    def _milliseconds_to_seconds(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return round(value / 1000, 3)
        return None

    async def _analyze_with_retry(
        self,
        request: ConversationEvidenceRequest,
        prompt_template: str,
        transcript: str,
        media_id: str,
        deadline: float,
    ) -> dict[str, Any]:
        """对话分析：超时或连接中断时，用已有的转写文本原地再试一次，不重新下载、核验手机号和转写（转写要付费）。"""
        log = logging.getLogger(__name__)
        started = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            timeout = min(self.config.timeout_seconds, deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("音视频审核超过总超时限制")
            attempt_started = time.monotonic()
            try:
                analysis = await asyncio.wait_for(
                    self._analyze_transcript(request, prompt_template, transcript, timeout=timeout),
                    timeout=timeout,
                )
            except Exception as exc:
                transient = isinstance(exc, TimeoutError) or type(exc).__name__ in self._TRANSIENT_MODEL_ERRORS
                can_retry = attempt == 1 and transient and deadline - time.monotonic() >= self.ANALYSIS_RETRY_MIN_SECONDS
                log.warning(
                    "delivery conversation analysis %s request_id=%s media_id=%s attempt=%s duration_ms=%s error_type=%s",
                    "retrying" if can_retry else "failed",
                    request.request_id,
                    media_id,
                    attempt,
                    int((time.monotonic() - attempt_started) * 1000),
                    type(exc).__name__,
                )
                if not can_retry:
                    raise
                continue
            log.info(
                "delivery conversation analysis request_id=%s media_id=%s attempts=%s duration_ms=%s transcript_chars=%s",
                request.request_id,
                media_id,
                attempt,
                int((time.monotonic() - started) * 1000),
                len(transcript),
            )
            return analysis

    async def _analyze_transcript(
        self,
        request: ConversationEvidenceRequest,
        prompt_template: str,
        transcript: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        timeout = timeout or self.config.timeout_seconds
        prompt = (
            prompt_template
            .replace("{country}", request.country)
            .replace("{pno}", request.pno)
            .replace("{forward_pno}", request.forward_pno or "未提供")
            .replace(
                "{direction}",
                request.direction.value if request.direction else "未提供（业务方向未知）",
            )
            .replace(
                "{platform}",
                request.platform.value if request.platform else "未提供（平台规则未知）",
            )
            .replace("{transcript}", transcript)
        )
        call_started = time.monotonic()
        if self.config.api_mode == "responses":
            response = await self.model_client.responses.create(
                model=self.config.model,
                input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                timeout=timeout,
            )
            raw_text = str(getattr(response, "output_text", "") or "")
        else:
            response = await self.model_client.chat.completions.create(
                model=self.config.model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                timeout=timeout,
            )
            raw_text = str(response.choices[0].message.content or "")
        try:
            log_usage(
                "analysis", response, self.config.model, int((time.monotonic() - call_started) * 1000),
                transcript_chars=len(transcript),
            )
        except Exception:  # noqa: BLE001 - 用量日志失败不能影响审核结果
            logging.getLogger(__name__).debug("usage log failed", exc_info=True)

        payload = json.loads(self._strip_json_fence(raw_text))
        if not isinstance(payload, dict):
            raise ValueError("对话分析结果必须是 JSON 对象")
        return payload

    def _build_media_result(
        self,
        media: Any,
        request: ConversationEvidenceRequest,
        transcript: str,
        segments: list[dict[str, Any]],
        analysis: dict[str, Any],
    ) -> ConversationMediaResult:
        waybill_raw = analysis.get("waybill_number")
        waybill_raw = str(waybill_raw).strip() if waybill_raw is not None else ""
        waybill_mentioned = analysis.get("waybill_mentioned")
        matched_waybill, dropped, match_kind = "", 0, "exact"
        targets = [self._normalize_waybill(number) for number in request.accepted_waybills()]
        if waybill_raw:
            normalized_candidate = self._normalize_waybill(waybill_raw)
            candidate_in_transcript = any(
                self._contains_waybill_identifier(segment["text"], normalized_candidate)
                for segment in segments
            )
            if not candidate_in_transcript:
                waybill_match = WaybillMatch.UNCERTAIN
            elif primary_match := self._closest_match(waybill_raw, targets):
                waybill_match = WaybillMatch.MATCHED
                matched_waybill, match_kind, dropped = primary_match
            else:
                waybill_match = WaybillMatch.MISMATCH
        elif waybill_mentioned is False:
            waybill_match = WaybillMatch.NOT_FOUND
        else:
            waybill_match = WaybillMatch.UNCERTAIN

        # 主转写（带说话人分离、带目标运单热词）偶尔会把号码转错；第二份转写不带目标号码，它转出了目标号码就是音频里真有。
        # 两份里任意一份对上就算一致（第二份只核对运单号，说话人和收货表达仍然只看主转写）
        second_note = ""
        second_opinion = getattr(segments, "second_opinion", None)
        if waybill_match != WaybillMatch.MATCHED and second_opinion:
            for target in targets:
                if found := find_in_spoken_text(second_opinion, target):
                    waybill_match, matched_waybill, dropped = WaybillMatch.MATCHED, target, 0
                    match_kind = found[0]
                    second_note = (
                        f"主转写识别的运单号是「{waybill_raw or '未识别到'}」，与目标不一致；"
                        f"第二份转写（{self.config.audio_second_model}，没有给它目标号码）核对到「{found[1]}」，与目标一致"
                        f"{'（转写中没有识别出前缀，号码主体完全一致）' if found[0] == 'prefix_omitted' else ''}，以第二份核对结果为准，建议抽查复核。"
                    )
                    waybill_raw = waybill_raw or found[1]
                    break

        speaker_roles = {}
        raw_speaker_roles = analysis.get("speaker_roles", [])
        if isinstance(raw_speaker_roles, list):
            for speaker in raw_speaker_roles:
                if isinstance(speaker, dict) and speaker.get("speaker_id"):
                    role = speaker.get("role")
                    if role in {"customer", "courier", "agent", "unknown"}:
                        normalized_speaker_id = self._normalize_speaker_id(speaker["speaker_id"])
                        speaker_roles[normalized_speaker_id] = role

        receipt_status = CustomerReceiptStatus.UNCLEAR
        receipt_role = "unknown"
        receipt_quote = str(analysis.get("receipt_quote") or "").strip()
        receipt_speaker_id = self._normalize_speaker_id(analysis.get("receipt_speaker_id") or "")
        quote_candidates = [
            segment for segment in segments
            if segment["speaker_id"] == receipt_speaker_id
            and self._contains_quote(segment["text"], receipt_quote)
        ]
        quote_segment = quote_candidates[0] if quote_candidates else None
        receipt_question = str(analysis.get("receipt_question") or "").strip()
        receipt_question_speaker_id = self._normalize_speaker_id(analysis.get("receipt_question_speaker_id") or "")
        # “ค่ะ”这类肯定词在通话里会出现很多次：模型引用的是回答那个提问的那一次，不一定是第一次出现的
        if is_bare_affirmation(receipt_quote):
            quote_segment = next(
                (
                    segment for segment in quote_candidates
                    if question_precedes_reply(segments, segment, receipt_speaker_id, receipt_question_speaker_id, receipt_question)
                ),
                quote_segment,
            )
        # 客户只回了“ค่ะ”这类肯定词时，必须是对“是否已收到包裹”这个问题的直接回复：问题要在转写里真实存在、
        # 是另一位说话人说的、紧挨在回复之前；否则这句话不能证明收到包裹
        bare_reply = is_bare_affirmation(receipt_quote)
        acknowledgement_only = is_acknowledgement_only(receipt_quote)
        question_verified = bare_reply and question_precedes_reply(
            segments, quote_segment, receipt_speaker_id, receipt_question_speaker_id, receipt_question,
        ) if quote_segment is not None else False
        if quote_segment is not None:
            receipt_role = speaker_roles.get(receipt_speaker_id, "unknown")
            # 收货关键词存在还不够：引用必须属于该说话人原文，且该说话人必须被判为客户。
            if receipt_role == "customer":
                try:
                    receipt_status = CustomerReceiptStatus(analysis.get("customer_received", "unclear"))
                except ValueError:
                    receipt_status = CustomerReceiptStatus.UNCLEAR
                if receipt_status == CustomerReceiptStatus.CONFIRMED and (acknowledgement_only or (bare_reply and not question_verified)):
                    receipt_status = CustomerReceiptStatus.UNCLEAR

        receipt_note = self._receipt_diagnosis(analysis, receipt_status, receipt_quote, receipt_speaker_id, quote_segment, speaker_roles)
        if (
            acknowledgement_only and receipt_role == "customer" and quote_segment is not None
            and str(analysis.get("customer_received") or "") == CustomerReceiptStatus.CONFIRMED.value
        ):
            receipt_note = (
                f"收货判断说明：客户只是应声「{receipt_quote}」（表示听懂了或同意，不是“已收到”），"
                "客户本人没有明确表示收到包裹，不能据此确认收货。"
            )
        elif (
            bare_reply and not question_verified and receipt_role == "customer" and quote_segment is not None
            and str(analysis.get("customer_received") or "") == CustomerReceiptStatus.CONFIRMED.value
        ):
            receipt_note = (
                f"收货判断说明：客户只回了肯定词「{receipt_quote}」，没有找到可核验的“是否已收到包裹”提问紧挨在它之前"
                "（提问必须是另一位说话人刚说过的话），不能据此确认收货。"
            )

        if waybill_match == WaybillMatch.MATCHED and receipt_status == CustomerReceiptStatus.CONFIRMED:
            finding = f"识别运单号原文「{waybill_raw}」，与{self._matched_waybill_label(matched_waybill, request)}匹配；客户收货原话「{receipt_quote}」。"
            if bare_reply:
                finding += f" 这是对提问「{receipt_question}」（是否已收到包裹）的直接回复。"
        elif waybill_match == WaybillMatch.MISMATCH:
            finding = f"识别运单号原文「{waybill_raw}」，与{self._describe_waybill_targets(request)}不匹配。"
        elif waybill_raw and waybill_match == WaybillMatch.UNCERTAIN:
            finding = f"模型提取的运单号原文「{waybill_raw}」未能在 ASR 转写中核实，不能与{self._describe_waybill_targets(request)}确认匹配。"
        elif waybill_match == WaybillMatch.NOT_FOUND:
            finding = f"转写中未识别到包裹号，无法将对话与{self._describe_waybill_targets(request)}关联。"
        else:
            finding = f"运单号比对状态为 {waybill_match.value}；未确认客户本人明确收货。"

        if waybill_match == WaybillMatch.MATCHED and dropped:
            finding += (
                f" 疑似漏听 {dropped} 位：转写的运单号比目标少 {dropped} 位字符，其余字符顺序一致，按漏念认定一致，建议抽查复核。"
            )
        elif waybill_match == WaybillMatch.MATCHED and match_kind == "prefix_omitted" and not second_note:
            finding += " 转写中没有识别出运单号前缀（可能是快递员没念，也可能是转写漏了），号码主体完全一致，按一致认定，建议抽查复核。"
        if second_note:
            finding += " " + second_note

        if request.platform is None:
            finding += " 请求未提供 platform，无法匹配平台举证规则，本次不能自动认定妥投。"
        elif request.direction is None:
            finding += " 请求未提供 direction，无法匹配正向/退件举证规则，本次不能自动认定妥投。"
        elif self._is_platform_evidence_accepted(request.platform, media.media_type, request.direction):
            finding += (
                f" 平台「{request.platform.value}」在{self._direction_label(request.direction)}方向允许该类音视频证据；"
                "仍须核验运单和客户本人收货，视频还须核对手机号。"
            )
            if self._requires_forward_waybill(request) and not request.forward_pno:
                finding += " TT 退件须按正向单号沟通，请求未提供 forward_pno，无法核对，需人工复核。"
        else:
            finding += (
                f" 平台「{request.platform.value}」在{self._direction_label(request.direction)}方向不采纳该类音视频证据，"
                "即使转写中有收货表达也不能据此认定妥投。"
            )

        if receipt_note:
            finding += " " + receipt_note

        confidence = self._safe_float(analysis.get("confidence")) or 0.0
        confidence = min(max(confidence, 0.0), 1.0)
        if not (
            waybill_match == WaybillMatch.MATCHED
            and receipt_status == CustomerReceiptStatus.CONFIRMED
            and self._is_policy_accepted(request, media.media_type)
        ):
            confidence = min(confidence, 0.5)
        return ConversationMediaResult(
            media_id=media.media_id,
            media_type=media.media_type,
            transcription=transcript[:20000],
            detected_waybill_number=waybill_raw or None,
            waybill_match=waybill_match,
            customer_receipt_status=receipt_status,
            receipt_speaker_role=receipt_role,
            receipt_quote=receipt_quote if quote_segment is not None else "",
            receipt_time_seconds=quote_segment["start"] if quote_segment else None,
            confidence=confidence,
            findings=[finding],
        )

    @staticmethod
    def _receipt_diagnosis(
        analysis: dict[str, Any],
        receipt_status: CustomerReceiptStatus,
        receipt_quote: str,
        receipt_speaker_id: str,
        quote_segment: dict[str, Any] | None,
        speaker_roles: dict[str, str],
    ) -> str:
        """客户收货没有确认时，说明是模型自己判的 unclear，还是它给了引用但没能通过校验。

        只在没确认时输出；内容是模型的判断值和校验结果（说话人编号、角色），不含转写正文。
        """
        if receipt_status == CustomerReceiptStatus.CONFIRMED:
            return ""
        claimed = str(analysis.get("customer_received") or "未给出")
        if not receipt_quote:
            return f"收货判断说明：模型的判断是 customer_received={claimed}，没有给出收货原话。"
        if quote_segment is None:
            return (
                f"收货判断说明：模型判断 customer_received={claimed}，引用了说话人 {receipt_speaker_id or '未指明'} 的一句话，"
                "但该句没有在这位说话人的转写原文里找到，不采信。"
            )
        role = speaker_roles.get(receipt_speaker_id, "unknown")
        if role != "customer":
            return (
                f"收货判断说明：模型判断 customer_received={claimed}，引用的话属于说话人 {receipt_speaker_id}，"
                f"但这位说话人的角色被判为 {role}，不是客户本人，不采信。"
            )
        return f"收货判断说明：模型判断 customer_received={claimed}，引用的话来自客户本人，但语义不是明确的已收到。"

    @staticmethod
    def _is_delivery_supported(
        result: ConversationMediaResult,
        request: ConversationEvidenceRequest,
    ) -> bool:
        """视频证据额外要求画面通话号码与本单收件手机号匹配。"""
        phone_verified = (
            result.media_type != ConversationMediaType.VIDEO
            or (
                result.phone_match == PhoneMatch.MATCHED
                and request.recipient_phone is not None
            )
        )
        return (
            result.waybill_match == WaybillMatch.MATCHED
            and result.customer_receipt_status == CustomerReceiptStatus.CONFIRMED
            and result.receipt_speaker_role == "customer"
            and phone_verified
            and ConversationEvidenceAgent._is_policy_accepted(request, result.media_type)
        )

    @staticmethod
    def _is_policy_accepted(
        request: ConversationEvidenceRequest,
        media_type: ConversationMediaType,
    ) -> bool:
        """按平台与方向的准入矩阵判断；方向缺失时规则未知，不自动采纳。"""
        return request.direction is not None and ConversationEvidenceAgent._is_platform_evidence_accepted(
            request.platform, media_type, request.direction,
        )

    @staticmethod
    def _policy_fields(
        request: ConversationEvidenceRequest,
        media_type: ConversationMediaType,
    ) -> tuple[EvidenceType, PolicyMatch]:
        """输出与图片结果对齐的证据类型和规则状态（由代码按准入矩阵决定）。"""
        evidence_type = (
            EvidenceType.COURIER_CALL_VIDEO
            if media_type == ConversationMediaType.VIDEO
            else EvidenceType.COURIER_CALL_AUDIO
        )
        if request.platform is None or request.direction is None:
            return evidence_type, PolicyMatch.UNKNOWN
        if not ConversationEvidenceAgent._is_platform_evidence_accepted(
            request.platform, media_type, request.direction,
        ):
            return evidence_type, PolicyMatch.REJECTED
        if ConversationEvidenceAgent._requires_forward_waybill(request) and not request.forward_pno:
            return evidence_type, PolicyMatch.CONDITIONAL
        return evidence_type, PolicyMatch.ACCEPTED

    @staticmethod
    def _requires_forward_waybill(request: ConversationEvidenceRequest) -> bool:
        return request.direction == DeliveryDirection.RETURN and request.platform == DeliveryPlatform.TT

    @staticmethod
    def _extra_waybill(request: ConversationEvidenceRequest) -> str | None:
        """需要一并作为 ASR 热词和核对对象的正向单号。"""
        forward = request.forward_pno
        return forward if forward and forward in request.accepted_waybills() else None

    @staticmethod
    def _direction_label(direction: DeliveryDirection | None) -> str:
        return {DeliveryDirection.FORWARD: "正向", DeliveryDirection.RETURN: "退件"}.get(direction, "未知")

    @staticmethod
    def _describe_waybill_targets(request: ConversationEvidenceRequest) -> str:
        targets = request.accepted_waybills()
        if request.direction == DeliveryDirection.RETURN and request.platform == DeliveryPlatform.TT:
            return f"正向单号 forward_pno「{request.forward_pno}」" if targets else "正向单号（请求未提供 forward_pno）"
        if len(targets) == 2:
            return f"目标 pno「{request.pno}」或正向单号 forward_pno「{request.forward_pno}」"
        return f"目标 pno「{request.pno}」"

    def _matched_waybill_label(self, matched_waybill: str, request: ConversationEvidenceRequest) -> str:
        if request.forward_pno and matched_waybill == self._normalize_waybill(request.forward_pno):
            return f"正向单号 forward_pno「{request.forward_pno}」"
        return f"目标 pno「{request.pno}」"

    @staticmethod
    def _progress_text(results: list[ConversationMediaResult]) -> str:
        """逐项写明各条件的实际状态，让人一眼看出卡在哪一项，而不是笼统地说「无法确认」。

        取视频里满足条件最多的一段来描述；纯音频不参与（按规则一律不采纳）。
        """
        videos = [result for result in results if result.media_type == ConversationMediaType.VIDEO]
        if not videos:
            return ""

        def label(ok: bool | None, ok_text: str, bad_text: str) -> str:
            return ok_text if ok else bad_text

        def items(result: ConversationMediaResult) -> list[tuple[bool, str]]:
            receipt_ok = (
                result.customer_receipt_status == CustomerReceiptStatus.CONFIRMED
                and result.receipt_speaker_role == "customer"
            )
            phone = {
                PhoneMatch.MATCHED: (True, "视频手机号：已核对一致"),
                PhoneMatch.MISMATCH: (False, "视频手机号：与收件手机号不一致"),
            }.get(result.phone_match, (False, "视频手机号：未能核验"))
            waybill = {
                WaybillMatch.MATCHED: (True, "运单号：已核对一致"),
                WaybillMatch.MISMATCH: (False, "运单号：与目标运单号不一致"),
                WaybillMatch.NOT_FOUND: (False, "运单号：通话中未提到"),
            }.get(result.waybill_match, (False, "运单号：未能核验"))
            return [
                waybill,
                phone,
                (receipt_ok, label(receipt_ok, "客户本人收货：已明确确认", "客户本人收货：未能确认（没有可核验的客户本人明确收货原话）")),
            ]

        best = max(videos, key=lambda result: sum(1 for ok, _ in items(result) if ok))
        return "（逐项结果：" + "；".join(text for _, text in items(best)) + "）"

    @staticmethod
    def _manual_review_summary(
        request: ConversationEvidenceRequest, results: list[ConversationMediaResult] | None = None,
    ) -> str:
        if request.platform is None:
            return "未提供 platform，无法匹配对应的音视频举证规则，需人工复核。"
        if request.direction is None:
            return "未提供 direction，无法匹配正向/退件举证规则，需人工复核。"
        if ConversationEvidenceAgent._requires_forward_waybill(request) and not request.forward_pno:
            return "TT 退件要求按正向单号沟通，请求未提供 forward_pno，无法核对通话中的运单号，需人工复核。"
        return "音视频证据未能全部确认，需人工复核。" + ConversationEvidenceAgent._progress_text(results or [])

    @staticmethod
    def _is_platform_evidence_accepted(
        platform: DeliveryPlatform | None,
        media_type: ConversationMediaType,
        direction: DeliveryDirection = DeliveryDirection.FORWARD,
    ) -> bool:
        """应用《DNR举证要求》文档的准入矩阵；LAZADA 与其他平台一样，只按对应方向的要求判断。

        纯音频没有画面，无法核验「询问客户电话号码一致」，业务确认一律按不采纳处理。
        """
        if platform is None or media_type == ConversationMediaType.AUDIO:
            return False
        if direction == DeliveryDirection.RETURN:
            return True
        return platform != DeliveryPlatform.LAZADA

    def _uncertain_media_result(
        self,
        media: Any,
        error_type: str,
        failure_stage: str = "unknown",
        transcription: str = "",
        cause_chain: str = "",
    ) -> ConversationMediaResult:
        stage_names = {
            "download": "媒体下载",
            "transcription": "语音转写",
            "analysis": "对话分析",
            "total_timeout": "整单超时",
            "unknown": "媒体处理",
        }
        return ConversationMediaResult(
            media_id=media.media_id,
            media_type=media.media_type,
            transcription=transcription[:20000],
            waybill_match=WaybillMatch.UNCERTAIN,
            customer_receipt_status=CustomerReceiptStatus.UNCLEAR,
            findings=[
                f"{stage_names.get(failure_stage, '媒体处理')}失败（{error_type}"
                f"{f'；底层原因链：{cause_chain}' if cause_chain else ''}），未据此判定妥投。"
            ],
        )

    @staticmethod
    def _exception_cause_chain(exception: BaseException, max_depth: int = 5) -> str:
        """只返回异常类型和数字 errno，避免把请求地址、密钥或响应正文写进日志。"""
        chain = []
        current: BaseException | None = exception
        seen: set[int] = set()
        while current is not None and len(chain) < max_depth and id(current) not in seen:
            seen.add(id(current))
            item = type(current).__name__
            error_number = getattr(current, "errno", None)
            if isinstance(error_number, int):
                item += f"(errno={error_number})"
            else:
                status_code = getattr(current, "status_code", None)
                if not isinstance(status_code, int):
                    response = getattr(current, "response", None)
                    status_code = getattr(response, "status_code", None)
                error_code = ConversationEvidenceAgent._safe_provider_error_code(current)
                diagnostics = []
                if isinstance(status_code, int):
                    diagnostics.append(f"HTTP {status_code}")
                if error_code:
                    diagnostics.append(f"code={error_code}")
                if diagnostics:
                    item += f"({', '.join(diagnostics)})"
            chain.append(item)
            current = current.__cause__ or current.__context__
        return " -> ".join(chain)

    @staticmethod
    def _safe_provider_error_code(exception: BaseException) -> str | None:
        """仅提取格式受限的供应商错误码；绝不回显错误消息或响应正文。"""
        error_code = getattr(exception, "code", None)
        if not isinstance(error_code, str):
            body = getattr(exception, "body", None)
            error = body.get("error") if isinstance(body, dict) else None
            error_code = error.get("code") if isinstance(error, dict) else None
        if isinstance(error_code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", error_code):
            return error_code
        return None

    @staticmethod
    def _render_transcript(segments: list[dict[str, Any]]) -> str:
        rendered = []
        for segment in segments:
            timestamp = ""
            if segment["start"] is not None:
                timestamp = f"[{segment['start']:.2f}s] "
            rendered.append(f"{timestamp}{segment['speaker_id']}: {segment['text']}")
        return "\n".join(rendered)

    @staticmethod
    def _normalize_speaker_id(value: Any) -> str:
        """容忍模型将 ASR 标签 1 写成 speaker_1，同时保持未知标签原样。"""
        text = str(value).strip()
        match = re.fullmatch(r"(?:speaker[_\s-]*)?(\d+)", text, flags=re.IGNORECASE)
        return match.group(1) if match else text

    @staticmethod
    def _contains_quote(segment_text: str, quote: str) -> bool:
        if not quote:
            return False
        normalize = lambda value: re.sub(r"\s+", " ", value).strip().casefold()
        return normalize(quote) in normalize(segment_text)

    @staticmethod
    def _normalize_waybill(value: str) -> str:
        return re.sub(r"[^A-Z0-9]", "", value.upper())

    @staticmethod
    def _contains_waybill_identifier(text: str, normalized_candidate: str) -> bool:
        escaped_characters = r"[^A-Za-z0-9]*".join(re.escape(character) for character in normalized_candidate)
        pattern = rf"(?<![A-Za-z0-9]){escaped_characters}(?![A-Za-z0-9])"
        return re.search(pattern, text, flags=re.IGNORECASE) is not None

    @staticmethod
    def _strip_json_fence(value: str) -> str:
        text = value.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        return text

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None
