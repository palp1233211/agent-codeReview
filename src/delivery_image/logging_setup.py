"""妥投审核服务的日志输出配置。

用 uvicorn 启动时，应用自己的 logger 没有 handler，根 logger 默认只输出 WARNING 及以上，
所以耗时、用量、跳过原因这些 INFO 日志都看不到。这里给两个妥投包挂一个 handler 并放开到 INFO。
可用 DELIVERY_LOG_LEVEL（如 WARNING）调回，不设置就是 INFO。
"""

import logging
import os

_PACKAGES = ("src.delivery_image", "src.delivery_conversation")
_MARK = "_delivery_stream_handler"


def configure_delivery_logging(environ: dict[str, str] | None = None) -> None:
    values = os.environ if environ is None else environ
    level = getattr(logging, values.get("DELIVERY_LOG_LEVEL", "INFO").strip().upper(), logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    for name in _PACKAGES:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        if not any(getattr(handler, _MARK, False) for handler in logger.handlers):
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            setattr(handler, _MARK, True)
            logger.addHandler(handler)
