import json

from src.agents.review_guard import ReviewGuard
from src.agents.runtime import _compact_mcp_result, _guard_rejected_result, _tool_error_message


def guard():
    g = ReviewGuard()
    g.capture({'content': [{'type': 'text', 'text': json.dumps({'diffs': [
        {'newPath': 'a.php', 'diff': '@@ -10,2 +10,2 @@\n unchanged\n-old\n+new'}
    ]})}]})
    return g


def report(path='a.php', line=11, side='new', quote='new'):
    return (f'## 🤖 AI 代码审查报告\n#### 1. [问题] `{path}:{line}`\n'
            f'- 变更证据：{side} `{quote}`\n### 📊 审查结论\nHigh 1')


def test_guard_rejects_historical_file_and_context_line():
    g = guard()
    assert g.validate(report('historical.php'))
    assert g.validate(report(line=10, quote='unchanged'))
    assert g.validate(report(quote='invented'))
    assert g.validate(report()) is None
    assert g.validate(report(side='old', quote='old')) is None


def test_compare_always_uses_merge_base_and_comments_are_validated():
    g = guard()
    args = {'straight': 'true', 'sourceType': 'branch'}
    g.before('mcp__yunxiao__compare', args)
    assert args == {'straight': 'false'}
    tool = 'mcp__yunxiao__create_change_request_comment'
    rejection = g.before(tool, {'content': report('historical.php')})
    assert rejection
    assert '未向 Yunxiao 发起请求' in rejection
    assert not g.commented
    assert g.before(tool, {'content': report()}) is None
    assert g.before(tool, {'content': report()})
    assert g.valid_comment == report()


def test_comment_receipt_and_errors_survive_exhausted_budget():
    result = {'content': [{'type': 'text', 'text': '{"id": "123"}'}]}
    assert _compact_mcp_result('mcp__yunxiao__create_change_request_comment', result, max_chars=0) == result
    error = {'isError': True, 'content': [{'type': 'text', 'text': 'failed'}]}
    assert _compact_mcp_result('mcp__yunxiao__compare', error, max_chars=0) == error


def test_local_guard_rejection_is_retryable_not_an_unresolved_tool_error():
    result = _guard_rejected_result('本地拦截')

    assert result['local_validation_rejected'] is True
    assert result['retryable_error'] == '本地拦截'
    assert _tool_error_message(result) is None
