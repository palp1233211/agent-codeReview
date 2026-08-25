"""把本地知识文档渲染成推送给 Dify 的正文。

本地文件里有几类维护用的元数据注释：

    <!-- src: app/BLL/PunishBLL.php -->   源码锚点，用于代码变更时定位过期知识
    <!-- updated: 2026-08-18 -->          更新日期
    <!-- summary: 处罚关联信息 ... -->     Dify Summary Index 的备份

它们只对维护有意义。原样推给 Dify 会被当成正文切进 chunk 里参与 embedding，
既稀释了语义，也会出现在 LLM 的上下文里。所以推送前一律剥掉。

只剥这几个已知前缀，别人手写的普通 HTML 注释保留——那可能是给读文档的人看的。

反过来会**加**一行来源标注：

    > 来源：本网点处罚统计

原因有两个。一是 Dify 的摘要提示词只拿得到 `{language}` 和当前块正文，看不到
文档名，不写进正文它就无从知道业务名称。二是这对检索本身就有价值——一个内容
只有「第一步：查 key 列表」的块，不带业务名时，用户问「本网点处罚统计怎么查」
根本匹配不上。每块独立可回答，是分块检索的基本要求。
"""
from __future__ import annotations

import re

_SEPARATOR = "\n---\n"
_SOURCE_PREFIX = "> 来源："

# 只匹配我们自己的元数据前缀，普通注释不动
_METADATA_COMMENT_RE = re.compile(
    r"[ \t]*<!--\s*(?:src|updated|summary)\s*:.*?-->[ \t]*\n?",
    re.IGNORECASE | re.DOTALL,
)
_EXCESS_BLANK_RE = re.compile(r"\n{3,}")
_HEADING_LINE_RE = re.compile(r"^\s*#{1,6}\s")


def render_for_dify(text: str, *, source_label: str = "") -> str:
    """剥掉元数据注释、给每块补来源标注，保留分块结构。

    整块被剥空的直接丢弃——只剩一行来源标注的空块进了知识库也是垃圾。
    """
    cleaned: list[str] = []

    for block in text.split(_SEPARATOR):
        stripped = _METADATA_COMMENT_RE.sub("", block)
        stripped = _EXCESS_BLANK_RE.sub("\n\n", stripped).strip()
        if not stripped or not _has_body(stripped):
            continue
        cleaned.append(_with_source(stripped, source_label))

    return _SEPARATOR.join(cleaned)


def _has_body(block: str) -> bool:
    """块里除了标题行还有没有实质内容。

    只有标题的块（典型是文档开头的 `# xxx 知识库`）绝不能推上去：摘要模型拿不到
    任何事实，会直接编造。实测它给一个光标题块编出了「cat=21 对应严重违规，
    cat=22 对应一般违规」——前者张冠李戴，后者根本不存在，而且照样参与检索。
    """
    return any(
        line.strip() and not _HEADING_LINE_RE.match(line)
        for line in block.splitlines()
    )


def _with_source(block: str, source_label: str) -> str:
    """在块首加来源标注；已经有了就不重复加（重复推送不能越加越多）。"""
    if not source_label:
        return block
    if block.lstrip().startswith(_SOURCE_PREFIX):
        return block
    return f"{_SOURCE_PREFIX}{source_label}\n\n{block}"
