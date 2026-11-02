"""汇总 my-agent 日志里的 `delivery model usage` 行：按调用类型统计次数和平均 token，并列出图片尺寸与输入 token 的对应关系。

用法：
    python scripts/summarize_token_usage.py my-agent.log
    cat my-agent.log | python scripts/summarize_token_usage.py
    python scripts/summarize_token_usage.py my-agent.log --request dnr-1400481   # 只看某个单子

日志只含数字、图片字节数和像素尺寸，不含提示词、图片内容或手机号。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from statistics import mean

_LINE = re.compile(r"delivery model usage (?P<rest>.*)$")
_PAIR = re.compile(r"(\w+)=(\S+)")
_NUMBERS = ("input", "output", "total", "cached", "reasoning")


def parse_line(line: str) -> dict[str, str] | None:
    match = _LINE.search(line)
    return dict(_PAIR.findall(match.group("rest"))) if match else None


def _number(record: dict[str, str], key: str) -> int | None:
    value = record.get(key, "-")
    return int(value) if value.isdigit() else None


def _avg(values: list[int]) -> str:
    return f"{mean(values):,.0f}" if values else "-"


def summarize(lines, request_filter: str | None = None) -> str:
    records = [r for r in map(parse_line, lines) if r and (not request_filter or r.get("request_id", "").startswith(request_filter))]
    if not records:
        return "没有找到 `delivery model usage` 日志行（需要重启 my-agent 后再跑，且日志要保存下来）。"

    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for record in records:
        groups[record.get("call", "?")].append(record)

    out = [f"共 {len(records)} 次模型调用", "", f"{'call':<10}{'次数':>6}{'平均输入':>10}{'平均输出':>10}{'平均推理':>10}{'平均缓存命中':>14}{'总输入':>12}{'总输出':>10}"]
    for call, items in sorted(groups.items()):
        cols = {key: [n for n in (_number(r, key) for r in items) if n is not None] for key in _NUMBERS}
        out.append(
            f"{call:<10}{len(items):>6}{_avg(cols['input']):>10}{_avg(cols['output']):>10}{_avg(cols['reasoning']):>10}"
            f"{_avg(cols['cached']):>14}{sum(cols['input']):>12,}{sum(cols['output']):>10,}"
        )

    # 单图调用：图片尺寸 -> 输入 token，用来确认图片大小对 token 的真实影响（提示词部分每次相同）
    by_size: dict[str, list[int]] = defaultdict(list)
    for record in groups.get("image", []):
        tokens, size = _number(record, "input"), record.get("image_size", "-")
        if tokens is not None and "," not in size:
            by_size[size].append(tokens)
    if by_size:
        out += ["", "单图调用：像素尺寸 → 输入 token（含约相同的提示词部分）", f"{'尺寸':<14}{'次数':>6}{'平均输入':>10}{'最小':>8}{'最大':>8}"]
        for size, tokens in sorted(by_size.items(), key=lambda item: -len(item[1]))[:15]:
            out.append(f"{size:<14}{len(tokens):>6}{_avg(tokens):>10}{min(tokens):>8,}{max(tokens):>8,}")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总 my-agent 的模型 token 用量日志")
    parser.add_argument("logfile", nargs="?", help="日志文件；不给就读标准输入")
    parser.add_argument("--request", help="只统计 request_id 以此开头的调用，如 dnr-1400481")
    args = parser.parse_args()
    if args.logfile:
        with open(args.logfile, encoding="utf-8", errors="replace") as handle:
            print(summarize(handle, args.request))
    else:
        print(summarize(sys.stdin, args.request))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
