"""妥投图片审核 Agent 的独立运行配置。"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class DeliveryImageConfigError(ValueError):
    """配置缺失或格式不安全。"""


_HOST_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_DEFAULT_AUDIO_BASE_URL = "https://maas.qianwenaiapi.com/api/v1"
_DEFAULT_CONVERSATION_ANALYSIS_BASE_URL = "https://maas.qianwenaiapi.com/compatible-mode/v1"


@dataclass(frozen=True)
class DeliveryImageConfig:
    model: str
    api_token: str
    service_token: str
    allowed_hosts: tuple[str, ...]
    api_mode: str = "responses"
    base_url: str | None = None
    timeout_seconds: float = 45.0
    prompt_version: str = "delivery-image-v4"
    audio_filetrans_model: str = "qwen-audio-3.1-asr-flash-filetrans"
    audio_api_token: str | None = None
    audio_base_url: str = _DEFAULT_AUDIO_BASE_URL
    audio_timeout_seconds: float = 600.0
    conversation_analysis_model: str = "qwen-plus"
    conversation_analysis_api_token: str | None = None
    conversation_analysis_base_url: str = _DEFAULT_CONVERSATION_ANALYSIS_BASE_URL
    conversation_analysis_api_mode: str = "chat_completions"
    conversation_prompt_version: str = "delivery-conversation-v1"
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
        model = values.get("DELIVERY_IMAGE_MODEL", "").strip()
        api_token = values.get("DELIVERY_IMAGE_API_TOKEN", "").strip()
        service_token = values.get("DELIVERY_IMAGE_SERVICE_TOKEN", "").strip()
        api_mode = values.get("DELIVERY_IMAGE_API_MODE", "responses").strip().lower()
        conversation_analysis_api_mode = values.get(
            "DELIVERY_CONVERSATION_ANALYSIS_API_MODE",
            "chat_completions",
        ).strip().lower()
        base_url = values.get("DELIVERY_IMAGE_BASE_URL", "").strip() or None
        hosts = tuple(
            host.strip().lower()
            for host in values.get("DELIVERY_IMAGE_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        )
        if not model:
            raise DeliveryImageConfigError("缺少 DELIVERY_IMAGE_MODEL")
        if not api_token:
            raise DeliveryImageConfigError("缺少 DELIVERY_IMAGE_API_TOKEN")
        if not service_token:
            raise DeliveryImageConfigError("缺少 DELIVERY_IMAGE_SERVICE_TOKEN")
        if not hosts:
            raise DeliveryImageConfigError("缺少 DELIVERY_IMAGE_ALLOWED_HOSTS")
        if api_mode not in {"responses", "chat_completions"}:
            raise DeliveryImageConfigError("DELIVERY_IMAGE_API_MODE 只能是 responses 或 chat_completions")
        if conversation_analysis_api_mode not in {"responses", "chat_completions"}:
            raise DeliveryImageConfigError(
                "DELIVERY_CONVERSATION_ANALYSIS_API_MODE 只能是 responses 或 chat_completions"
            )
        if any(not _HOST_PATTERN.fullmatch(host) or "." not in host for host in hosts):
            raise DeliveryImageConfigError("DELIVERY_IMAGE_ALLOWED_HOSTS 包含非法主机名")
        hosts = tuple(dict.fromkeys(hosts))
        try:
            timeout_seconds = float(values.get("DELIVERY_IMAGE_TIMEOUT", "45"))
        except ValueError as exc:
            raise DeliveryImageConfigError("超时配置格式错误") from exc
        if not 1 <= timeout_seconds <= 120:
            raise DeliveryImageConfigError("DELIVERY_IMAGE_TIMEOUT 必须在1到120秒之间")
        audio_timeout_raw = values.get("DELIVERY_AUDIO_TIMEOUT", "600").strip()
        try:
            audio_timeout_seconds = float(audio_timeout_raw)
        except ValueError as exc:
            raise DeliveryImageConfigError("DELIVERY_AUDIO_TIMEOUT 配置格式错误") from exc
        if not 1 <= audio_timeout_seconds <= 900:
            raise DeliveryImageConfigError("DELIVERY_AUDIO_TIMEOUT 必须在1到900秒之间")
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
            prompt_version=values.get("DELIVERY_IMAGE_PROMPT_VERSION", "delivery-image-v4").strip(),
            audio_filetrans_model=values.get(
                "DELIVERY_AUDIO_FILETRANS_MODEL",
                "qwen-audio-3.1-asr-flash-filetrans",
            ).strip(),
            audio_api_token=(
                values.get("DELIVERY_AUDIO_API_TOKEN", "").strip()
                or values.get("DASHSCOPE_API_KEY", "").strip()
                or None
            ),
            audio_base_url=(
                values.get("DELIVERY_AUDIO_BASE_URL", _DEFAULT_AUDIO_BASE_URL).strip()
                or _DEFAULT_AUDIO_BASE_URL
            ).rstrip("/"),
            audio_timeout_seconds=audio_timeout_seconds,
            conversation_analysis_model=values.get(
                "DELIVERY_CONVERSATION_ANALYSIS_MODEL",
                "qwen-plus",
            ).strip(),
            conversation_analysis_api_token=(
                values.get("DELIVERY_CONVERSATION_ANALYSIS_API_TOKEN", "").strip()
                or values.get("DASHSCOPE_API_KEY", "").strip()
                or values.get("DELIVERY_AUDIO_API_TOKEN", "").strip()
                or api_token
            ),
            conversation_analysis_base_url=(
                values.get(
                    "DELIVERY_CONVERSATION_ANALYSIS_BASE_URL",
                    _DEFAULT_CONVERSATION_ANALYSIS_BASE_URL,
                ).strip()
                or _DEFAULT_CONVERSATION_ANALYSIS_BASE_URL
            ).rstrip("/"),
            conversation_analysis_api_mode=conversation_analysis_api_mode,
            conversation_prompt_version=values.get(
                "DELIVERY_CONVERSATION_PROMPT_VERSION",
                "delivery-conversation-v1",
            ).strip(),
            media_download_timeout_seconds=media_download_timeout_seconds,
            conversation_total_timeout_seconds=conversation_total_timeout_seconds,
        )
