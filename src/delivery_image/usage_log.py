"""模型调用的 token 用量日志：每次调用打一行，用来算每张图、每个视频实际花了多少 token。

responses 与 chat_completions 两种接口的 usage 字段名不同，这里统一读取；字段缺失（网关不返回）时记为 "-"，不报错。
只记录数字和图片的字节数、像素尺寸，不记录提示词、图片内容、手机号或地址。
"""

import base64
import logging
import struct
from typing import Any

from .log_context import log_context

_logger = logging.getLogger(__name__)

# (responses 字段路径, chat_completions 字段路径)
_FIELDS = {
    "input": (("input_tokens",), ("prompt_tokens",)),
    "output": (("output_tokens",), ("completion_tokens",)),
    "total": (("total_tokens",), ("total_tokens",)),
    "cached": (("input_tokens_details", "cached_tokens"), ("prompt_tokens_details", "cached_tokens")),
    "reasoning": (("output_tokens_details", "reasoning_tokens"), ("completion_tokens_details", "reasoning_tokens")),
}


def _read(obj: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        obj = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
        if obj is None:
            return None
    return obj


def usage_fields(response: Any) -> dict[str, str]:
    """从模型响应里取用量；两种接口的字段名都认，取不到的项记为 "-"。"""
    usage = _read(response, ("usage",))
    fields: dict[str, str] = {}
    for name, paths in _FIELDS.items():
        value = None
        if usage is not None:
            for path in paths:
                value = _read(usage, path)
                if value is not None:
                    break
        fields[name] = str(value) if isinstance(value, int) and not isinstance(value, bool) else "-"
    return fields


def image_dimensions(data: bytes) -> str:
    """PNG / JPEG 的像素尺寸，如 "1080x1920"；其他格式或解析失败返回 "-"。"""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            width, height = struct.unpack(">II", data[16:24])
            return f"{width}x{height}"
        if data.startswith(b"\xff\xd8"):
            index = 2
            while index + 9 < len(data):
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    index += 2
                    continue
                length = struct.unpack(">H", data[index + 2:index + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">HH", data[index + 5:index + 9])
                    return f"{width}x{height}"
                index += 2 + length
    except (struct.error, IndexError):
        pass
    return "-"


def describe_data_urls(data_urls: list[str]) -> str:
    """图片的字节数和像素尺寸，用来对照 token 数，如 "bytes=204800 size=800x600"（多张时逗号分隔）。"""
    sizes, dimensions = [], []
    for url in data_urls:
        try:
            raw = base64.b64decode(url.split(",", 1)[1], validate=False)
        except (IndexError, ValueError):
            sizes.append("-")
            dimensions.append("-")
            continue
        sizes.append(str(len(raw)))
        dimensions.append(image_dimensions(raw))
    return f"image_bytes={','.join(sizes) or '-'} image_size={','.join(dimensions) or '-'}"


def log_usage(call: str, response: Any, model: str, duration_ms: int | None = None, **extra: Any) -> None:
    """打一行用量日志。call 标明是哪类调用：image / joint / phone / analysis。"""
    fields = usage_fields(response)
    parts = [f"call={call}", f"model={model}"]
    parts.extend(f"{key}={value}" for key, value in extra.items())
    parts.extend(f"{name}={value}" for name, value in fields.items())
    if duration_ms is not None:
        parts.append(f"duration_ms={duration_ms}")
    _logger.info("delivery model usage %s %s", log_context(), " ".join(parts))
