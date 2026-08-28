"""推送给 Dify 的正文渲染：本地维护用的元数据注释不能进检索内容。"""
from src.knowledge.render import render_for_dify


def test_strips_source_anchors():
    text = "## 处罚人是什么字段\n\n<!-- src: app/BLL/PunishBLL.php -->\n\n是 punish_user_id。"

    rendered = render_for_dify(text)

    assert "src:" not in rendered
    assert "PunishBLL" not in rendered
    assert "是 punish_user_id。" in rendered


def test_strips_summary_and_updated_comments():
    text = (
        "<!-- summary: 处罚关联信息 运单、pno -->\n\n"
        "## 标题\n\n"
        "<!-- updated: 2026-08-18 -->\n\n"
        "正文。"
    )

    rendered = render_for_dify(text)

    assert "summary:" not in rendered
    assert "updated:" not in rendered
    assert "## 标题" in rendered and "正文。" in rendered


def test_keeps_block_separators_intact():
    """分隔符决定 Dify 的分块边界，剥注释时绝不能动它。"""
    text = (
        "## A\n\n<!-- src: a.php -->\n\n内容A\n\n---\n\n"
        "## B\n\n<!-- src: b.php -->\n\n内容B"
    )

    rendered = render_for_dify(text)

    assert rendered.count("\n---\n") == 1
    blocks = rendered.split("\n---\n")
    assert "内容A" in blocks[0] and "内容B" in blocks[1]


def test_does_not_leave_blank_gaps_where_comments_were():
    text = "## 标题\n\n<!-- src: a.php -->\n<!-- updated: 2026-08-18 -->\n\n正文。"

    rendered = render_for_dify(text)

    assert "\n\n\n" not in rendered


def test_preserves_ordinary_html_comments():
    """只剥我们自己的元数据前缀，别人写的注释不动。"""
    text = "## 标题\n\n<!-- 这是给人看的说明 -->\n\n正文。"

    assert "这是给人看的说明" in render_for_dify(text)


def test_handles_text_without_any_comments():
    text = "## 标题\n\n正文。"

    assert render_for_dify(text) == text


def test_prepends_source_label_to_every_block():
    """摘要模型只看得到块正文，看不到文档名；不带来源行它就无从得知业务名称。"""
    text = "## A\n\n内容A\n\n---\n\n## B\n\n内容B"

    rendered = render_for_dify(text, source_label="本网点处罚统计")

    blocks = rendered.split("\n---\n")
    assert len(blocks) == 2
    for block in blocks:
        assert block.strip().startswith("> 来源：本网点处罚统计")


def test_source_label_does_not_change_block_count():
    text = "## A\n\n内容A\n\n---\n\n## B\n\n内容B\n\n---\n\n## C\n\n内容C"

    rendered = render_for_dify(text, source_label="某业务")

    assert rendered.count("\n---\n") == 2


def test_no_source_line_when_label_is_empty():
    text = "## A\n\n内容A"

    assert "来源：" not in render_for_dify(text, source_label="")
    assert "来源：" not in render_for_dify(text)


def test_does_not_duplicate_existing_source_line():
    """重复推送同一篇时不能越加越多。"""
    text = "> 来源：本网点处罚统计\n\n## A\n\n内容A"

    rendered = render_for_dify(text, source_label="本网点处罚统计")

    assert rendered.count("来源：") == 1


def test_empty_block_gets_no_source_line():
    """被剥空的块直接丢弃，不该只剩一行来源标注。"""
    text = "## A\n\n内容A\n\n---\n\n<!-- src: only.php -->"

    rendered = render_for_dify(text, source_label="某业务")

    assert rendered.count("来源：") == 1
    assert "\n---\n" not in rendered


def test_source_label_precedes_content_after_stripping_metadata():
    text = "## A\n\n<!-- src: a.php -->\n\n内容A"

    rendered = render_for_dify(text, source_label="某业务")

    assert rendered.index("来源：") < rendered.index("## A")
    assert "<!-- src:" not in rendered


def test_drops_title_only_block():
    """只有标题、没有正文的块必须丢掉。

    实测教训：文档开头的 `# 客户投诉平台知识库` 单独成块推上去后，Dify 的摘要模型
    手里只有一个标题，直接编出了「cat=21 对应严重违规，cat=22 对应一般违规」——
    cat=21 实为客户投诉，cat=22 完全不存在。这种块进了库还会参与检索。
    """
    text = "# 客户投诉平台知识库\n\n---\n\n## 真正的知识\n\n<!-- src: a.php -->\n\n有实质内容。"

    rendered = render_for_dify(text, source_label="客诉平台")

    assert "客户投诉平台知识库" not in rendered
    assert "有实质内容。" in rendered
    assert "\n---\n" not in rendered  # 只剩一块，没有分隔符


def test_drops_block_with_only_heading_and_source_label():
    text = "## 光杆标题\n\n---\n\n## 有内容的\n\n正文在此。"

    rendered = render_for_dify(text, source_label="某业务")

    assert "光杆标题" not in rendered
    assert "正文在此。" in rendered


def test_keeps_short_block_that_has_real_content():
    """短不等于空，有正文就得留。"""
    text = "## state 是什么\n\n是审核状态。"

    rendered = render_for_dify(text, source_label="某业务")

    assert "是审核状态。" in rendered


def test_keeps_block_with_table_but_no_prose():
    text = "## 取值表\n\n|值|含义|\n|---|---|\n|1|满意|"

    rendered = render_for_dify(text, source_label="某业务")

    assert "|1|满意|" in rendered


def test_block_reduced_to_nothing_is_dropped():
    """整块只有注释时，推过去会变成空分段，直接丢掉。"""
    text = "## A\n\n内容A\n\n---\n\n<!-- src: only.php -->\n\n---\n\n## C\n\n内容C"

    rendered = render_for_dify(text)

    assert rendered.count("\n---\n") == 1
    assert "内容A" in rendered and "内容C" in rendered
