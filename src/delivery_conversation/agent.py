"""转写录音/录屏对话，核对运单号并验证客户收货表达。"""

import asyncio
import base64
import ipaddress
import json
import logging
import os
import re
import shutil
import tempfile
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from src.delivery_image.schemas import DeliveryDirection, DeliveryPlatform
from .video_phone import VideoPhoneReader, compare_phones

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


class ConversationEvidenceAgent:
    """逐个处理通话媒体；必须同时核对目标运单和客户本人收货表达。"""

    MAX_MEDIA_BYTES = 24 * 1024 * 1024

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
        deadline = started + self.config.conversation_total_timeout_seconds
        results: list[ConversationMediaResult] = []
        try:
            for media in request.media_files:
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    results.append(
                        self._uncertain_media_result(media, "TotalTimeout", "total_timeout")
                    )
                    continue
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
                            results.append(phone_result)
                            continue

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
                    analysis = await asyncio.wait_for(
                        self._analyze_transcript(
                            request,
                            prompt_template,
                            transcript,
                            timeout=min(self.config.timeout_seconds, remaining_seconds),
                        ),
                        timeout=min(self.config.timeout_seconds, remaining_seconds),
                    )
                    if time.monotonic() >= deadline:
                        raise TimeoutError("音视频审核超过总超时限制")
                    result = self._build_media_result(media, request, transcript, segments, analysis)
                    if phone_result is not None:
                        self._merge_phone_review_result(result, phone_result)
                    results.append(result)
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
                    results.append(result)

                # 音频没有视频画面；只补充“不适用”标记，不触发手机号识别。
                if media_bytes is not None and media.media_type == ConversationMediaType.AUDIO:
                    await self._append_phone_review(results[-1], request, media_bytes, suffix, deadline)

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
                or (self._requires_forward_waybill(request) and not request.forward_pno)
                or any(self._requires_manual_review(result) for result in results)
            ):
                decision = ConversationDecision.UNCERTAIN
                confidence = min((result.confidence for result in results), default=0.0)
                summary = self._manual_review_summary(request)
            else:
                decision = ConversationDecision.NOT_CONFIRMED
                confidence = max((result.confidence for result in results), default=0.0)
                summary = "音视频对话未能证明客户收到与目标运单号对应的包裹。"

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

    async def _append_phone_review(
        self, result: ConversationMediaResult, request: ConversationEvidenceRequest,
        media_bytes: bytes, suffix: str, deadline: float,
    ) -> None:
        """单独记录画面识别失败，避免抹去已经成功得到的转写与客户收货原话。"""
        if result.media_type == ConversationMediaType.AUDIO:
            result.phone_match = PhoneMatch.NOT_APPLICABLE
            result.findings.append("纯音频没有画面，未执行画面手机号识别。")
            return
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
        except Exception as exc:
            result.phone_match = PhoneMatch.UNCERTAIN
            result.confidence = min(result.confidence, 0.5)
            cause_chain = self._exception_cause_chain(exc)
            result.findings.append(
                f"视频画面手机号识别失败（{type(exc).__name__}；底层原因链：{cause_chain}），"
                "无法核验手机号。"
            )
            logging.getLogger(__name__).warning(
                "delivery conversation phone failed request_id=%s media_id=%s stage=phone_review "
                "duration_ms=%s timeout_seconds=%.2f error_type=%s cause_chain=%s",
                request.request_id,
                result.media_id,
                int((time.monotonic() - started) * 1000),
                max(0.0, budget),
                type(exc).__name__,
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
        """手机号不匹配导致跳过分析时，不把未执行的分析误算成信息不足。"""
        if (
            result.conversation_analysis_skipped
            and result.media_type == ConversationMediaType.VIDEO
            and result.phone_match == PhoneMatch.MISMATCH
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
            raise ValueError("音视频 URL 不得包含用户信息或端口")
        if hostname not in self.config.allowed_hosts:
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
                if response.status_code != 200:
                    raise ValueError(f"媒体下载失败，HTTP 状态码={response.status_code}")
                content_length = response.headers.get("Content-Length", "")
                if content_length.isdigit() and int(content_length) > self.MAX_MEDIA_BYTES:
                    raise ValueError(f"媒体文件超过 {self.MAX_MEDIA_BYTES} 字节限制")

                content = bytearray()
                download_deadline = time.monotonic() + timeout
                async for chunk in response.aiter_bytes(64 * 1024):
                    if time.monotonic() >= download_deadline:
                        raise TimeoutError("音视频下载超过总超时限制")
                    if chunk:
                        content.extend(chunk)
                        if len(content) > self.MAX_MEDIA_BYTES:
                            raise ValueError(f"媒体文件超过 {self.MAX_MEDIA_BYTES} 字节限制")

                suffix = self._detect_supported_suffix(bytes(content), media.media_type)
                if suffix is None:
                    media_type = response.headers.get("Content-Type", "unknown").split(";", 1)[0]
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
            if os.path.getsize(mono_path) > self.MAX_MEDIA_BYTES:
                raise ValueError(f"单声道音频超过 {self.MAX_MEDIA_BYTES} 字节限制")
            return await self._transcribe_with_diarization(
                mono_path,
                request_id=request_id,
                media_id=media_id,
                pno=pno,
                forward_pno=forward_pno,
                timeout=timeout,
            )

    async def _transcribe_with_diarization(
        self, audio_path: str, *, request_id: str | None, media_id: str | None,
        pno: str | None, timeout: float, forward_pno: str | None = None,
    ) -> list[dict[str, Any]]:
        """使用同步ASR专用参数，保留供应商的句子/词级说话人标签。"""
        api_token = self.config.audio_api_token or self.config.api_token
        if not api_token:
            raise RuntimeError("缺少 Qwen ASR API Token")
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
            raise RuntimeError(f"Qwen 同步转写失败 HTTP {response.status_code}")
        payload = response.json()
        output = payload.get("output") if isinstance(payload, dict) else None
        if not isinstance(output, dict):
            raise ValueError("Qwen 同步转写响应缺少 output")
        sentences = output.get("sentences")
        if not isinstance(sentences, list):
            sentence = output.get("sentence")
            sentences = [sentence] if isinstance(sentence, dict) else []
        segments = self._normalize_filetrans_segments({"transcripts": [{"sentences": sentences}]}, pno, forward_pno)
        logger = logging.getLogger(__name__)
        speaker_count = len({segment["speaker_id"] for segment in segments})
        logger.info(
            "delivery conversation sync_asr request_id=%s media_id=%s duration_ms=%s segments=%s speakers=%s",
            request_id, media_id, int((time.monotonic() - started) * 1000), len(segments), speaker_count,
        )
        if speaker_count < 2:
            logger.warning("delivery conversation single_speaker request_id=%s media_id=%s", request_id, media_id)
        return segments

    @classmethod
    def _normalize_filetrans_segments(
        cls,
        payload: dict[str, Any],
        pno: str | None,
        forward_pno: str | None = None,
    ) -> list[dict[str, Any]]:
        transcripts = payload.get("transcripts")
        if not isinstance(transcripts, list):
            raise ValueError("Qwen 转写结果缺少 transcripts")
        normalized: list[dict[str, Any]] = []
        for transcript in transcripts:
            if not isinstance(transcript, dict):
                continue
            sentences = transcript.get("sentences")
            if not isinstance(sentences, list):
                continue
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
    def _normalize_thai_waybill(text: str, pno: str | None) -> str:
        """仅在泰语 THT 音译后的尾号与目标运单尾段完全一致时纠正规范字母。"""
        if not pno:
            return text
        expected = re.sub(r"[^A-Z0-9]", "", pno.upper())
        if not expected.startswith("THT") or len(expected) <= 3:
            return text
        match = re.search(r"ที\s*เอช\s*ที\s*([A-Z0-9\s-]+)", text, re.IGNORECASE)
        if not match:
            return text
        suffix = re.sub(r"[^A-Z0-9]", "", match.group(1).upper())
        if suffix != expected[3:]:
            return text
        return f"{text[:match.start()]}{pno}{text[match.end():]}"

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
            raise ValueError("音视频预处理失败，请确认文件包含有效音轨")
        return stdout

    @staticmethod
    def _milliseconds_to_seconds(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return round(value / 1000, 3)
        return None

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
        if waybill_raw:
            normalized_candidate = self._normalize_waybill(waybill_raw)
            candidate_in_transcript = any(
                self._contains_waybill_identifier(segment["text"], normalized_candidate)
                for segment in segments
            )
            if not candidate_in_transcript:
                waybill_match = WaybillMatch.UNCERTAIN
            elif normalized_candidate in {
                self._normalize_waybill(number) for number in request.accepted_waybills()
            }:
                waybill_match = WaybillMatch.MATCHED
            else:
                waybill_match = WaybillMatch.MISMATCH
        elif waybill_mentioned is False:
            waybill_match = WaybillMatch.NOT_FOUND
        else:
            waybill_match = WaybillMatch.UNCERTAIN

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
        quote_segment = next(
            (
                segment for segment in segments
                if segment["speaker_id"] == receipt_speaker_id
                and self._contains_quote(segment["text"], receipt_quote)
            ),
            None,
        )
        if quote_segment is not None:
            receipt_role = speaker_roles.get(receipt_speaker_id, "unknown")
            # 收货关键词存在还不够：引用必须属于该说话人原文，且该说话人必须被判为客户。
            if receipt_role == "customer":
                try:
                    receipt_status = CustomerReceiptStatus(analysis.get("customer_received", "unclear"))
                except ValueError:
                    receipt_status = CustomerReceiptStatus.UNCLEAR

        if waybill_match == WaybillMatch.MATCHED and receipt_status == CustomerReceiptStatus.CONFIRMED:
            finding = f"识别运单号原文「{waybill_raw}」，与{self._matched_waybill_label(waybill_raw, request)}匹配；客户收货原话「{receipt_quote}」。"
        elif waybill_match == WaybillMatch.MISMATCH:
            finding = f"识别运单号原文「{waybill_raw}」，与{self._describe_waybill_targets(request)}不匹配。"
        elif waybill_raw and waybill_match == WaybillMatch.UNCERTAIN:
            finding = f"模型提取的运单号原文「{waybill_raw}」未能在 ASR 转写中核实，不能与{self._describe_waybill_targets(request)}确认匹配。"
        elif waybill_match == WaybillMatch.NOT_FOUND:
            finding = f"转写中未识别到包裹号，无法将对话与{self._describe_waybill_targets(request)}关联。"
        else:
            finding = f"运单号比对状态为 {waybill_match.value}；未确认客户本人明确收货。"

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

    def _matched_waybill_label(self, waybill_raw: str, request: ConversationEvidenceRequest) -> str:
        heard = self._normalize_waybill(waybill_raw)
        if request.forward_pno and heard == self._normalize_waybill(request.forward_pno):
            return f"正向单号 forward_pno「{request.forward_pno}」"
        return f"目标 pno「{request.pno}」"

    @staticmethod
    def _manual_review_summary(request: ConversationEvidenceRequest) -> str:
        if request.platform is None:
            return "未提供 platform，无法匹配对应的音视频举证规则，需人工复核。"
        if request.direction is None:
            return "未提供 direction，无法匹配正向/退件举证规则，需人工复核。"
        if ConversationEvidenceAgent._requires_forward_waybill(request) and not request.forward_pno:
            return "TT 退件要求按正向单号沟通，请求未提供 forward_pno，无法核对通话中的运单号，需人工复核。"
        return "音视频证据无法同时确认目标运单号、客户本人收货表达及视频手机号匹配，需人工复核。"

    @staticmethod
    def _is_platform_evidence_accepted(
        platform: DeliveryPlatform | None,
        media_type: ConversationMediaType,
        direction: DeliveryDirection = DeliveryDirection.FORWARD,
    ) -> bool:
        """应用《DNR举证要求》文档的准入矩阵；退件沿用 LAZADA 原规则（不适用 0910 修改）。"""
        if platform is None:
            return False
        if direction == DeliveryDirection.RETURN:
            if media_type == ConversationMediaType.VIDEO:
                return True
            return platform != DeliveryPlatform.TT
        if media_type == ConversationMediaType.VIDEO:
            return platform != DeliveryPlatform.LAZADA
        return platform in {
            DeliveryPlatform.SHOPEE,
            DeliveryPlatform.SHEIN,
            DeliveryPlatform.TMALL,
        }

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
