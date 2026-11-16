"""把 Markdown 回答转成飞书卡片（JSON 2.0）。

飞书的「文本消息」不渲染 Markdown：`**加粗**`、`` `代码` ``、表格都会原样显示。
卡片 JSON 2.0 的 markdown 组件支持标准 Markdown（加粗、行内代码、代码块、列表、引用、表格），
所以把回答放进卡片里发送。官方文档里的限制：
- 一个 markdown 组件里最多 4 张表格，超过的要拆成多个组件；
- 卡片请求体不能超过 30KB，这里留余量，超出就抛 ValueError，由调用方降级成纯文本。

纯函数，不依赖飞书 SDK，可以直接单元测试。
"""
import json
import re

# 官方文档：单个富文本（markdown）组件最多 4 张表格
MAX_TABLES_PER_ELEMENT = 4
# 飞书卡片请求体上限约 30KB，留出外层结构的余量
MAX_CARD_BYTES = 28_000

_EMPTY_PLACEHOLDER = "（无内容）"
_FENCE = re.compile(r"^\s*(```|~~~)")
# 表格分隔行：只由 | : - 和空格组成，且至少有一个 | 和一个 -，例如 |---|:--:|---:|
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def _is_table_separator(line: str) -> bool:
    return "|" in line and "-" in line and bool(_TABLE_SEPARATOR.match(line))


def _split_blocks(lines: list[str]) -> list[tuple[str, list[str]]]:
    """把文本切成 ("text", 行列表) 和 ("table", 行列表) 两类块；代码围栏里的竖线不算表格。"""
    blocks: list[tuple[str, list[str]]] = []
    text: list[str] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):
            in_fence = not in_fence
            text.append(line)
            i += 1
            continue

        is_table_start = (
            not in_fence
            and "|" in line
            and i + 1 < len(lines)
            and _is_table_separator(lines[i + 1])
        )
        if not is_table_start:
            text.append(line)
            i += 1
            continue

        if text:
            blocks.append(("text", text))
            text = []
        table = [line, lines[i + 1]]
        i += 2
        while i < len(lines) and lines[i].strip() and "|" in lines[i]:
            table.append(lines[i])
            i += 1
        blocks.append(("table", table))

    if text:
        blocks.append(("text", text))
    return blocks


def _group_blocks(blocks: list[tuple[str, list[str]]]) -> list[list[tuple[str, list[str]]]]:
    """按「每组最多 4 张表」分组；触发拆分的那张表，它前面的说明文字跟着它去下一组。"""
    groups: list[list[tuple[str, list[str]]]] = [[]]
    tables_in_group = 0
    for block in blocks:
        kind = block[0]
        if kind == "table" and tables_in_group >= MAX_TABLES_PER_ELEMENT:
            carried = []
            if groups[-1] and groups[-1][-1][0] == "text":
                carried.append(groups[-1].pop())
            groups.append(carried)
            tables_in_group = 0
        groups[-1].append(block)
        if kind == "table":
            tables_in_group += 1
    return groups


def _render_group(group: list[tuple[str, list[str]]]) -> str:
    """还原成文本；表格前后保证有空行（没有空行时飞书可能把表格当普通文字）。"""
    out: list[str] = []
    pad_after_table = False
    for kind, lines in group:
        if kind == "table":
            if out and out[-1].strip():
                out.append("")
            out.extend(lines)
            out.append("")
            pad_after_table = True
            continue
        if pad_after_table and lines and not lines[0].strip():
            lines = lines[1:]
        pad_after_table = False
        out.extend(lines)
    return "\n".join(out).strip("\n")


def build_markdown_card(text: str) -> dict:
    """返回卡片 JSON 2.0 的 dict：一个或多个 markdown 组件。"""
    normalised = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not normalised.strip():
        contents = [_EMPTY_PLACEHOLDER]
    else:
        groups = _group_blocks(_split_blocks(normalised.split("\n")))
        contents = [rendered for rendered in (_render_group(g) for g in groups) if rendered.strip()]
    return {
        "schema": "2.0",
        "body": {"elements": [{"tag": "markdown", "content": content} for content in contents]},
    }


def card_json(text: str) -> str:
    """卡片的 JSON 字符串（不转义中文）；超过飞书的请求体上限时抛 ValueError。"""
    payload = json.dumps(build_markdown_card(text), ensure_ascii=False)
    size = len(payload.encode("utf-8"))
    if size > MAX_CARD_BYTES:
        raise ValueError(f"卡片内容过大: {size} 字节，上限 {MAX_CARD_BYTES}")
    return payload
