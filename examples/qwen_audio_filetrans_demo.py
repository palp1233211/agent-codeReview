#!/usr/bin/env python3
"""使用 Qwen 同步 ASR 分离短音频说话人，或使用 Filetrans 转写长音频/视频。

用法：
    export DASHSCOPE_API_KEY="你的百炼 API Key"
    python examples/qwen_audio_filetrans_demo.py \
        --media-file "/path/to/call.mp3" \
        --pno "THT1234567890"

默认使用 qwen-audio-3.1-asr-flash 同步接口识别 5 分钟以内的本地媒体并开启说话人分离。
长音频或远程 URL 使用 --engine filetrans 切换到异步 Filetrans 接口。
如使用 Workspace 专属域名，可通过 QWEN_ASR_BASE_URL 覆盖默认 API 地址。
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import mimetypes
import os
import re
import shutil
import sys
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://maas.qianwenaiapi.com/api/v1"
TRANSCRIPTION_PATH = "/services/audio/asr/transcription"
MODEL_NAME = "qwen-audio-3.1-asr-flash-filetrans"
DIARIZATION_MODEL_NAME = "qwen-audio-3.1-asr-flash"
DIARIZATION_PATH = "/services/aigc/multimodal-generation/generation"
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "UNKNOWN"}


def _load_project_env() -> None:
    """读取项目根目录 .env 的简单 KEY=VALUE 配置，不依赖第三方库。"""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            os.environ.setdefault(key, value)


def _request_json(
    method: str,
    url: str,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """发送 HTTP 请求并校验 JSON 响应，避免错误响应中泄露 URL 查询签名。"""
    request_headers = dict(headers or {})
    request_body = None
    if body is not None:
        request_headers.setdefault("Content-Type", "application/json")
        request_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=request_body, headers=request_headers, method=method)

    try:
        with urlopen(request, timeout=timeout) as response:
            status_code = response.status
            response_body = response.read()
    except HTTPError as exc:
        status_code = exc.code
        response_body = exc.read()

    if not 200 <= status_code < 300:
        # 错误响应可能回显输入媒体 URL（可能带签名），仅保留格式受限的错误码。
        try:
            error_payload = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            error_payload = {}
        error_code = ""
        if isinstance(error_payload, dict):
            error_value = error_payload.get("code")
            if isinstance(error_value, str) and error_value.replace("_", "").replace("-", "").isalnum():
                error_code = error_value[:80]
        suffix = f", code={error_code}" if error_code else ""
        raise RuntimeError(f"HTTP {status_code}{suffix}")
    try:
        payload = json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("API 返回的内容不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("API 返回的 JSON 顶层必须是对象")
    return payload


def _submit_task(
    base_url: str,
    api_key: str,
    media_url: str,
    *,
    pno: str | None,
    language: str | None,
    diarization: bool,
    speaker_count: int,
    timeout: float,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "channel_id": [0],
        "diarization_enabled": diarization,
    }
    if language:
        parameters["language_hints"] = [language]
    if diarization:
        # 客服回访录音通常是客服与客户两人对话；这是人数提示，不代表模型必定分出两人。
        parameters["speaker_count"] = speaker_count
    if pno:
        # Filetrans 支持权重 50 的强热词，强化目标运单号识别；仍保留原始转写供核验。
        parameters["vocabulary"] = {pno: 50}

    body = {
        "model": MODEL_NAME,
        "input": {"file_urls": [media_url]},
        # Filetrans 模型必须显式传 parameters，即使没有额外配置也要传空对象。
        "parameters": parameters,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    if media_url.startswith("oss://"):
        headers["X-DashScope-OssResourceResolve"] = "enable"
    return _request_json(
        "POST",
        f"{base_url}{TRANSCRIPTION_PATH}",
        headers=headers,
        body=body,
        timeout=timeout,
    )


def _upload_local_file(base_url: str, api_key: str, file_path: Path, *, timeout: float) -> str:
    """上传本地媒体到百炼临时 OSS，返回本次模型调用可用的 oss:// URL。"""
    if not file_path.is_file():
        raise RuntimeError(f"本地媒体文件不存在：{file_path}")
    file_size = file_path.stat().st_size
    if file_size <= 0:
        raise RuntimeError("本地媒体文件为空")

    policy_url = f"{base_url}/uploads?{urlencode({'action': 'getPolicy', 'model': MODEL_NAME})}"
    policy_response = _request_json(
        "GET",
        policy_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    policy = policy_response.get("data")
    if not isinstance(policy, dict):
        raise RuntimeError("获取临时上传凭证失败：响应缺少 data")

    try:
        max_size_bytes = int(float(policy["max_file_size_mb"]) * 1024 * 1024)
        upload_host = str(policy["upload_host"])
        upload_dir = str(policy["upload_dir"])
        fields = {
            "OSSAccessKeyId": str(policy["oss_access_key_id"]),
            "Signature": str(policy["signature"]),
            "policy": str(policy["policy"]),
            "x-oss-object-acl": str(policy["x_oss_object_acl"]),
            "x-oss-forbid-overwrite": str(policy["x_oss_forbid_overwrite"]),
            "key": f"{upload_dir.rstrip('/')}/{file_path.name}",
            "success_action_status": "200",
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("上传凭证缺少必需字段或文件大小限制无效") from exc

    if file_size > max_size_bytes:
        max_size_mb = max_size_bytes / (1024 * 1024)
        raise RuntimeError(f"文件超过本次上传凭证限制（{max_size_mb:g} MB）")

    upload_url = urlsplit(upload_host)
    if upload_url.scheme != "https" or not upload_url.hostname:
        raise RuntimeError("上传凭证返回了无效的 HTTPS OSS 地址")

    boundary = f"----QwenASR{os.urandom(16).hex()}"
    boundary_bytes = boundary.encode("ascii")
    form_prefix = bytearray()
    for name, value in fields.items():
        form_prefix.extend(b"--" + boundary_bytes + b"\r\n")
        form_prefix.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        form_prefix.extend(value.encode("utf-8"))
        form_prefix.extend(b"\r\n")

    safe_filename = file_path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    content_type = mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"
    file_header = (
        b"--" + boundary_bytes + b"\r\n"
        + f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'.encode("utf-8")
        + f"Content-Type: {content_type}\r\n\r\n".encode("ascii")
    )
    trailer = b"\r\n--" + boundary_bytes + b"--\r\n"
    body_length = len(form_prefix) + len(file_header) + file_size + len(trailer)

    connection = http.client.HTTPSConnection(
        upload_url.hostname,
        upload_url.port or 443,
        timeout=timeout,
    )
    upload_path = upload_url.path or "/"
    if upload_url.query:
        upload_path += "?" + upload_url.query
    try:
        connection.putrequest("POST", upload_path)
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(body_length))
        connection.endheaders()
        connection.send(form_prefix)
        connection.send(file_header)
        with file_path.open("rb") as media_file:
            while chunk := media_file.read(1024 * 1024):
                connection.send(chunk)
        connection.send(trailer)
        response = connection.getresponse()
        response.read()
        if response.status != 200:
            raise RuntimeError(f"上传本地文件到临时 OSS 失败：HTTP {response.status}")
    finally:
        connection.close()

    return f"oss://{fields['key']}"


def _convert_media_to_mono(file_path: Path, output_dir: Path) -> Path:
    """提取媒体音轨并转成 16 kHz 单声道 WAV，满足 Filetrans 说话人分离要求。"""
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("本地媒体启用说话人分离需要 ffmpeg；请先安装 ffmpeg，或传入单声道媒体 URL")

    mono_path = output_dir / f"{file_path.stem}_mono.wav"
    command = [
        ffmpeg_path,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(file_path),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(mono_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or "").strip()
        details = details[-500:] if details else "ffmpeg 未提供错误详情"
        raise RuntimeError(f"本地媒体转单声道失败：{details}") from exc
    if not mono_path.is_file() or mono_path.stat().st_size <= 0:
        raise RuntimeError("ffmpeg 未生成有效的单声道音频文件")
    return mono_path


def _get_media_duration_seconds(file_path: Path) -> float | None:
    """读取本地文件时长；ffprobe 不可用时交由模型 API 校验时长。"""
    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


def _submit_sync_diarization(
    base_url: str,
    api_key: str,
    mono_wav: Path,
    *,
    pno: str | None,
    timeout: float,
) -> dict[str, Any]:
    """调用短音频同步 ASR，使用其专用说话人分离参数与结果结构。"""
    audio_base64 = base64.b64encode(mono_wav.read_bytes()).decode("ascii")
    parameters: dict[str, Any] = {
        "format": "wav",
        "sample_rate": 16000,
        "speaker_diarization_enabled": True,
    }
    if pno:
        parameters["vocabulary"] = {pno: 50}

    body = {
        "model": DIARIZATION_MODEL_NAME,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": f"data:audio/wav;base64,{audio_base64}",
                            },
                        }
                    ],
                }
            ]
        },
        "parameters": parameters,
    }
    return _request_json(
        "POST",
        f"{base_url}{DIARIZATION_PATH}",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-DashScope-SSE": "disable",
        },
        body=body,
        timeout=timeout,
    )


def _format_sync_diarization_result(
    result: dict[str, Any],
    *,
    pno: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """将同步 ASR 的 output.sentences 适配成 demo 统一输出并保留诊断字段。"""
    output = result.get("output")
    if not isinstance(output, dict):
        raise RuntimeError("同步 ASR 响应缺少 output 对象")
    sentences = output.get("sentences")
    if not isinstance(sentences, list):
        sentence = output.get("sentence")
        sentences = [sentence] if isinstance(sentence, dict) else []
    normalized = {"transcripts": [{"channel_id": 0, "sentences": sentences}]}
    formatted = _format_transcript(normalized, pno=pno)
    return formatted, _speaker_diagnostics(normalized)


def _poll_task(
    base_url: str,
    api_key: str,
    task_id: str,
    *,
    poll_interval: float,
    max_wait: float,
    timeout: float,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"}
    url = f"{base_url}/tasks/{task_id}"
    deadline = time.monotonic() + max_wait

    while True:
        payload = _request_json("GET", url, headers=headers, timeout=timeout)
        output = payload.get("output")
        if not isinstance(output, dict):
            raise RuntimeError("任务查询响应缺少 output 对象")
        status = str(output.get("task_status", "UNKNOWN")).upper()
        print(f"任务状态：{status}", file=sys.stderr)

        if status in TERMINAL_STATUSES:
            if status != "SUCCEEDED":
                failed_items = output.get("results")
                if isinstance(failed_items, list):
                    failed_item = next(
                        (
                            item
                            for item in failed_items
                            if isinstance(item, dict) and item.get("subtask_status") == "FAILED"
                        ),
                        None,
                    )
                    if failed_item:
                        code = failed_item.get("code", "未知错误")
                        raise RuntimeError(f"转写子任务失败：{code}")
                code = output.get("code", output.get("message", status))
                raise RuntimeError(f"转写任务失败：{code}")

            results = output.get("results")
            if isinstance(results, list):
                successful_item = next(
                    (
                        item
                        for item in results
                        if isinstance(item, dict)
                        and item.get("subtask_status", "SUCCEEDED") == "SUCCEEDED"
                        and isinstance(item.get("transcription_url"), str)
                    ),
                    None,
                )
                if successful_item:
                    return {"transcription_url": successful_item["transcription_url"]}
                failed_item = next(
                    (
                        item
                        for item in results
                        if isinstance(item, dict) and item.get("subtask_status") == "FAILED"
                    ),
                    None,
                )
                if failed_item:
                    code = failed_item.get("code", "未知错误")
                    raise RuntimeError(f"转写子任务失败：{code}")

            # 兼容部分接口版本使用的单数 result 响应结构。
            result = output.get("result")
            if isinstance(result, dict) and isinstance(result.get("transcription_url"), str):
                return result
            raise RuntimeError("任务状态为 SUCCEEDED，但响应中没有成功子任务的转写结果链接")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"等待转写任务超过 {max_wait:g} 秒，task_id={task_id}")
        time.sleep(min(poll_interval, remaining))


def _download_transcript(
    transcript_url: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    # 结果链接含短期签名，不打印完整 URL，避免把访问凭据写入日志。
    return _request_json("GET", transcript_url, timeout=timeout)


def _normalize_waybill_text(text: Any, pno: str | None) -> tuple[Any, Any | None]:
    """仅在泰语字母音译后的运单尾号与目标单号完全匹配时，将其规范为拉丁字母。"""
    if not isinstance(text, str) or not pno:
        return text, None

    expected = re.sub(r"[^A-Z0-9]", "", pno.upper())
    if not expected.startswith("THT") or len(expected) <= 3:
        return text, None

    # 泰语 ASR 可能把字母 T-H-T 识别成“ทีเอชที”；仅接受后续内容与目标单号尾段完全一致。
    pattern = re.compile(r"ที\s*เอช\s*ที\s*([A-Z0-9\s-]+)", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return text, None

    detected_suffix = re.sub(r"[^A-Z0-9]", "", match.group(1).upper())
    if detected_suffix != expected[3:]:
        return text, None

    normalized = f"{text[:match.start()]}{pno}{text[match.end():]}"
    return normalized, text


def _format_word_speaker_segments(
    sentence: dict[str, Any],
    *,
    channel_id: Any,
) -> list[dict[str, Any]]:
    """若词级结果含多个说话人标签，按连续说话人归并为可读片段。"""
    words = sentence.get("words")
    if not isinstance(words, list) or not words:
        return []

    valid_words = [
        word
        for word in words
        if isinstance(word, dict) and isinstance(word.get("speaker_id"), int)
    ]
    if len({word["speaker_id"] for word in valid_words}) < 2 or len(valid_words) != len(words):
        return []

    segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for word in valid_words:
        speaker_id = word["speaker_id"]
        word_text = word.get("text", "")
        if not isinstance(word_text, str):
            word_text = ""
        punctuation = word.get("punctuation", "")
        if isinstance(punctuation, str) and punctuation:
            punctuation_mark = punctuation.strip()
            if punctuation_mark and not word_text.rstrip().endswith(punctuation_mark):
                word_text += punctuation
            elif punctuation.endswith(" ") and not word_text.endswith(" "):
                word_text += " "

        if current is None or current["speaker_id"] != speaker_id:
            current = {
                "channel_id": channel_id,
                "speaker_id": speaker_id,
                "start_seconds": _milliseconds_to_seconds(word.get("begin_time")),
                "end_seconds": _milliseconds_to_seconds(word.get("end_time")),
                "language": sentence.get("language"),
                "text": word_text,
            }
            segments.append(current)
        else:
            current["text"] += word_text
            end_seconds = _milliseconds_to_seconds(word.get("end_time"))
            if end_seconds is not None:
                current["end_seconds"] = end_seconds

    return segments


def _format_transcript(
    result: dict[str, Any],
    *,
    pno: str | None = None,
) -> list[dict[str, Any]]:
    """只输出审核需要的句子信息，不回显原始媒体 URL 或结果下载签名。"""
    formatted: list[dict[str, Any]] = []
    transcripts = result.get("transcripts", [])
    if not isinstance(transcripts, list):
        return formatted

    for transcript in transcripts:
        if not isinstance(transcript, dict):
            continue
        channel_id = transcript.get("channel_id")
        sentences = transcript.get("sentences", [])
        if isinstance(sentences, list) and sentences:
            for sentence in sentences:
                if not isinstance(sentence, dict):
                    continue
                word_segments = _format_word_speaker_segments(sentence, channel_id=channel_id)
                if word_segments:
                    for segment in word_segments:
                        text, raw_text = _normalize_waybill_text(segment["text"], pno)
                        segment["text"] = text
                        if raw_text is not None:
                            segment["raw_text"] = raw_text
                        formatted.append(segment)
                    continue
                text, raw_text = _normalize_waybill_text(sentence.get("text", ""), pno)
                formatted_sentence = {
                    "channel_id": channel_id,
                    "speaker_id": sentence.get("speaker_id"),
                    "start_seconds": _milliseconds_to_seconds(sentence.get("begin_time")),
                    "end_seconds": _milliseconds_to_seconds(sentence.get("end_time")),
                    "language": sentence.get("language"),
                    "text": text,
                }
                if raw_text is not None:
                    formatted_sentence["raw_text"] = raw_text
                formatted.append(formatted_sentence)
        else:
            text, raw_text = _normalize_waybill_text(transcript.get("text", ""), pno)
            formatted_transcript = {
                "channel_id": channel_id,
                "speaker_id": None,
                "start_seconds": None,
                "end_seconds": None,
                "language": None,
                "text": text,
            }
            if raw_text is not None:
                formatted_transcript["raw_text"] = raw_text
            formatted.append(formatted_transcript)
    return formatted


def _speaker_diagnostics(result: dict[str, Any]) -> dict[str, Any]:
    """提取原始响应中的标签与声道信息，便于区分模型输出和格式化行为。"""
    sentence_ids: set[int] = set()
    word_ids: set[int] = set()
    transcripts = result.get("transcripts", [])
    if isinstance(transcripts, list):
        for transcript in transcripts:
            if not isinstance(transcript, dict):
                continue
            sentences = transcript.get("sentences", [])
            if not isinstance(sentences, list):
                continue
            for sentence in sentences:
                if not isinstance(sentence, dict):
                    continue
                speaker_id = sentence.get("speaker_id")
                if isinstance(speaker_id, int):
                    sentence_ids.add(speaker_id)
                words = sentence.get("words", [])
                if isinstance(words, list):
                    word_ids.update(
                        word["speaker_id"]
                        for word in words
                        if isinstance(word, dict) and isinstance(word.get("speaker_id"), int)
                    )
    properties = result.get("properties")
    audio_channels = properties.get("channels") if isinstance(properties, dict) else None
    return {
        "audio_channels": audio_channels,
        "sentence_speaker_ids": sorted(sentence_ids),
        "word_speaker_ids": sorted(word_ids),
    }


def _milliseconds_to_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return round(value / 1000, 3)
    return None


def main() -> int:
    _load_project_env()
    parser = argparse.ArgumentParser(description="转写公开可访问的音频/视频 URL 并输出带说话人和时间戳的 JSON。")
    media_input = parser.add_mutually_exclusive_group(required=True)
    media_input.add_argument("--media-url", help="Filetrans 使用的百炼服务端可访问音频/视频 URL")
    media_input.add_argument("--media-file", type=Path, help="本地音频或视频；同步分离模式会提取并转为 16 kHz 单声道 WAV")
    parser.add_argument(
        "--engine",
        choices=["sync-diarization", "filetrans"],
        default="sync-diarization",
        help="识别接口：短音频默认使用同步说话人分离模型；长音频/URL 可选 filetrans",
    )
    parser.add_argument("--pno", help="可选运单号热词，提高专有运单号的识别召回")
    parser.add_argument("--language", help="Filetrans 的可选语言提示，例如 th、en、zh；同步分离模式由模型自动识别")
    parser.add_argument("--speaker-count", type=int, default=2, help="Filetrans 的说话人数提示，默认 2；模型不保证严格按此人数输出")
    parser.add_argument("--no-diarization", action="store_true", help="关闭说话人分离（默认开启）")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="任务轮询间隔秒数，默认 2")
    parser.add_argument("--max-wait", type=float, default=600.0, help="最大等待秒数，默认 600")
    parser.add_argument("--request-timeout", type=float, default=120.0, help="单次 HTTP 请求超时秒数，默认 120")
    args = parser.parse_args()

    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        parser.error("请设置 DASHSCOPE_API_KEY 环境变量")
    if args.poll_interval <= 0 or args.max_wait <= 0 or args.request_timeout <= 0:
        parser.error("轮询间隔、最大等待时间和请求超时都必须大于 0")
    if args.engine == "filetrans" and not 2 <= args.speaker_count <= 100:
        parser.error("--speaker-count 必须在 2 到 100 之间")
    if args.engine == "sync-diarization" and not args.media_file:
        parser.error("同步说话人分离模式需要 --media-file；远程 URL 请加 --engine filetrans")
    if args.engine == "sync-diarization" and args.no_diarization:
        parser.error("同步说话人分离模式固定开启说话人分离，不支持 --no-diarization")

    base_url = os.environ.get("QWEN_ASR_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    if args.engine == "sync-diarization":
        source_file = args.media_file.expanduser().resolve()
        if not source_file.is_file():
            raise RuntimeError(f"本地媒体文件不存在：{source_file}")
        duration = _get_media_duration_seconds(source_file)
        if duration is not None and duration > 300:
            parser.error("同步说话人分离模型只适用于 5 分钟以内音频；长音频请使用 --engine filetrans")
        if args.language:
            print("提示：同步说话人分离模式由模型自动识别语言，未使用 --language。", file=sys.stderr)

        with tempfile.TemporaryDirectory(prefix="qwen-asr-mono-") as temp_dir:
            mono_file = _convert_media_to_mono(source_file, Path(temp_dir))
            sync_result = _submit_sync_diarization(
                base_url,
                api_key,
                mono_file,
                pno=args.pno,
                timeout=args.request_timeout,
            )
        print("本地媒体已转为 16 kHz 单声道，使用 qwen-audio-3.1-asr-flash 同步识别", file=sys.stderr)
        formatted_transcript, diagnostics = _format_sync_diarization_result(sync_result, pno=args.pno)
        speaker_ids = {
            item.get("speaker_id")
            for item in formatted_transcript
            if isinstance(item.get("speaker_id"), int)
        }
        if len(speaker_ids) < 2:
            print(
                f"警告：同步 ASR 仍只识别到一个说话人；原始标签诊断：{json.dumps(diagnostics, ensure_ascii=False)}。",
                file=sys.stderr,
            )
        print(json.dumps(formatted_transcript, ensure_ascii=False, indent=2))
        return 0

    media_url = args.media_url
    if args.media_file:
        source_file = args.media_file.expanduser().resolve()
        if not source_file.is_file():
            raise RuntimeError(f"本地媒体文件不存在：{source_file}")
        if args.no_diarization:
            media_url = _upload_local_file(base_url, api_key, source_file, timeout=args.request_timeout)
            print("本地媒体已上传到百炼临时存储（URL 有效期 48 小时）", file=sys.stderr)
        else:
            with tempfile.TemporaryDirectory(prefix="qwen-asr-mono-") as temp_dir:
                mono_file = _convert_media_to_mono(source_file, Path(temp_dir))
                media_url = _upload_local_file(base_url, api_key, mono_file, timeout=args.request_timeout)
            print("本地媒体已转为 16 kHz 单声道并上传到百炼临时存储（URL 有效期 48 小时）", file=sys.stderr)
    submitted = _submit_task(
        base_url,
        api_key,
        media_url,
        pno=args.pno,
        language=args.language,
        diarization=not args.no_diarization,
        speaker_count=args.speaker_count,
        timeout=args.request_timeout,
    )
    output = submitted.get("output")
    task_id = output.get("task_id") if isinstance(output, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError("提交响应缺少 output.task_id")
    print(f"已提交转写任务：{task_id}", file=sys.stderr)

    result_meta = _poll_task(
        base_url,
        api_key,
        task_id,
        poll_interval=args.poll_interval,
        max_wait=args.max_wait,
        timeout=args.request_timeout,
    )
    transcript_url = result_meta.get("transcription_url")
    if not isinstance(transcript_url, str) or not transcript_url:
        raise RuntimeError("任务完成响应缺少 result.transcription_url")
    transcript_result = _download_transcript(transcript_url, timeout=args.request_timeout)

    formatted_transcript = _format_transcript(transcript_result, pno=args.pno)
    if not args.no_diarization:
        speaker_ids = {
            item.get("speaker_id")
            for item in formatted_transcript
            if isinstance(item.get("speaker_id"), int)
        }
        if len(speaker_ids) < 2:
            diagnostics = _speaker_diagnostics(transcript_result)
            print(
                f"警告：本次已启用说话人分离并提示 {args.speaker_count} 人，但 API 结果没有拆出多个说话人；原始标签诊断：{json.dumps(diagnostics, ensure_ascii=False)}。",
                file=sys.stderr,
            )
    print(json.dumps(formatted_transcript, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (URLError, OSError, RuntimeError, TimeoutError) as exc:
        print(f"转写失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
