"""妥投审核提示词加载与公共客户确认规则组合。"""

from pathlib import Path
from typing import Any

import yaml


PROMPTS_DIRECTORY = Path(__file__).parent / "prompts"
SHARED_CUSTOMER_CONFIRMATION_PLACEHOLDER = "{shared_customer_confirmation_rules}"


def load_review_prompt(prompt_filename: str) -> tuple[str, str]:
    """加载审核提示词，并注入集中维护的客户收货确认规则。"""
    prompt_config = _load_yaml(PROMPTS_DIRECTORY / prompt_filename)
    shared_config = _load_yaml(PROMPTS_DIRECTORY / "customer_confirmation_rules.yaml")
    prompt = str(prompt_config["prompt"])
    if SHARED_CUSTOMER_CONFIRMATION_PLACEHOLDER not in prompt:
        raise ValueError(f"{prompt_filename} 未引用公共客户收货确认规则")
    prompt = prompt.replace(
        SHARED_CUSTOMER_CONFIRMATION_PLACEHOLDER,
        str(shared_config["prompt"]),
    )
    return prompt, str(prompt_config.get("version", prompt_config.get("prompt_version", "unknown")))


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as prompt_file:
        config = yaml.safe_load(prompt_file)
    if not isinstance(config, dict) or not isinstance(config.get("prompt"), str):
        raise ValueError(f"提示词配置格式错误：{path.name}")
    return config
