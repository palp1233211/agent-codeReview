"""知识盲区流水线的就绪自检。

部署时（尤其是 Docker 挂载宿主机代码库那种）最容易出问题的不是代码，而是
路径没挂上、密钥填错类型、表没建好。这里把这些一次性查清楚，避免等到用户在
飞书里发了 /kb fill 才发现跑不起来。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    label: str
    ok: bool
    detail: str = ""
    fatal: bool = True


def _path_check(label: str, value: str, *, must_contain: tuple[str, ...] = ()) -> list[Check]:
    if not value:
        return [Check(label, False, "未配置")]

    root = Path(value)
    if not root.is_dir():
        return [Check(label, False, f"目录不存在: {value}")]

    checks = [Check(label, True, value)]
    checks += [
        Check(f"  └ {sub}", (root / sub).exists(), "" if (root / sub).exists() else "缺失")
        for sub in must_contain
    ]
    return checks


def run_checks() -> list[Check]:
    checks: list[Check] = []

    checks += _path_check(
        "FBI_REPO_PATH",
        os.environ.get("FBI_REPO_PATH", ""),
        must_contain=("app/controllers", "app/BLL", "app/library/enums.php"),
    )
    checks += _path_check("KNOWLEDGE_DOCS_DIR", os.environ.get("KNOWLEDGE_DOCS_DIR", ""))

    checks += _claude_checks()

    key = os.environ.get("DIFY_DATASET_API_KEY", "")
    checks.append(
        Check(
            "DIFY_DATASET_API_KEY",
            bool(key) and not key.startswith("app-"),
            "填成了应用密钥（app- 前缀）" if key.startswith("app-") else "",
        )
    )
    checks.append(Check("DIFY_DATASET_ID", bool(os.environ.get("DIFY_DATASET_ID"))))

    checks += _database_checks()
    return checks


def _claude_checks(timeout_seconds: int = 90) -> list[Check]:
    """真发一次最小请求验证鉴权。

    只查二进制存在是不够的——凭证过期时 CLI 照样在，agent 却会在跑了几分钟后
    以 `exit code 1` 失败，错误信息还被 SDK 吞掉。这里花几秒换一条明确的报错。
    """
    import subprocess

    claude = shutil.which("claude")
    if claude is None:
        return [Check("claude CLI", False, "PATH 里找不到 claude")]

    endpoint = os.environ.get("ANTHROPIC_BASE_URL", "(官方默认)")
    checks = [Check("claude CLI", True, claude)]

    from .gap_agent import clean_agent_env

    try:
        proc = subprocess.run(
            [claude, "-p", "ok", "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            # 与真实 agent 用同一套干净环境，否则在 Claude Code 会话里跑 doctor
            # 会挂死并误报成"凭证有问题"
            env=clean_agent_env(),
            stdin=subprocess.DEVNULL,
            cwd=os.environ.get("FBI_REPO_PATH") or None,
        )
    except subprocess.TimeoutExpired:
        checks.append(Check("claude 鉴权", False, f"{timeout_seconds}s 内无响应，端点 {endpoint}"))
        return checks

    output = f"{proc.stdout}\n{proc.stderr}".strip()
    failed = proc.returncode != 0 or "authenticate" in output.lower() or "401" in output
    checks.append(
        Check(
            "claude 鉴权",
            not failed,
            (output.splitlines()[0][:120] if failed else f"端点 {endpoint}"),
        )
    )
    return checks


def _database_checks() -> list[Check]:
    import pymysql

    required = ("lark_bot_knowledge_gaps", "kb_documents", "lark_bot_conversations")
    try:
        conn = pymysql.connect(
            host=os.environ["DB_HOST"],
            port=int(os.environ.get("DB_PORT", "3306")),
            user=os.environ["DB_USERNAME"],
            password=os.environ["DB_PASSWORD"],
            database=os.environ["DB_DATABASE"],
            connect_timeout=5,
        )
    except (KeyError, pymysql.MySQLError) as exc:
        return [Check("MySQL 连接", False, str(exc))]

    checks = [Check("MySQL 连接", True, os.environ["DB_HOST"])]
    database = os.environ["DB_DATABASE"]
    with conn.cursor() as cursor:
        for table in required:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name=%s",
                (database, table),
            )
            exists = cursor.fetchone()[0] == 1
            rows = ""
            if exists:
                cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
                rows = f"{cursor.fetchone()[0]} 行"
            checks.append(
                Check(
                    f"表 {table}",
                    exists,
                    rows or "不存在（首次运行 lark-bot 会自动建）",
                    fatal=False,
                )
            )
    conn.close()
    return checks


def format_report(checks: list[Check]) -> tuple[str, bool]:
    """返回 (报告文本, 是否全部通过关键项)。"""
    lines = [f"  {'✅' if c.ok else '❌'} {c.label}" + (f"  {c.detail}" if c.detail else "")
             for c in checks]
    blocking = [c for c in checks if not c.ok and c.fatal]
    return "\n".join(lines), not blocking
