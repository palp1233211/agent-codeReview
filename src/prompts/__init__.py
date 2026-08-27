"""提示词模块 - 从 YAML 配置加载 AgentSpec

支持：
- 基础规则：security, quality, performance, yunxiao_mr
- 业务场景：default, frontend, backend（支持继承）
"""
import yaml
from pathlib import Path
from typing import Any

from ..agents.runtime import AgentSpec

# 提示词配置目录
PROMPTS_DIR = Path(__file__).parent
BUSINESS_DIR = PROMPTS_DIR / "business"


def _load_yaml(path: Path) -> dict[str, Any]:
    """加载 YAML 文件"""
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_agent_definition(name: str) -> AgentSpec:
    """从 YAML 文件加载单个 AgentSpec

    Args:
        name: 规则名称（security, quality, performance, yunxiao_mr）

    Returns:
        AgentSpec 实例
    """
    config = _load_yaml(PROMPTS_DIR / f"{name}.yaml")

    return AgentSpec(
        description=config["description"],
        prompt=config["prompt"],
        tools=tuple(config["tools"]),
    )


def load_business_config(business_type: str) -> dict[str, Any]:
    """加载业务场景配置

    Args:
        business_type: 业务类型（default, frontend, backend）

    Returns:
        业务配置字典，包含 extends 和 custom_prompt
    """
    return _load_yaml(BUSINESS_DIR / f"{business_type}.yaml")


def load_business_agents(business_type: str = "default") -> dict[str, AgentSpec]:
    """根据业务场景加载所有 AgentSpec

    Args:
        business_type: 业务类型（default, frontend, backend）

    Returns:
        AgentSpec 字典，key 为 agent 名称
    """
    business_config = load_business_config(business_type)

    agents: dict[str, AgentSpec] = {}

    # 加载继承的基础规则
    extends = business_config.get("extends", [])
    for base_rule in extends:
        agent_def = load_agent_definition(base_rule)
        agents[f"{base_rule}-reviewer"] = agent_def

    # 如果有自定义提示词，合并到所有 agent 的 prompt 中
    custom_prompt = business_config.get("custom_prompt")
    if custom_prompt:
        for name, agent_def in agents.items():
            # 在原有 prompt 后追加业务特定提示词
            agents[name] = AgentSpec(
                description=agent_def.description,
                prompt=f"{agent_def.prompt}\n\n---\n\n{business_config['description']}特定关注:\n{custom_prompt}",
                tools=agent_def.tools,
            )

    return agents


def get_available_business_types() -> list[str]:
    """获取所有可用的业务类型"""
    return [f.stem for f in BUSINESS_DIR.glob("*.yaml")]


def get_available_base_rules() -> list[str]:
    """获取所有可用的基础规则"""
    return [f.stem for f in PROMPTS_DIR.glob("*.yaml") if f.stem != "__init__"]


# 预加载的默认 Agent（保持向后兼容）
DEFAULT_AGENTS = load_business_agents("default")

# 单独导出的 AgentSpec（向后兼容）
SECURITY_AGENT = load_agent_definition("security")
QUALITY_AGENT = load_agent_definition("quality")
PERFORMANCE_AGENT = load_agent_definition("performance")
YUNXIAO_MR_AGENT = load_agent_definition("yunxiao_mr")


__all__ = [
    "load_agent_definition",
    "load_business_config",
    "load_business_agents",
    "get_available_business_types",
    "get_available_base_rules",
    "DEFAULT_AGENTS",
    "SECURITY_AGENT",
    "QUALITY_AGENT",
    "PERFORMANCE_AGENT",
    "YUNXIAO_MR_AGENT",
]
