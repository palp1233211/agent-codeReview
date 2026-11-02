"""妥投审核请求的日志上下文：让深层函数里的日志（下载被拒绝、URL 被拒绝等）也能带上是哪个请求、哪个运单。

用 ContextVar 保存，不需要把 request_id 一路作为参数传下去（也就不会破坏现有函数签名）。
同步接口在线程池里执行、异步接口在各自的 task 里执行，ContextVar 都按调用隔离，不会串到别的请求。
只记录 request_id 和运单号，不记录手机号、证据地址。
"""

from contextvars import ContextVar

_context: ContextVar[str] = ContextVar("delivery_log_context", default="request_id=- pno=-")


def set_log_context(request_id: str, pno: str) -> None:
    _context.set(f"request_id={request_id} pno={pno}")


def log_context() -> str:
    return _context.get()
