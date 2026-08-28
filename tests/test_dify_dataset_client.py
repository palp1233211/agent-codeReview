"""DifyDatasetClient 测试。

断言里写死了 Dify 真实的路由形态（create 用单数 document、update 用复数 documents），
这是从 Dify 源码 service_api/dataset/document.py 核实过的，不是笔误。
"""
from unittest.mock import Mock, patch

import pytest

from src.dify.dataset_client import DifyDatasetClient


def _client() -> DifyDatasetClient:
    return DifyDatasetClient(
        base_url="http://localhost/v1", api_key="dataset-test", dataset_id="ds-1"
    )


def _response(status_code: int, payload: dict) -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = payload
    return resp


def test_rejects_app_scoped_key():
    """app- 前缀是应用密钥，打 dataset 接口会 401，构造时就要拦住。"""
    with pytest.raises(ValueError, match="知识库"):
        DifyDatasetClient(
            base_url="http://localhost/v1", api_key="app-xxxx", dataset_id="ds-1"
        )


@patch("src.dify.dataset_client.requests.post")
def test_update_by_text_uses_plural_documents_path(mock_post):
    mock_post.return_value = _response(200, {"document": {"id": "doc-1"}, "batch": "b-1"})

    ref = _client().update_document_by_text(
        document_id="doc-1", name="customer_complaints", text="# 内容"
    )

    url = mock_post.call_args[0][0]
    assert url == "http://localhost/v1/datasets/ds-1/documents/doc-1/update-by-text"
    assert ref.document_id == "doc-1"
    assert ref.batch == "b-1"


@patch("src.dify.dataset_client.requests.post")
def test_update_by_text_always_sends_name_with_text(mock_post):
    """Dify 要求带 text 时 name 必填，漏了会 400。"""
    mock_post.return_value = _response(200, {"document": {"id": "doc-1"}, "batch": "b-1"})

    _client().update_document_by_text(document_id="doc-1", name="abnormalv2", text="# 内容")

    body = mock_post.call_args.kwargs["json"]
    assert body["name"] == "abnormalv2"
    assert body["text"] == "# 内容"


@patch("src.dify.dataset_client.requests.post")
def test_create_by_text_uses_singular_document_path(mock_post):
    mock_post.return_value = _response(200, {"document": {"id": "doc-2"}, "batch": "b-2"})

    _client().create_document_by_text(name="parcel_loss", text="# 内容")

    url = mock_post.call_args[0][0]
    assert url == "http://localhost/v1/datasets/ds-1/document/create-by-text"


@patch("src.dify.dataset_client.requests.post")
def test_create_sends_indexing_technique(mock_post):
    """数据集第一篇文档必须带 indexing_technique，否则 Dify 报错。"""
    mock_post.return_value = _response(200, {"document": {"id": "doc-2"}, "batch": "b-2"})

    _client().create_document_by_text(name="parcel_loss", text="# 内容")

    assert mock_post.call_args.kwargs["json"]["indexing_technique"] == "high_quality"


@patch("src.dify.dataset_client.requests.post")
def test_separator_is_newline_wrapped_to_protect_markdown_tables(mock_post):
    """分隔符必须带前后换行。

    Dify 是纯字符串匹配，裸的 `---` 会命中 markdown 表格分隔行 `|---|---|`，
    把表格从中间切碎——实测一篇两块、含两张表的文档被切成 7 块，
    其中 3 块内容只有一个 `|`。带换行后精确切 2 块。
    """
    mock_post.return_value = _response(200, {"document": {"id": "doc-1"}, "batch": "b-1"})

    _client().update_document_by_text(document_id="doc-1", name="x", text="# 内容")

    rules = mock_post.call_args.kwargs["json"]["process_rule"]["rules"]
    assert rules["segmentation"]["separator"] == "\n---\n"
    assert rules["segmentation"]["separator"] != "---"
    assert mock_post.call_args.kwargs["json"]["process_rule"]["mode"] == "custom"


@patch("src.dify.dataset_client.requests.post")
def test_sends_chinese_doc_language(mock_post):
    """doc_language 决定摘要提示词里 {language} 的取值；不传默认 English，
    中文文档会被生成英文摘要，中文提问匹配不上。"""
    mock_post.return_value = _response(200, {"document": {"id": "doc-1"}, "batch": "b-1"})

    _client().create_document_by_text(name="x", text="# 内容")

    assert mock_post.call_args.kwargs["json"]["doc_language"] == "Chinese"


def test_max_tokens_below_dify_minimum_is_rejected():
    """Dify 要求 max_tokens >= 50，传小了会 ValueError，本地先拦。"""
    with pytest.raises(ValueError, match="max_tokens"):
        DifyDatasetClient(
            base_url="http://localhost/v1",
            api_key="dataset-test",
            dataset_id="ds-1",
            max_tokens=10,
        )


@patch("src.dify.dataset_client.requests.post")
def test_raises_runtime_error_on_non_200(mock_post):
    mock_post.return_value = _response(
        401, {"code": "unauthorized", "message": "Invalid token"}
    )

    with pytest.raises(RuntimeError, match="Invalid token"):
        _client().update_document_by_text(document_id="doc-1", name="x", text="y")


@patch("src.dify.dataset_client.requests.get")
def test_indexing_status_reads_batch_progress(mock_get):
    mock_get.return_value = _response(
        200, {"data": [{"id": "doc-1", "indexing_status": "completed"}]}
    )

    assert _client().indexing_status("b-1") == "completed"

    url = mock_get.call_args[0][0]
    assert url == "http://localhost/v1/datasets/ds-1/documents/b-1/indexing-status"


@patch("src.dify.dataset_client.requests.get")
def test_indexing_status_reports_error_state(mock_get):
    mock_get.return_value = _response(
        200, {"data": [{"id": "doc-1", "indexing_status": "error"}]}
    )

    assert _client().indexing_status("b-1") == "error"


@patch("src.dify.dataset_client.requests.get")
def test_indexing_status_handles_empty_batch(mock_get):
    mock_get.return_value = _response(200, {"data": []})

    assert _client().indexing_status("b-1") == "unknown"


@patch("src.dify.dataset_client.requests.post")
def test_verify_retrievable_true_when_document_appears_in_hits(mock_post):
    """indexing_status=completed 不代表真能查到（实测碰到过 Weaviate 只读时 Dify 照样报完成）。
    这里用推送内容本身做一次检索，确认这篇文档的 segment 真的在结果里。"""
    mock_post.return_value = _response(
        200,
        {"records": [{"segment": {"document_id": "doc-1"}, "score": 0.9}]},
    )

    assert _client().verify_retrievable("doc-1", "旷工处罚的统计逻辑") is True

    url = mock_post.call_args[0][0]
    assert url == "http://localhost/v1/datasets/ds-1/hit-testing"
    body = mock_post.call_args.kwargs["json"]
    assert body["query"] == "旷工处罚的统计逻辑"
    # 不设阈值、不 rerank：只想知道向量存不存在，不想让分数排名掩盖"完全查不到"
    assert body["retrieval_model"]["reranking_enable"] is False
    assert body["retrieval_model"]["score_threshold_enabled"] is False


@patch("src.dify.dataset_client.requests.post")
def test_verify_retrievable_false_when_document_absent_from_hits(mock_post):
    mock_post.return_value = _response(
        200,
        {"records": [{"segment": {"document_id": "some-other-doc"}, "score": 0.9}]},
    )

    assert _client().verify_retrievable("doc-1", "旷工处罚的统计逻辑") is False


@patch("src.dify.dataset_client.requests.post")
def test_verify_retrievable_false_when_no_hits_at_all(mock_post):
    mock_post.return_value = _response(200, {"records": []})

    assert _client().verify_retrievable("doc-1", "旷工处罚的统计逻辑") is False


def test_verify_retrievable_skips_check_when_no_sample_text():
    """没有可用的正文就没法验证，不该由这里误判失败。"""
    assert _client().verify_retrievable("doc-1", "   ") is True
