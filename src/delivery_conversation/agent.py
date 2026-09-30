"""转写录音/录屏对话，核对运单号并验证客户收货表达。"""

import asyncio
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from src.delivery_image.schemas import DeliveryPlatform

from .schemas import (
    ConversationDecision,
    ConversationEvidenceData,
    ConversationEvidenceRequest,
    ConversationMediaResult,
    ConversationMediaType,
    CustomerReceiptStatus,
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
    ) -> None:
        self.config = config
        self.asr_client = asr_client
        if model_client is None:
            from openai import AsyncOpenAI

            model_client = AsyncOpenAI(
                api_key=(
                    config.conversation_analysis_api_token
                    or config.audio_api_token
                    or config.api_token
                ),
                base_url=config.conversation_analysis_base_url,
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
                    stage = "transcription"
                    segments = await asyncio.wait_for(
                        self._transcribe(
                            media_bytes,
                            suffix,
                            request_id=request.request_id,
                            media_id=media.media_id,
                            pno=request.pno,
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
                    results.append(self._build_media_result(media, request, transcript, segments, analysis))
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
                    results.append(
                        self._uncertain_media_result(
                            media,
                            type(exc).__name__,
                            stage,
                            transcript,
                            cause_chain,
                        )
                    )

            supported = [
                result for result in results
                if result.waybill_match == WaybillMatch.MATCHED
                and result.customer_receipt_status == CustomerReceiptStatus.CONFIRMED
                and result.receipt_speaker_role == "customer"
                and self._is_platform_evidence_accepted(request.platform, result.media_type)
            ]
            if supported:
                decision = ConversationDecision.SUPPORTS_DELIVERY
                confidence = max(result.confidence for result in supported)
                summary = "至少一段对话提到与目标运单号一致的包裹，并由客户本人明确确认收到。"
            elif request.platform is None or any(
                result.waybill_match in {WaybillMatch.NOT_FOUND, WaybillMatch.UNCERTAIN}
                or result.customer_receipt_status == CustomerReceiptStatus.UNCLEAR
                for result in results
            ):
                decision = ConversationDecision.UNCERTAIN
                confidence = min((result.confidence for result in results), default=0.0)
                summary = (
                    "未提供 platform，无法匹配对应的音视频举证规则，需人工复核。"
                    if request.platform is None
                    else "音视频证据无法同时确认目标运单号和客户本人收货表达，需人工复核。"
                )
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
                transcription_model=self.config.audio_filetrans_model,
                analysis_model=self.config.conversation_analysis_model,
                prompt_version=self.config.conversation_prompt_version,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "delivery conversation request failed request_id=%s",
                request.request_id,
            )
            raise

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
        request_timeout = httpx.Timeout(
            connect=min(10.0, timeout),
            read=min(15.0, timeout),
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
        if media_type == ConversationMediaType.AUDIO:
            if content.startswith(b"RIFF") and content[8:12] == b"WAVE":
                return ".wav"
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
    ) -> list[dict[str, Any]]:
        """提取单声道音轨，上传到 Qwen 临时 OSS 后调用 Filetrans 说话人分离。"""
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
            return await self._transcribe_with_filetrans(
                mono_path,
                request_id=request_id,
                media_id=media_id,
                pno=pno,
                timeout=timeout,
            )

    async def _transcribe_with_filetrans(
        self,
        audio_path: str,
        *,
        request_id: str | None,
        media_id: str | None,
        pno: str | None,
        timeout: float,
    ) -> list[dict[str, Any]]:
        """按示例脚本的流程上传本地音频到 Qwen 临时 OSS 并轮询 Filetrans 结果。"""
        api_token = self.config.audio_api_token or self.config.api_token
        if not api_token:
            raise RuntimeError("缺少 Qwen ASR API Token")

        if self.asr_client is None:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                return await self._run_filetrans(
                    client, audio_path, api_token, pno, timeout, request_id, media_id
                )
        return await self._run_filetrans(
            self.asr_client,
            audio_path,
            api_token,
            pno,
            timeout,
            request_id,
            media_id,
        )

    async def _run_filetrans(
        self,
        client: Any,
        audio_path: str,
        api_token: str,
        pno: str | None,
        timeout: float,
        request_id: str | None,
        media_id: str | None,
    ) -> list[dict[str, Any]]:
        base_url = self.config.audio_base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_token}"}
        deadline = time.monotonic() + timeout
        stage_started = time.monotonic()
        policy_response = await client.get(
            f"{base_url}/uploads",
            params={
                "action": "getPolicy",
                "model": self.config.audio_filetrans_model,
            },
            headers=headers,
            timeout=self._remaining_timeout(deadline),
        )
        policy_payload = self._filetrans_json(policy_response, "获取上传凭证")
        logger = logging.getLogger(__name__)
        logger.info(
            "delivery conversation filetrans stage=upload_policy request_id=%s media_id=%s duration_ms=%s",
            request_id,
            media_id,
            int((time.monotonic() - stage_started) * 1000),
        )
        policy = policy_payload.get("data")
        if not isinstance(policy, dict):
            raise ValueError("Qwen 上传凭证响应缺少 data 对象")

        try:
            max_size_bytes = int(float(policy["max_file_size_mb"]) * 1024 * 1024)
            upload_host = str(policy["upload_host"])
            upload_dir = str(policy["upload_dir"])
            object_key = f"{upload_dir.rstrip('/')}/{os.path.basename(audio_path)}"
            upload_fields = {
                "OSSAccessKeyId": str(policy["oss_access_key_id"]),
                "Signature": str(policy["signature"]),
                "policy": str(policy["policy"]),
                "x-oss-object-acl": str(policy["x_oss_object_acl"]),
                "x-oss-forbid-overwrite": str(policy["x_oss_forbid_overwrite"]),
                "key": object_key,
                "success_action_status": "200",
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Qwen 上传凭证缺少必要字段") from exc

        audio_size = os.path.getsize(audio_path)
        if audio_size <= 0:
            raise ValueError("提取的音频文件为空")
        if audio_size > max_size_bytes:
            raise ValueError("提取的音频超过 Qwen 临时 OSS 上传大小限制")
        self._validate_provider_https_url(upload_host, "Qwen 临时 OSS 上传地址")

        content_type = mimetypes.guess_type(audio_path)[0] or "audio/wav"
        stage_started = time.monotonic()
        with open(audio_path, "rb") as audio_file:
            upload_response = await client.post(
                upload_host,
                data=upload_fields,
                files={"file": (os.path.basename(audio_path), audio_file, content_type)},
                timeout=self._remaining_timeout(deadline),
            )
        if upload_response.status_code != 200:
            raise RuntimeError(f"上传音频到 Qwen 临时 OSS 失败 HTTP {upload_response.status_code}")
        logger.info(
            "delivery conversation filetrans stage=audio_upload request_id=%s media_id=%s duration_ms=%s bytes=%s",
            request_id,
            media_id,
            int((time.monotonic() - stage_started) * 1000),
            audio_size,
        )

        audio_url = f"oss://{object_key}"
        parameters: dict[str, Any] = {
            "channel_id": [0],
            "diarization_enabled": True,
            "speaker_count": 2,
        }
        if pno:
            parameters["vocabulary"] = {pno: 50}
        stage_started = time.monotonic()
        task_response = await client.post(
            f"{base_url}/services/audio/asr/transcription",
            headers={
                **headers,
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
                "X-DashScope-OssResourceResolve": "enable",
            },
            json={
                "model": self.config.audio_filetrans_model,
                "input": {"file_urls": [audio_url]},
                "parameters": parameters,
            },
            timeout=self._remaining_timeout(deadline),
        )
        task_payload = self._filetrans_json(task_response, "提交转写任务")
        logger.info(
            "delivery conversation filetrans stage=task_submit request_id=%s media_id=%s duration_ms=%s",
            request_id,
            media_id,
            int((time.monotonic() - stage_started) * 1000),
        )
        output = task_payload.get("output")
        task_id = output.get("task_id") if isinstance(output, dict) else None
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise ValueError("Qwen Filetrans 响应缺少有效 task_id")

        task_url = f"{base_url}/tasks/{task_id}"
        poll_count = 0
        stage_started = time.monotonic()
        while True:
            remaining = self._remaining_timeout(deadline)
            task_status_response = await client.get(
                task_url,
                headers=headers,
                timeout=remaining,
            )
            status_payload = self._filetrans_json(task_status_response, "查询转写任务")
            task_output = status_payload.get("output")
            if not isinstance(task_output, dict):
                raise ValueError("Qwen Filetrans 任务响应缺少 output 对象")
            task_status = str(task_output.get("task_status", "UNKNOWN")).upper()
            poll_count += 1
            if task_status in {"FAILED", "UNKNOWN"}:
                error_code = task_output.get("code", task_status)
                safe_code = str(error_code)
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", safe_code):
                    safe_code = task_status
                raise RuntimeError(f"Qwen Filetrans 转写失败：{safe_code}")
            if task_status == "SUCCEEDED":
                transcript_url = self._filetrans_result_url(task_output)
                logger.info(
                    "delivery conversation filetrans stage=task_poll request_id=%s media_id=%s duration_ms=%s polls=%s status=%s",
                    request_id,
                    media_id,
                    int((time.monotonic() - stage_started) * 1000),
                    poll_count,
                    task_status,
                )
                break
            await asyncio.sleep(min(2.0, self._remaining_timeout(deadline)))

        self._validate_provider_https_url(transcript_url, "Qwen Filetrans 转写结果地址")
        transcript_response = await client.get(
            transcript_url,
            timeout=self._remaining_timeout(deadline),
        )
        transcript_payload = self._filetrans_json(transcript_response, "下载转写结果")
        return self._normalize_filetrans_segments(transcript_payload, pno)

    @staticmethod
    def _remaining_timeout(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Qwen Filetrans 超过音频审核时限")
        return remaining

    @staticmethod
    def _validate_provider_https_url(url: str, description: str) -> None:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username
            or parsed.password
            or parsed.port
            or hostname in {"localhost", "localhost.localdomain"}
            or "." not in hostname
        ):
            raise ValueError(f"{description}无效")
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            return
        raise ValueError(f"{description}不得使用 IP 地址")

    @staticmethod
    def _filetrans_json(response: Any, stage: str) -> dict[str, Any]:
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"Qwen Filetrans {stage}失败 HTTP {response.status_code}")
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"Qwen Filetrans {stage}响应不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Qwen Filetrans {stage}响应必须是 JSON 对象")
        return payload

    @staticmethod
    def _filetrans_result_url(task_output: dict[str, Any]) -> str:
        results = task_output.get("results")
        if isinstance(results, list):
            for result in results:
                if (
                    isinstance(result, dict)
                    and result.get("subtask_status", "SUCCEEDED") == "SUCCEEDED"
                    and isinstance(result.get("transcription_url"), str)
                ):
                    return result["transcription_url"]
        result = task_output.get("result")
        if isinstance(result, dict) and isinstance(result.get("transcription_url"), str):
            return result["transcription_url"]
        raise ValueError("Qwen Filetrans 任务成功但响应中没有转写结果地址")

    @classmethod
    def _normalize_filetrans_segments(
        cls,
        payload: dict[str, Any],
        pno: str | None,
    ) -> list[dict[str, Any]]:
        transcripts = payload.get("transcripts")
        if not isinstance(transcripts, list):
            raise ValueError("Qwen Filetrans 转写结果缺少 transcripts")
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
                    word_segments = cls._split_word_speakers(sentence, words, pno)
                    if word_segments:
                        normalized.extend(word_segments)
                        continue
                text = str(sentence.get("text") or "").strip()
                speaker_id = sentence.get("speaker_id")
                if text and speaker_id is not None:
                    text = cls._normalize_thai_waybill(text, pno)
                    normalized.append(
                        {
                            "speaker_id": str(speaker_id),
                            "start": cls._milliseconds_to_seconds(sentence.get("begin_time")),
                            "end": cls._milliseconds_to_seconds(sentence.get("end_time")),
                            "text": text,
                        }
                    )
        if not normalized:
            raise ValueError("Qwen Filetrans 没有返回带说话人标签的可用句子")
        return normalized

    @classmethod
    def _split_word_speakers(
        cls,
        sentence: dict[str, Any],
        words: list[Any],
        pno: str | None,
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
            segment["text"] = cls._normalize_thai_waybill(segment["text"], pno).strip()
        return segments

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
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
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
            .replace(
                "{platform}",
                request.platform.value if request.platform else "未提供（平台规则未知）",
            )
            .replace("{transcript}", transcript)
        )
        if self.config.conversation_analysis_api_mode == "responses":
            response = await self.model_client.responses.create(
                model=self.config.conversation_analysis_model,
                input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                timeout=timeout,
            )
            raw_text = str(getattr(response, "output_text", "") or "")
        else:
            response = await self.model_client.chat.completions.create(
                model=self.config.conversation_analysis_model,
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
            elif normalized_candidate == self._normalize_waybill(request.pno):
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
                        speaker_roles[str(speaker["speaker_id"])] = role

        receipt_status = CustomerReceiptStatus.UNCLEAR
        receipt_role = "unknown"
        receipt_quote = str(analysis.get("receipt_quote") or "").strip()
        receipt_speaker_id = str(analysis.get("receipt_speaker_id") or "")
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
            finding = f"识别运单号原文「{waybill_raw}」，与目标 pno「{request.pno}」匹配；客户收货原话「{receipt_quote}」。"
        elif waybill_match == WaybillMatch.MISMATCH:
            finding = f"识别运单号原文「{waybill_raw}」，与目标 pno「{request.pno}」不匹配。"
        elif waybill_raw and waybill_match == WaybillMatch.UNCERTAIN:
            finding = f"模型提取的运单号原文「{waybill_raw}」未能在 ASR 转写中核实，不能与目标 pno「{request.pno}」确认匹配。"
        elif waybill_match == WaybillMatch.NOT_FOUND:
            finding = f"转写中未识别到包裹号，无法将对话与目标 pno「{request.pno}」关联。"
        else:
            finding = f"运单号比对状态为 {waybill_match.value}；未确认客户本人明确收货。"

        if request.platform is None:
            finding += " 请求未提供 platform，无法匹配平台举证规则，本次不能自动认定妥投。"
        elif self._is_platform_evidence_accepted(request.platform, media.media_type):
            finding += f" 平台「{request.platform.value}」允许该类音视频证据；仍须同时满足运单及客户本人收货核验。"
        else:
            finding += f" 平台「{request.platform.value}」不采纳该类音视频证据，即使转写中有收货表达也不能据此认定妥投。"

        confidence = self._safe_float(analysis.get("confidence")) or 0.0
        confidence = min(max(confidence, 0.0), 1.0)
        if not (
            waybill_match == WaybillMatch.MATCHED
            and receipt_status == CustomerReceiptStatus.CONFIRMED
            and self._is_platform_evidence_accepted(request.platform, media.media_type)
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
    def _is_platform_evidence_accepted(
        platform: DeliveryPlatform | None,
        media_type: ConversationMediaType,
    ) -> bool:
        """应用图片审核提示词中的平台与通话证据准入矩阵。"""
        if platform is None:
            return False
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
