"""加载通话审核提示词并编排音视频对话审核。"""

from src.delivery_image.config import DeliveryImageConfig
from src.review_prompt_loader import load_review_prompt

from .agent import ConversationEvidenceAgent
from .schemas import ConversationEvidenceRequest, ConversationEvidenceResponse


class ConversationEvidenceService:
    def __init__(self, config: DeliveryImageConfig, agent: ConversationEvidenceAgent | None = None) -> None:
        self.config = config
        self.agent = agent or ConversationEvidenceAgent(config)
        self.prompt, self.prompt_version = load_review_prompt("delivery_conversation.yaml")

    async def review(self, request: ConversationEvidenceRequest) -> ConversationEvidenceResponse:
        data = await self.agent.evaluate_async(request, self.prompt)
        return ConversationEvidenceResponse(data=data)
