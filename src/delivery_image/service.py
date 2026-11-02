"""妥投图片审核服务编排。"""

from dataclasses import is_dataclass, replace

from .agent import DeliveryImageAgent
from .config import DeliveryImageConfig
from .schemas import DeliveryImageRequest, DeliveryImageResponse
from src.review_prompt_loader import load_review_prompt


class DeliveryImageService:
    """加载固定 Prompt 并同步完成一个包裹的图片审核。"""

    def __init__(self, config: DeliveryImageConfig, agent: DeliveryImageAgent | None = None) -> None:
        self.prompt, self.prompt_version = load_review_prompt("delivery_image.yaml")
        # 响应里的 prompt_version 必须是提示词文件里真实的版本，不能靠环境变量声明
        self.config = replace(config, prompt_version=self.prompt_version) if is_dataclass(config) else config
        self.agent = agent or DeliveryImageAgent(self.config)

    def review(self, request: DeliveryImageRequest) -> DeliveryImageResponse:
        return DeliveryImageResponse(data=self.agent.evaluate(request, self.prompt))
