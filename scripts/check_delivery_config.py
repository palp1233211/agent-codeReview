"""自检妥投审核服务的配置：看进程实际读到了什么、值是否可用。

只有两套模型配置：
    GPT  （DELIVERY_GPT_*） 图片审核、视频抓帧识别手机号、视频对话文本分析
    Qwen （DELIVERY_QWEN_*）视频语音转写

用法（在 my-agent 目录，用和启动 uvicorn 完全相同的环境变量/.env）：
    python scripts/check_delivery_config.py          # 只检查配置，不联网
    python scripts/check_delivery_config.py --live   # 另外联网验证两个 Key

密钥只显示脱敏信息（前缀、长度、末 4 位），永远不打印完整值；联网检查也不打印供应商返回的错误正文。
--live 对转写接口只发一个故意不完整的请求（不产生转写费用，只看鉴权是否通过），
对 GPT 模型发一次 1~16 个 token 的 "ping"（费用可忽略）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.delivery_image.config import (  # noqa: E402
    DeliveryImageConfig,
    DeliveryImageConfigError,
    _obsolete_notes,
)

OK, BAD, WARN = "✅", "❌", "⚠️ "
_problems: list[str] = []


def mask(secret: str | None) -> str:
    if not secret:
        return "（未配置）"
    if len(secret) < 12:
        return f"（长度 {len(secret)}，过短，疑似不是真实密钥）"
    return f"{secret[:3]}…{secret[-4:]}（长度 {len(secret)}）"


def report(ok: bool | None, label: str, detail: str = "") -> None:
    icon = OK if ok else (WARN if ok is None else BAD)
    print(f"  {icon} {label}" + (f"：{detail}" if detail else ""))
    if ok is False:
        _problems.append(f"{label}：{detail}")


def source_of(name: str) -> str:
    """这个配置项是显式设置的，还是用了默认值。"""
    return name if os.environ.get(name, "").strip() else "（未设置，用默认值）"


def safe_code(error) -> str | None:
    """只取格式受限的错误码，不取错误正文（正文可能回显密钥片段）。"""
    code = getattr(error, "code", None)
    body = getattr(error, "body", None)
    if not isinstance(code, str) and isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        code = inner.get("code") if isinstance(inner, dict) else None
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else None


def check_static(config: DeliveryImageConfig) -> None:
    print("\n【1】进程实际读到的配置")
    report(True, "配置能正常加载", "必填项（GPT 模型和 Key、服务 Token、域名白名单）都有")

    print("\n  —— GPT 一套（图片审核、视频抓帧、视频文本分析）——")
    print(f"     模型：{config.model}    接口模式：{config.api_mode}    来源：{source_of('DELIVERY_GPT_MODEL')}")
    print(f"     地址：{config.base_url or '（OpenAI 默认地址）'}    来源：{source_of('DELIVERY_GPT_BASE_URL')}")
    print(f"     Key ：{mask(config.api_token)}    来源：{source_of('DELIVERY_GPT_API_KEY')}")

    print("\n  —— Qwen 一套（只用于视频语音转写）——")
    print(f"     模型：{config.audio_model}    来源：{source_of('DELIVERY_QWEN_ASR_MODEL')}")
    print(f"     地址：{config.audio_base_url}    来源：{source_of('DELIVERY_QWEN_BASE_URL')}")
    print(f"     Key ：{mask(config.audio_api_token)}    来源：{source_of('DELIVERY_QWEN_API_KEY')}")
    if config.audio_api_token is None:
        report(False, "Qwen Key 未配置", "视频无法转写（不会回退使用 GPT 的 Key）；请设置 DELIVERY_QWEN_API_KEY")
    elif config.audio_api_token == config.api_token:
        report(None, "Qwen Key 和 GPT Key 完全相同", "两家的 Key 通常不通用；如果它们确实是同一个网关的 Key 可以忽略")

    print("\n  —— 服务自身 ——")
    print(f"     域名白名单（{len(config.allowed_hosts)} 个，完全匹配，不支持通配符）    来源：{source_of('DELIVERY_ALLOWED_HOSTS')}")
    for host in config.allowed_hosts:
        print(f"       - {host}")
    print(f"     服务 Token：{mask(config.service_token)}    来源：{source_of('DELIVERY_SERVICE_TOKEN')}（ETL 的 delivery_review_token 必须与此一致）")
    print(f"     超时：GPT 单次 {config.timeout_seconds}s（{source_of('DELIVERY_GPT_TIMEOUT')}），"
          f"Qwen 转写 {config.audio_timeout_seconds}s（{source_of('DELIVERY_QWEN_TIMEOUT')}），"
          f"整单 {config.conversation_total_timeout_seconds}s，下载 {config.media_download_timeout_seconds}s")

    notes = _obsolete_notes(os.environ)
    if notes:
        print("\n  —— 需要清理的旧配置（旧名字不会生效）——")
        for note in notes:
            report(None, note)


def check_environment() -> None:
    print("\n【2】运行环境")
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        report(bool(path), f"{tool} 可用", path or "未找到：视频抽帧、转写需要它（brew install ffmpeg）")
    for module, install in (("openai", "requirements-openai.txt"), ("httpx", "requirements.txt")):
        try:
            __import__(module)
            report(True, f"{module} 包已安装")
        except ImportError:
            report(False, f"{module} 包未安装", f"pip install -r {install}")


async def probe_asr(config: DeliveryImageConfig) -> None:
    import httpx

    if not config.audio_api_token:
        report(False, "Qwen 转写无法验证", "未配置 DELIVERY_QWEN_API_KEY")
        return
    url = config.audio_base_url.rstrip("/") + "/services/aigc/multimodal-generation/generation"
    headers = {
        "Authorization": f"Bearer {config.audio_api_token}",
        "Content-Type": "application/json",
        "X-DashScope-SSE": "disable",
    }
    # 故意不带音频：只验证鉴权和路径，不会产生转写费用
    body = {"model": config.audio_model, "input": {"messages": []}}
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(url, headers=headers, json=body)
    except Exception as exc:  # noqa: BLE001
        report(False, "Qwen 转写接口连不上", f"{type(exc).__name__}，检查 DELIVERY_QWEN_BASE_URL 和网络")
        return

    code = None
    try:
        payload = response.json()
        code = payload.get("code") if isinstance(payload, dict) else None
        code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else None
    except Exception:  # noqa: BLE001
        pass
    detail = f"HTTP {response.status_code}" + (f"，code={code}" if code else "")
    if response.status_code in (401, 403):
        report(False, "Qwen Key 被拒绝", detail + "：Key 不属于这个接口地址，或已失效/无权限")
    elif response.status_code == 404:
        report(False, "Qwen 接口或模型不存在", detail + "：检查 DELIVERY_QWEN_BASE_URL、DELIVERY_QWEN_ASR_MODEL")
    elif response.status_code in (400, 422) or 200 <= response.status_code < 300:
        report(True, "Qwen Key 通过鉴权", detail + "（请求体是故意不完整的，所以 400 是预期结果）")
    elif response.status_code == 429:
        report(None, "Qwen 接口限流或额度不足", detail)
    else:
        report(None, "Qwen 接口返回了意外状态", detail)


def probe_gpt(config: DeliveryImageConfig) -> None:
    from openai import OpenAI

    client = OpenAI(api_key=config.api_token, base_url=config.base_url, max_retries=0, timeout=30.0)
    try:
        if config.api_mode == "responses":
            client.responses.create(model=config.model, input="ping", max_output_tokens=16)
        else:
            client.chat.completions.create(
                model=config.model, messages=[{"role": "user", "content": "ping"}], max_tokens=1
            )
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status_code", None)
        code = safe_code(exc)
        detail = type(exc).__name__ + (f"，HTTP {status}" if status else "") + (f"，code={code}" if code else "")
        hint = {
            401: "Key 无效，或不属于这个接口地址",
            403: "Key 无权限使用该模型",
            404: "模型名或接口地址不对，或接口模式选错（responses / chat_completions）",
            429: "限流或额度不足",
        }.get(status, "")
        report(False, "GPT 模型调用失败", detail + (f"：{hint}" if hint else ""))
        return
    report(True, "GPT 模型调用成功", f"模型 {config.model}（图片审核、视频抓帧、视频文本分析都用它）")


def check_live(config: DeliveryImageConfig) -> None:
    print("\n【3】联网验证（--live）")
    asyncio.run(probe_asr(config))
    probe_gpt(config)


def main() -> int:
    parser = argparse.ArgumentParser(description="自检妥投审核服务配置")
    parser.add_argument("--live", action="store_true", help="联网验证两个 Key（费用可忽略）")
    args = parser.parse_args()

    print("my-agent 妥投审核配置自检（密钥只显示脱敏信息）")
    try:
        config = DeliveryImageConfig.from_env()
    except DeliveryImageConfigError as exc:
        print(f"\n{BAD} 配置加载失败：{exc}")
        print("   启动 uvicorn 时同样会失败。请在启动 my-agent 的同一个终端、同一份 .env 下运行本脚本。")
        return 1

    check_static(config)
    check_environment()
    if args.live:
        check_live(config)
    else:
        print("\n【3】联网验证：未执行（加 --live 才会验证 Key 是否真的可用）")

    print("\n" + "=" * 60)
    if _problems:
        print(f"{BAD} 发现 {len(_problems)} 个问题：")
        for item in _problems:
            print(f"   - {item}")
        return 1
    print(f"{OK} 没有发现问题" + ("" if args.live else "（未联网，Key 是否有效请加 --live 验证）"))
    print("提示：配置在 my-agent 启动时读取一次，改完环境变量必须重启服务才会生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
