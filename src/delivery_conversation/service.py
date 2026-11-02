"""加载通话审核提示词并编排音视频对话审核。"""

from dataclasses import is_dataclass, replace

from src.delivery_image.config import DeliveryImageConfig
from src.review_prompt_loader import load_review_prompt

from .agent import ConversationEvidenceAgent
from .schemas import ConversationEvidenceRequest, ConversationEvidenceResponse


class ConversationEvidenceService:
    def __init__(self, config: DeliveryImageConfig, agent: ConversationEvidenceAgent | None = None) -> None:
        self.prompt, self.prompt_version = load_review_prompt("delivery_conversation.yaml")
        # 响应里的 prompt_version 必须是提示词文件里真实的版本，不能靠环境变量声明
        self.config = replace(config, conversation_prompt_version=self.prompt_version) if is_dataclass(config) else config
        self.agent = agent or ConversationEvidenceAgent(self.config)

    async def review(self, request: ConversationEvidenceRequest) -> ConversationEvidenceResponse:
        data = await self.agent.evaluate_async(request, self.prompt)
        return ConversationEvidenceResponse(data=data)
