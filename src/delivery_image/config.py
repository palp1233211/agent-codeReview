"""妥投图片审核 Agent 的独立运行配置。"""

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class DeliveryImageConfigError(ValueError):
    """配置缺失或格式不安全。"""


_HOST_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_DEFAULT_AUDIO_BASE_URL = "https://maas.qianwenaiapi.com/api/v1"

# 配置只有两套模型，其余是服务自身的配置：
#   GPT 一套（DELIVERY_GPT_*）：图片审核、视频抓帧识别手机号、视频对话文本分析，都用它。
#   Qwen 一套（DELIVERY_QWEN_*）：只用于视频语音转写（百炼 ASR）。
#   服务自身（DELIVERY_SERVICE_TOKEN、DELIVERY_ALLOWED_HOSTS）以及整单级的下载/总超时。
# 只认新名字，不兼容旧名字。旧名字留在环境里不会生效，下面的映射只用来给出明确的改名提示。
_RENAMED = {
    "DELIVERY_IMAGE_MODEL": "DELIVERY_GPT_MODEL",
    "DELIVERY_IMAGE_API_TOKEN": "DELIVERY_GPT_API_KEY",
    "DELIVERY_IMAGE_BASE_URL": "DELIVERY_GPT_BASE_URL",
    "DELIVERY_IMAGE_API_MODE": "DELIVERY_GPT_API_MODE",
    "DELIVERY_IMAGE_TIMEOUT": "DELIVERY_GPT_TIMEOUT",
    "DELIVERY_AUDIO_API_TOKEN": "DELIVERY_QWEN_API_KEY",
    "DELIVERY_AUDIO_BASE_URL": "DELIVERY_QWEN_BASE_URL",
    "DELIVERY_AUDIO_MODEL": "DELIVERY_QWEN_ASR_MODEL",
    "DELIVERY_AUDIO_TIMEOUT": "DELIVERY_QWEN_TIMEOUT",
    "DELIVERY_IMAGE_SERVICE_TOKEN": "DELIVERY_SERVICE_TOKEN",
    "DELIVERY_IMAGE_ALLOWED_HOSTS": "DELIVERY_ALLOWED_HOSTS",
}
# 早已不起作用、应该直接删除的配置
_ANALYSIS_REASON = "视频文本分析使用 GPT 那一套配置"
_PROMPT_REASON = "提示词版本只取提示词文件里的 version，响应里的 prompt_version 以它为准"
_DEAD_NAMES = {
    "DELIVERY_CONVERSATION_ANALYSIS_MODEL": _ANALYSIS_REASON,
    "DELIVERY_CONVERSATION_ANALYSIS_API_TOKEN": _ANALYSIS_REASON,
    "DELIVERY_CONVERSATION_ANALYSIS_BASE_URL": _ANALYSIS_REASON,
    "DELIVERY_CONVERSATION_ANALYSIS_API_MODE": _ANALYSIS_REASON,
    "DELIVERY_IMAGE_PROMPT_VERSION": _PROMPT_REASON,
    "DELIVERY_CONVERSATION_PROMPT_VERSION": _PROMPT_REASON,
}


def _pick(values, name: str, default: str = "") -> str:
    """取配置值；只认这个新名字。"""
    value = str(values.get(name, "")).strip()
    return value if value else default


_SECOND_ASR_OFF = {"off", "none", "disabled", "false", "0"}


def _second_asr_model(values) -> str | None:
    """第二份转写的模型；默认 qwen3-asr-flash，设为 off 关闭。"""
    value = _pick(values, "DELIVERY_QWEN_SECOND_ASR_MODEL", "qwen3-asr-flash")
    return None if value.lower() in _SECOND_ASR_OFF else value


def _missing(values, name: str) -> str:
    """缺少必填项的报错；如果环境里还留着对应的旧名字，明确提示改名，避免对着报错找不到原因。"""
    old_names = [old for old, new in _RENAMED.items() if new == name and str(values.get(old, "")).strip()]
    hint = f"（检测到旧名 {old_names[0]}，已不再支持，请改名为 {name}）" if old_names else ""
    return f"缺少 {name}{hint}"


def _obsolete_notes(values) -> list[str]:
    """环境里还留着的旧名字和已无效的配置，逐条给出处理建议。"""
    notes = []
    for old, new in _RENAMED.items():
        if str(values.get(old, "")).strip():
            notes.append(f"{old} 已不再支持，不会生效；请改名为 {new}")
    if str(values.get("DASHSCOPE_API_KEY", "")).strip() and not str(values.get("DELIVERY_QWEN_API_KEY", "")).strip():
        notes.append("DASHSCOPE_API_KEY 不再被本服务读取；转写 Key 请设置 DELIVERY_QWEN_API_KEY")
    for name, reason in _DEAD_NAMES.items():
        if str(values.get(name, "")).strip():
            notes.append(f"{name} 已不再生效（{reason}），请删除")
    return notes


@dataclass(frozen=True)
class DeliveryImageConfig:
    model: str
    api_token: str
    service_token: str
    allowed_hosts: tuple[str, ...]
    api_mode: str = "responses"
    base_url: str | None = None
    timeout_seconds: float = 45.0
    # 占位默认值：服务启动时由提示词文件里的 version 覆盖（见 DeliveryImageService）
    prompt_version: str = "unknown"
    audio_model: str = "qwen-audio-3.1-asr-flash"
    audio_api_token: str | None = None
    audio_base_url: str = _DEFAULT_AUDIO_BASE_URL
    audio_timeout_seconds: float = 600.0
    # 第二份转写的模型：不带目标号码、不分说话人，只用来核对运单号；None 表示不启用
    audio_second_model: str | None = "qwen3-asr-flash"
    conversation_prompt_version: str = "unknown"
    media_download_timeout_seconds: float = 90.0
    conversation_total_timeout_seconds: float = 900.0

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "DeliveryImageConfig":
        if environ is None:
            # 直接通过 uvicorn 启动时不会经过项目 CLI，显式加载项目根目录 .env；
            # load_dotenv 默认不覆盖已存在的进程环境变量，便于容器注入配置覆盖本地文件。
            local_env = Path.cwd() / ".env"
            project_env = Path(__file__).parents[2] / ".env"
            load_dotenv(dotenv_path=local_env if local_env.exists() else project_env)
            values = os.environ
        else:
            values = environ
        if environ is None:
            for note in _obsolete_notes(values):
                logging.getLogger(__name__).warning("delivery config: %s", note)
        model = _pick(values, "DELIVERY_GPT_MODEL")
        api_token = _pick(values, "DELIVERY_GPT_API_KEY")
        service_token = _pick(values, "DELIVERY_SERVICE_TOKEN")
        api_mode = _pick(values, "DELIVERY_GPT_API_MODE", "responses").lower()
        base_url = _pick(values, "DELIVERY_GPT_BASE_URL") or None
        hosts = tuple(
            host.strip().lower()
            for host in _pick(values, "DELIVERY_ALLOWED_HOSTS").split(",")
            if host.strip()
        )
        if not model:
            raise DeliveryImageConfigError(_missing(values, "DELIVERY_GPT_MODEL"))
        if not api_token:
            raise DeliveryImageConfigError(_missing(values, "DELIVERY_GPT_API_KEY"))
        if not service_token:
            raise DeliveryImageConfigError(_missing(values, "DELIVERY_SERVICE_TOKEN"))
        if not hosts:
            raise DeliveryImageConfigError(_missing(values, "DELIVERY_ALLOWED_HOSTS"))
        if api_mode not in {"responses", "chat_completions"}:
            raise DeliveryImageConfigError("DELIVERY_GPT_API_MODE 只能是 responses 或 chat_completions")
        if any(not _HOST_PATTERN.fullmatch(host) or "." not in host for host in hosts):
            raise DeliveryImageConfigError("DELIVERY_ALLOWED_HOSTS 包含非法主机名")
        hosts = tuple(dict.fromkeys(hosts))
        try:
            timeout_seconds = float(_pick(values, "DELIVERY_GPT_TIMEOUT", "45"))
        except ValueError as exc:
            raise DeliveryImageConfigError("超时配置格式错误") from exc
        if not 1 <= timeout_seconds <= 120:
            raise DeliveryImageConfigError("DELIVERY_GPT_TIMEOUT 必须在1到120秒之间")
        audio_timeout_raw = _pick(values, "DELIVERY_QWEN_TIMEOUT", "600")
        try:
            audio_timeout_seconds = float(audio_timeout_raw)
        except ValueError as exc:
            raise DeliveryImageConfigError("DELIVERY_QWEN_TIMEOUT 配置格式错误") from exc
        if not 1 <= audio_timeout_seconds <= 900:
            raise DeliveryImageConfigError("DELIVERY_QWEN_TIMEOUT 必须在1到900秒之间")
        try:
            media_download_timeout_seconds = float(
                values.get("DELIVERY_MEDIA_DOWNLOAD_TIMEOUT", "90").strip()
            )
        except ValueError as exc:
            raise DeliveryImageConfigError("DELIVERY_MEDIA_DOWNLOAD_TIMEOUT 配置格式错误") from exc
        if not 1 <= media_download_timeout_seconds <= 300:
            raise DeliveryImageConfigError("DELIVERY_MEDIA_DOWNLOAD_TIMEOUT 必须在1到300秒之间")
        try:
            conversation_total_timeout_seconds = float(
                values.get("DELIVERY_CONVERSATION_TOTAL_TIMEOUT", "900").strip()
            )
        except ValueError as exc:
            raise DeliveryImageConfigError("DELIVERY_CONVERSATION_TOTAL_TIMEOUT 配置格式错误") from exc
        if not 1 <= conversation_total_timeout_seconds <= 1800:
            raise DeliveryImageConfigError("DELIVERY_CONVERSATION_TOTAL_TIMEOUT 必须在1到1800秒之间")

        return cls(
            model=model,
            api_token=api_token,
            service_token=service_token,
            allowed_hosts=hosts,
            api_mode=api_mode,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            audio_model=_pick(values, "DELIVERY_QWEN_ASR_MODEL", "qwen-audio-3.1-asr-flash"),
            audio_api_token=_pick(values, "DELIVERY_QWEN_API_KEY") or None,
            audio_base_url=_pick(values, "DELIVERY_QWEN_BASE_URL", _DEFAULT_AUDIO_BASE_URL).rstrip("/"),
            audio_timeout_seconds=audio_timeout_seconds,
            audio_second_model=_second_asr_model(values),
            media_download_timeout_seconds=media_download_timeout_seconds,
            conversation_total_timeout_seconds=conversation_total_timeout_seconds,
        )
