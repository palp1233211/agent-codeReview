"""从有限视频采样帧提取号码；识别模型不接触目标号码，比对由代码完成。"""

import base64
import json
import math
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from src.delivery_image.phone import normalize_phone

from .schemas import PhoneMatch, PhoneObservation

MIN_PHONE_FRAMES = 3
MAX_PHONE_FRAMES = 6
PHONE_FRAME_INTERVAL_SECONDS = 4


_WILDCARDS = "?*•xX_"


def _partial_compatible(raw: str, complete: str, country: str) -> bool:
    """遮挡/转场帧里可见的数字不得与已识别的完整号码矛盾；全是遮挡符则无信息。"""
    value = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in raw)
    value = re.sub(r"[\s()\-+]", "", value)
    if value.startswith("00"):
        value = value[2:]
    if any(not (c.isdigit() or c in _WILDCARDS) for c in value):
        return False
    pattern = "".join("?" if c in _WILDCARDS else c for c in value)
    if not any(c.isdigit() for c in pattern):
        return True
    international = complete.lstrip("+")
    forms = [international]
    if country.upper() == "TH" and international.startswith("66"):
        forms.append("0" + international[2:])

    def fits(chunk: str, target: str) -> bool:
        return len(chunk) == len(target) and all(a in ("?", b) for a, b in zip(chunk, target))

    # 可见片段可能是号码前缀（被截断）或后缀（前部被遮挡/省略国家码）。
    return any(
        len(pattern) <= len(form)
        and (fits(pattern, form[: len(pattern)]) or fits(pattern, form[len(form) - len(pattern):]))
        for form in forms
    )


def compare_phones(observations: list[PhoneObservation], recipient: str | None, country: str) -> PhoneMatch:
    target = normalize_phone(recipient, country) if recipient else None
    if target is None:
        return PhoneMatch.UNCERTAIN
    candidates = [item for item in observations if item.is_call_counterparty]
    if not candidates:
        return PhoneMatch.UNCERTAIN
    complete: set[str] = set()
    partials: list[str] = []
    for item in candidates:
        number = normalize_phone(item.raw_phone, country) if item.readable else None
        if number is None:
            partials.append(item.raw_phone)
        else:
            complete.add(number)
    # 多个不同通话号码无法关联到同一段录音，不允许选取其中恰好匹配的一个。
    if len(complete) != 1:
        return PhoneMatch.UNCERTAIN
    only = next(iter(complete))
    # 不可读帧只要不与完整号码矛盾就忽略；矛盾说明画面出现了别的号码。
    if any(not _partial_compatible(raw, only, country) for raw in partials):
        return PhoneMatch.UNCERTAIN
    return PhoneMatch.MATCHED if only == target else PhoneMatch.MISMATCH


class VideoPhoneReader:
    def __init__(self, config: Any, run_command: Any, client: Any = None) -> None:
        self.config = config
        self.run_command = run_command
        self.client = client

    @staticmethod
    def sample_times(duration: float) -> list[float]:
        if not math.isfinite(duration) or not 0 < duration <= 300:
            raise ValueError("视频时长无效或超过5分钟")
        end = max(0, duration - 0.1)
        # 通话界面可能在任意时刻出现：约每 4 秒一帧，至少 3 帧、至多 6 帧，首尾必取。
        count = min(MAX_PHONE_FRAMES, max(MIN_PHONE_FRAMES, math.ceil(duration / PHONE_FRAME_INTERVAL_SECONDS)))
        return sorted({round(end * index / (count - 1), 3) for index in range(count)})

    async def read(self, content: bytes, suffix: str, timeout: float) -> tuple[list[PhoneObservation], list[float]]:
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise RuntimeError("视频手机号抽帧需要 ffmpeg 和 ffprobe")
        with tempfile.TemporaryDirectory(prefix="delivery-phone-") as directory:
            source = Path(directory) / ("source" + suffix)
            source.write_bytes(content)
            raw_duration = await self.run_command(
                [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
                timeout=timeout,
            )
            times = self.sample_times(float(raw_duration.decode().strip()))
            frames = []
            for timestamp in times:
                frame = await self.run_command(
                    [ffmpeg, "-v", "error", "-nostdin", "-ss", str(timestamp), "-i", str(source),
                     "-map", "0:v:0", "-frames:v", "1", "-vf", "scale='min(1600,iw)':-2",
                     "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "2", "pipe:1"], timeout=timeout,
                )
                if not frame.startswith(b"\xff\xd8") or len(frame) > 4 * 1024 * 1024:
                    raise ValueError("视频抽帧未返回有效且大小合适的 JPEG")
                frames.append("data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii"))
        prompt = (Path(__file__).parents[1] / "prompts" / "video_phone.txt").read_text(encoding="utf-8")
        if self.client is None:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(api_key=self.config.api_token, base_url=self.config.base_url, max_retries=0)
        if self.config.api_mode == "responses":
            parts = [{"type": "input_text", "text": prompt}]
            for index, frame in enumerate(frames, 1):
                parts.extend([{"type": "input_text", "text": f"frame_index={index}"},
                              {"type": "input_image", "image_url": frame, "detail": "high"}])
            response = await self.client.responses.create(model=self.config.model, input=[{"role": "user", "content": parts}], timeout=timeout)
            raw = response.output_text
        else:
            parts = [{"type": "text", "text": prompt}]
            for index, frame in enumerate(frames, 1):
                parts.extend([{"type": "text", "text": f"frame_index={index}"},
                              {"type": "image_url", "image_url": {"url": frame, "detail": "high"}}])
            response = await self.client.chat.completions.create(model=self.config.model, messages=[{"role": "user", "content": parts}], timeout=timeout)
            raw = response.choices[0].message.content
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("observations"), list):
            raise ValueError("手机号识别响应格式错误")
        observations = []
        if len(payload["observations"]) > 120:
            raise ValueError("手机号识别返回条目过多")
        for item in payload["observations"]:
            if not isinstance(item, dict):
                raise ValueError("手机号识别条目格式错误")
            index = item.get("frame_index")
            if type(index) is not int or not 1 <= index <= len(times):
                raise ValueError("手机号识别返回无效帧索引")
            observations.append(PhoneObservation.model_validate({**item, "time_seconds": times[index - 1]}))
        return observations, times
