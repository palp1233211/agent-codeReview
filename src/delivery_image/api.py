"""妥投图片审核 Agent 的同步 HTTP API。"""

from functools import lru_cache

from fastapi import Depends, FastAPI, Header, HTTPException, status

from .config import DeliveryImageConfig, DeliveryImageConfigError
from .logging_setup import configure_delivery_logging
from .schemas import DeliveryImageRequest, DeliveryImageResponse
from .service import DeliveryImageService
from src.delivery_conversation.schemas import ConversationEvidenceRequest, ConversationEvidenceResponse
from src.delivery_conversation.service import ConversationEvidenceService


configure_delivery_logging()

app = FastAPI(title="Delivery Image Review Agent", version="1.0.0")


@lru_cache(maxsize=1)
def get_config() -> DeliveryImageConfig:
    try:
        return DeliveryImageConfig.from_env()
    except DeliveryImageConfigError as exc:
        raise RuntimeError(str(exc)) from exc


@lru_cache(maxsize=1)
def get_service() -> DeliveryImageService:
    return DeliveryImageService(get_config())


@lru_cache(maxsize=1)
def get_conversation_service() -> ConversationEvidenceService:
    return ConversationEvidenceService(get_config())


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    config = get_config()
    if authorization != f"Bearer {config.service_token}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的服务Token")


@app.get("/health")
def health() -> dict[str, str]:
    get_config()
    return {"status": "ok"}


@app.post(
    "/v1/delivery-image/reviews",
    response_model=DeliveryImageResponse,
    dependencies=[Depends(require_service_token)],
)
def review_delivery_image(request: DeliveryImageRequest) -> DeliveryImageResponse:
    return get_service().review(request)


@app.post(
    "/v1/delivery-conversation/reviews",
    response_model=ConversationEvidenceResponse,
    dependencies=[Depends(require_service_token)],
)
async def review_delivery_conversation(request: ConversationEvidenceRequest) -> ConversationEvidenceResponse:
    return await get_conversation_service().review(request)
