"""src 模块 - 懒加载避免 CLI 依赖 FastAPI"""

def __getattr__(name: str):
    """懒加载模块成员"""
    if name == "app":
        from .main import app
        return app
    elif name == "CodeReviewAgent":
        from .agents import CodeReviewAgent
        return CodeReviewAgent
    elif name == "default_review_agent":
        from .agents import default_review_agent
        return default_review_agent
    elif name in ("ReviewRequest", "ReviewResult", "ReviewIssue"):
        from .models import ReviewRequest, ReviewResult, ReviewIssue
        return {"ReviewRequest": ReviewRequest, "ReviewResult": ReviewResult, "ReviewIssue": ReviewIssue}[name]
    elif name in ("git_server", "complexity_server", "linter_server"):
        from .tools import git_server, complexity_server, linter_server
        return {"git_server": git_server, "complexity_server": complexity_server, "linter_server": linter_server}[name]
    elif name == "get_hooks_config":
        from .hooks import get_hooks_config
        return get_hooks_config
    elif name == "get_audit_log":
        from .hooks import get_audit_log
        return get_audit_log
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "app",
    "CodeReviewAgent",
    "default_review_agent",
    "ReviewRequest",
    "ReviewResult",
    "ReviewIssue",
    "git_server",
    "complexity_server",
    "linter_server",
    "get_hooks_config",
    "get_audit_log",
]