"""从有限视频采样帧提取号码；识别模型不接触目标号码，比对由代码完成。"""

import base64
import json
import math
import re
import shutil
import logging
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any

from src.delivery_image.phone import normalize_phone
from src.delivery_image.usage_log import describe_data_urls, log_usage

from .schemas import PhoneMatch, PhoneObservation


def strip_json_fence(raw: Any) -> str:
    """模型常把 JSON 包在 ```json ... ``` 里；去掉围栏再解析。非字符串（如空回复）原样交给 json.loads 报错。"""
    if not isinstance(raw, str):
        return raw  # type: ignore[return-value]
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9_-]*\s*|\s*```$", "", text).strip()
    return text


class PhoneReviewInputError(ValueError):
    """视频本身不满足抽帧识别的要求（时长、抽帧结果）。

    消息只含固定文字和时长数字，不含地址、号码或模型响应，可以直接写日志和返回给调用方。
    视频本身的问题重试也不会好，和瞬时故障要区分开。
    """


class NoVideoStreamError(PhoneReviewInputError):
    """文件里没有视频画面（例如通话录音被存成 mp4），实际是纯音频。纯音频无法核验手机号，按规则不采纳。"""


class MediaCommandError(ValueError):
    """ffprobe/ffmpeg 返回非 0 退出码。消息含命令名和 stderr 第一行（已去掉临时文件路径），不含用户地址。"""


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
            shown = f"{duration:.1f}秒" if math.isfinite(duration) else "无法识别"
            raise PhoneReviewInputError(f"视频时长无效或超过5分钟（实测 {shown}）")
        end = max(0, duration - 0.1)
        # 通话界面可能在任意时刻出现：约每 4 秒一帧，至少 3 帧、至多 6 帧，首尾必取。
        count = min(MAX_PHONE_FRAMES, max(MIN_PHONE_FRAMES, math.ceil(duration / PHONE_FRAME_INTERVAL_SECONDS)))
        return sorted({round(end * index / (count - 1), 3) for index in range(count)})

    async def _extract_frame(self, ffmpeg: str, source: Path, timestamp: float, timeout: float) -> bytes:
        """抽一帧 JPEG。

        新版 ffmpeg 的 mjpeg 编码器遇到「非全范围 YUV」（多数手机 h264 视频的标准色彩范围）默认拒绝输出，报
        "Non full-range YUV is non-standard, set strict_std_compliance to at most unofficial"。
        先按 ffmpeg 自己的建议加 -strict unofficial；仍失败时再退一步，把像素格式转成 JPEG 的标准全范围格式重试一次。
        """
        base = [ffmpeg, "-v", "error", "-nostdin", "-ss", str(timestamp), "-i", str(source), "-map", "0:v:0", "-frames:v", "1"]
        scale = "scale='min(1600,iw)':-2"
        primary = base + ["-vf", scale, "-f", "image2pipe", "-vcodec", "mjpeg", "-strict", "unofficial", "-q:v", "2", "pipe:1"]
        try:
            return await self.run_command(primary, timeout=timeout)
        except MediaCommandError:
            fallback = base + ["-vf", scale + ",format=yuvj420p", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "2", "pipe:1"]
            return await self.run_command(fallback, timeout=timeout)

    def _log_usage(self, response: Any, frames: list[str], started: float) -> None:
        """记录抽帧识别手机号这次模型调用的真实 token 用量；日志失败不能影响审核结果。"""
        try:
            details = dict(pair.split("=", 1) for pair in describe_data_urls(frames).split(" "))
            log_usage("phone", response, self.config.model, int((time.monotonic() - started) * 1000),
                      images=len(frames), **details)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).debug("usage log failed", exc_info=True)

    # 取帧位置超出视频流实际结尾时，ffmpeg 不报错、只输出 0 字节（容器时长常比视频流略长），依次往前退再取
    EMPTY_FRAME_BACKOFF_SECONDS = (1.0, 3.0, 6.0)

    async def _frame_near(self, ffmpeg: str, source: Path, timestamp: float, timeout: float) -> tuple[bytes, float]:
        """在 timestamp 取一帧；取不到画面（0 字节）就往前退再试，返回帧和实际取帧时间（识别结果按实际时间记录）。"""
        frame = await self._extract_frame(ffmpeg, source, timestamp, timeout)
        tried = {timestamp}
        for back in self.EMPTY_FRAME_BACKOFF_SECONDS:
            if frame:
                break
            earlier = round(max(0.0, timestamp - back), 3)
            if earlier in tried:  # 已经退到开头，不重复取同一个位置
                continue
            tried.add(earlier)
            frame = await self._extract_frame(ffmpeg, source, earlier, timeout)
            if frame:
                return frame, earlier
        return frame, timestamp

    async def read(self, content: bytes, suffix: str, timeout: float) -> tuple[list[PhoneObservation], list[float]]:
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise RuntimeError("视频手机号抽帧需要 ffmpeg 和 ffprobe")
        with tempfile.TemporaryDirectory(prefix="delivery-phone-") as directory:
            source = Path(directory) / ("source" + suffix)
            source.write_bytes(content)
            # 一次 ffprobe 同时取「有没有视频流」和「总时长」：有视频流时先输出一行 video，再输出时长
            probe = await self.run_command(
                [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type:format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
                timeout=timeout,
            )
            lines = [line.strip() for line in probe.decode(errors="replace").splitlines() if line.strip()]
            if "video" not in lines:
                raise NoVideoStreamError("文件没有视频画面，实际是纯音频（视频类型的文件里只有音轨），无法核验画面手机号")
            try:
                duration = float(lines[-1])
            except ValueError as exc:
                # 部分封装格式（如 webm/mkv）读不出总时长，ffprobe 会返回 N/A
                raise PhoneReviewInputError("无法读取视频时长（ffprobe 返回了不可解析的值）") from exc
            frames = []
            times = []
            for frame_number, timestamp in enumerate(self.sample_times(duration), start=1):
                frame, actual = await self._frame_near(ffmpeg, source, timestamp, timeout)
                if not frame.startswith(b"\xff\xd8") or len(frame) > 4 * 1024 * 1024:
                    raise PhoneReviewInputError(
                        f"视频抽帧未返回有效且大小合适的 JPEG（第 {frame_number} 帧，{timestamp}s，返回 {len(frame)} 字节）"
                    )
                frames.append("data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii"))
                times.append(actual)
        prompt = (Path(__file__).parents[1] / "prompts" / "video_phone.txt").read_text(encoding="utf-8")
        if self.client is None:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(api_key=self.config.api_token, base_url=self.config.base_url, max_retries=0)
        if self.config.api_mode == "responses":
            parts = [{"type": "input_text", "text": prompt}]
            for index, frame in enumerate(frames, 1):
                parts.extend([{"type": "input_text", "text": f"frame_index={index}"},
                              {"type": "input_image", "image_url": frame, "detail": "high"}])
            call_started = time.monotonic()
            response = await self.client.responses.create(model=self.config.model, input=[{"role": "user", "content": parts}], timeout=timeout)
            raw = response.output_text
        else:
            parts = [{"type": "text", "text": prompt}]
            for index, frame in enumerate(frames, 1):
                parts.extend([{"type": "text", "text": f"frame_index={index}"},
                              {"type": "image_url", "image_url": {"url": frame, "detail": "high"}}])
            call_started = time.monotonic()
            response = await self.client.chat.completions.create(model=self.config.model, messages=[{"role": "user", "content": parts}], timeout=timeout)
            raw = response.choices[0].message.content
        self._log_usage(response, frames, call_started)
        payload = json.loads(strip_json_fence(raw))
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
