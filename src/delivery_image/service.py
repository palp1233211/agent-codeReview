"""妥投图片审核服务编排。"""

from .agent import DeliveryImageAgent
from .config import DeliveryImageConfig
from .schemas import DeliveryImageRequest, DeliveryImageResponse
from src.review_prompt_loader import load_review_prompt


class DeliveryImageService:
    """加载固定 Prompt 并同步完成一个包裹的图片审核。"""

    def __init__(self, config: DeliveryImageConfig, agent: DeliveryImageAgent | None = None) -> None:
        self.config = config
        self.agent = agent or DeliveryImageAgent(config)
        self.prompt, self.prompt_version = load_review_prompt("delivery_image.yaml")

    def review(self, request: DeliveryImageRequest) -> DeliveryImageResponse:
        return DeliveryImageResponse(data=self.agent.evaluate(request, self.prompt))
