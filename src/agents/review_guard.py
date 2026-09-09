"""Fail closed when an MR report cites code outside its actual patch."""
import json
import re


class ReviewGuard:
    def __init__(self):
        self.lines = None
        self.commented = False
        self.valid_comment = None

    def capture(self, result):
        self.lines = None
        try:
            payload = json.loads('\n'.join(b['text'] for b in result['content'] if b.get('type') == 'text'))
            diffs = payload['diffs']
            if not isinstance(diffs, list):
                return
            lines = {}
            for item in diffs:
                path = item.get('newPath') or item['oldPath']
                old_path = item.get('oldPath') or path
                old = new = None
                for text in item['diff'].splitlines():
                    match = re.match(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', text)
                    if match:
                        old, new = map(int, match.groups())
                    elif old is not None:
                        if text.startswith('+'):
                            lines[(path, 'new', new)] = text[1:].strip()
                            new += 1
                        elif text.startswith('-'):
                            lines[(old_path, 'old', old)] = text[1:].strip()
                            old += 1
                        elif text.startswith(' '):
                            old += 1
                            new += 1
            self.lines = lines
        except (KeyError, TypeError, ValueError):
            return

    def validate(self, report):
        if self.lines is None:
            return '未取得可验证的 MR diff，禁止输出或发布审查结论。'
        if '## 🤖 AI 代码审查报告' not in report or '### 📊 审查结论' not in report:
            return '报告缺少规定的标题或审查结论。'
        # Each finding must have a machine-checkable exact changed-line quote.
        for block in re.split(r'(?m)^#### (?=\d+\.)', report)[1:]:
            heading = block.splitlines()[0]
            location = re.search(r'`([^`]+):(\d+)`', heading)
            evidence = re.search(r'(?m)^- 变更证据：(new|old) `([^`\n]+)`\s*$', block)
            if not location or not evidence:
                return '正式问题必须提供 文件:行号 和 变更证据：new/old `原始变更行`。'
            path, number = location.groups()
            side, quote = evidence.groups()
            if self.lines.get((path, side, int(number))) != quote.strip():
                return f'正式问题 {path}:{number} 未命中本次 diff 的对应变更行及原文。'
        # A report with counts but no finding blocks must not bypass validation.
        for severity in ('Critical', 'High', 'Medium', 'Low'):
            if re.search(rf'{severity}\s*[:：]?\s*[1-9]\d*', report) and '#### ' not in report:
                return '存在问题统计但缺少可验证的问题条目。'
        return None

    def before(self, name, args):
        if name == 'mcp__yunxiao__compare':
            args['straight'] = 'false'
            args.pop('sourceType', None)
            args.pop('targetType', None)
        if name == 'mcp__yunxiao__create_change_request_comment':
            if self.commented:
                return '已尝试发布评论，禁止重复发布。'
            error = self.validate(str(args.get('content') or ''))
            if error:
                return error
            self.commented = True
            self.valid_comment = str(args.get('content') or '')
        return None
