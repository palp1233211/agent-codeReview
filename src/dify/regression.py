"""Dify 机器人回归测试：用机器人同款接口（已发布版本）跑一批固定问题，检查回答。

改 Dify 提示词/流程后跑一遍，能看出有没有改坏别的问法。判定规则都是确定性的
（是否拒答、必须/禁止出现的字样、字段名泄漏），因为大模型的措辞每次不同，不能比对整段文字。
同一问法可以重复多次（--runs），用来发现偶发误判（比如 Guard 偶尔误拒）。

用法：
    venv/bin/python -m src.dify.regression --runs 3
    venv/bin/python -m src.dify.regression --tag guard --runs 5
    venv/bin/python -m src.dify.regression --id deleted_hint

接口地址和密钥读 DIFY_BASE_URL / DIFY_API_KEY（也就是机器人用的那个应用）。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

import yaml

from src.dify.client import ChatReply, DifyClient

DEFAULT_CASES_PATH = Path(__file__).with_name("regression_cases.yaml")

# Guard 拒答时回答里一定有的这句话（见 Dify 拒答节点）
REFUSE_MARK = "无法回答与业务无关"

# 任何用例都不该出现：说明查询链路本身出错了
# 其中后几项是问题分类器各类别的描述开头：流程接错线时，机器人会把这段描述当回答发出去
_GLOBAL_FORBIDDEN = (
    "查询失败", "内部错误", "Traceback",
    "知识查询：询问", "任务执行：用户", "配置查询：要求", "上下文追问：用户",
    "具体包裹实时查询：", "兜底类别：", "处罚查询：询问",
)

# 字段名泄漏：state=1，或小写下划线标识符（edit_reason、del_reason_desc……）。
# 用前后断言而不是 \b：中文紧挨着英文时 \b 不成立。运单号全是大写，不会误伤。
_JARGON = re.compile(
    r"(?<![A-Za-z0-9_])state\s*=\s*\d"
    r"|(?<![A-Za-z0-9_])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![A-Za-z0-9_])"
)

_CASE_KEYS = frozenset(
    {
        "id", "turns", "expect_refused", "must_contain", "must_contain_any",
        "must_not_contain", "must_not_match", "no_jargon", "tags", "note",
    }
)


@dataclass(frozen=True)
class Case:
    id: str
    turns: tuple[str, ...]
    expect_refused: Optional[bool] = None
    must_contain: tuple[str, ...] = ()
    must_contain_any: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()
    must_not_match: tuple[str, ...] = ()
    no_jargon: bool = False
    tags: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class CaseResult:
    case: Case
    passed: int
    total: int
    failures: tuple[tuple[str, ...], ...] = field(default=())  # 每个失败的那次运行一组


class _Chatter(Protocol):
    def chat(self, user_id: str, query: str) -> ChatReply: ...


def evaluate(case: Case, answer: str) -> tuple[str, ...]:
    """返回这条回答没通过的检查项；空元组表示通过。"""
    failures: list[str] = []
    refused = REFUSE_MARK in answer
    if case.expect_refused is True and not refused:
        failures.append("应该拒答，但正常回答了")
    if case.expect_refused is False and refused:
        failures.append("不该拒答，但被拒了")

    failures += [f"缺少「{s}」" for s in case.must_contain if s not in answer]
    if case.must_contain_any and not any(s in answer for s in case.must_contain_any):
        failures.append("缺少任一：" + " / ".join(case.must_contain_any))
    failures += [f"不该出现「{s}」" for s in (*case.must_not_contain, *_GLOBAL_FORBIDDEN) if s in answer]
    failures += [f"命中禁止的格式 /{p}/" for p in case.must_not_match if re.search(p, answer)]

    if case.no_jargon:
        leaked = sorted({m.group(0) for m in _JARGON.finditer(answer)})
        if leaked:
            failures.append("漏出字段名：" + "、".join(leaked[:5]))
    return tuple(failures)


def _as_tuple(value, key: str, case_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"用例 {case_id}：{key} 必须是字符串列表")
    return tuple(value)


def load_cases(path: Path = DEFAULT_CASES_PATH) -> list[Case]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        raise ValueError("用例文件顶层必须是列表")

    cases: list[Case] = []
    seen: set[str] = set()
    for item in raw:
        case_id = str(item.get("id", "")).strip()
        if not case_id:
            raise ValueError("用例缺少 id")
        unknown = set(item) - _CASE_KEYS
        if unknown:
            raise ValueError(f"用例 {case_id} 有未知字段：{sorted(unknown)}")
        if case_id in seen:
            raise ValueError(f"用例 id 重复：{case_id}")
        seen.add(case_id)

        turns = _as_tuple(item.get("turns"), "turns", case_id)
        if not turns:
            raise ValueError(f"用例 {case_id}：turns 不能为空")
        cases.append(
            Case(
                id=case_id,
                turns=turns,
                expect_refused=item.get("expect_refused"),
                must_contain=_as_tuple(item.get("must_contain"), "must_contain", case_id),
                must_contain_any=_as_tuple(item.get("must_contain_any"), "must_contain_any", case_id),
                must_not_contain=_as_tuple(item.get("must_not_contain"), "must_not_contain", case_id),
                must_not_match=_as_tuple(item.get("must_not_match"), "must_not_match", case_id),
                no_jargon=bool(item.get("no_jargon", False)),
                tags=_as_tuple(item.get("tags"), "tags", case_id),
                note=str(item.get("note", "")),
            )
        )
    return cases


def run_case(case: Case, client_factory: Callable[[], _Chatter], runs: int = 1) -> CaseResult:
    """同一条用例跑 runs 次；每次用全新的用户和会话，多轮用例按顺序发，只判最后一轮的回答。"""
    passed = 0
    failed_runs: list[tuple[str, ...]] = []
    stamp = int(time.time())
    for i in range(runs):
        user_id = f"regression-{case.id}-{stamp}-{i}"
        client = client_factory()
        try:
            reply = None
            for query in case.turns:
                reply = client.chat(user_id, query)
            failures = evaluate(case, reply.answer)
        except Exception as exc:  # noqa: BLE001 - 接口异常也算这一次没通过，不能中断整批
            failures = (f"调用失败：{exc}",)
        if failures:
            failed_runs.append(failures)
        else:
            passed += 1
    return CaseResult(case=case, passed=passed, total=runs, failures=tuple(failed_runs))


def _select(cases: list[Case], ids: list[str], tags: list[str], exclude_tags: Optional[list[str]] = None) -> list[Case]:
    skip = set(exclude_tags or [])
    picked = [
        c for c in cases
        if (not ids or c.id in ids) and (not tags or set(tags) & set(c.tags)) and not (skip & set(c.tags))
    ]
    missing = set(ids) - {c.id for c in cases}
    if missing:
        raise SystemExit(f"没有这些用例：{sorted(missing)}")
    return picked


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Dify 机器人回归测试")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--runs", type=int, default=1, help="每条用例重复次数（查偶发误判用 3~5）")
    parser.add_argument("--id", action="append", default=[], help="只跑指定 id，可多次")
    parser.add_argument("--tag", action="append", default=[], help="只跑带指定标签的用例，可多次")
    parser.add_argument("--exclude-tag", action="append", default=[], help="跳过带指定标签的用例（如 backlog=还没做的功能）")
    parser.add_argument("--pause", type=float, default=1.0, help="两次调用间隔秒数（Dify 网关有并发限制）")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    base_url = os.environ.get("DIFY_BASE_URL", "")
    api_key = os.environ.get("DIFY_API_KEY", "")
    if not base_url or not api_key:
        print("缺少 DIFY_BASE_URL / DIFY_API_KEY", file=sys.stderr)
        return 2

    cases = _select(load_cases(args.cases), args.id, args.tag, args.exclude_tag)
    print(f"接口 {base_url} | {len(cases)} 条用例 × {args.runs} 次\n", flush=True)

    bad = 0
    for case in cases:
        result = run_case(case, lambda: DifyClient(base_url, api_key), runs=args.runs)
        ok = result.passed == result.total
        bad += 0 if ok else 1
        print(f"{'✓' if ok else '✗'} {case.id:<28} {result.passed}/{result.total}", flush=True)
        for failure in result.failures[:2]:
            print(f"    · {'；'.join(failure)}", flush=True)
        time.sleep(args.pause)

    print(f"\n{len(cases) - bad}/{len(cases)} 条用例全部通过")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
