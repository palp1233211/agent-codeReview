"""src 模块"""

def __getattr__(name: str):
    if name == "CodeReviewAgent":
        from .agents import CodeReviewAgent
        return CodeReviewAgent
    elif name == "default_review_agent":
        from .agents import default_review_agent
        return default_review_agent
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
    "CodeReviewAgent",
    "default_review_agent",
    "git_server",
    "complexity_server",
    "linter_server",
    "get_hooks_config",
    "get_audit_log",
]
