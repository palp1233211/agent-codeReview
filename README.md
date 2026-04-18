# Claude Code Review Agent Service

基于 Claude Agent SDK 构建的智能代码审查服务，集成云效平台 MCP 工具。

## 功能特性

- **多维度审查**: 安全漏洞、代码质量、性能问题
- **云效 MR 审查**: 自动审查云效平台的 Merge Request 并添加评论
- **自定义 MCP Tools**: Git diff、复杂度分析、Bandit 安全扫描、云效工具
- **Hooks 系统**: PreToolUse 验证、PostToolUse 审计
- **API 服务**: FastAPI HTTP 接口 + SSE 流式响应
- **CLI 工具**: 命令行快速审查

## 项目结构

```
my-agent/
├── src/
│   ├── main.py                 # FastAPI 服务入口
│   ├── cli/
│   │   ├── __init__.py
│   │   └── main.py             # CLI 入口（基于 Claude Agent SDK）
│   ├── agents/
│   │   └── reviewer.py         # Code Review Agent（动态加载提示词）
│   ├── prompts/                # 🆕 提示词配置模块
│   │   ├── __init__.py         # 加载函数
│   │   ├── security.yaml       # 安全审查规则
│   │   ├── quality.yaml        # 质量审查规则
│   │   ├── performance.yaml    # 性能审查规则
│   │   ├── yunxiao_mr.yaml     # 云效 MR 审查规则
│   │   └── business/           # 业务场景配置
│   │       ├── default.yaml    # 默认组合
│   │       ├── frontend.yaml   # 前端项目规则
│   │       └── backend.yaml    # 后端项目规则
│   ├── tools/
│   │   ├── git_tools.py        # Git diff MCP Tools
│   │   ├── complexity.py       # 代码复杂度 MCP Tools
│   │   ├── linter.py           # Bandit 安全扫描 MCP Tools
│   │   └── yunxiao_tools.py    # 云效 MR MCP Tools
│   ├── hooks/
│   │   └── validation.py       # PreToolUse/PostToolUse Hooks
│   └── models/
│       └── schemas.py          # API 数据模型
├── cli.py                      # CLI 入口脚本
├── run.py                      # 服务启动脚本
├── .env                        # 环境变量配置（API Key 等）
├── tests/
└── requirements.txt
```

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量（编辑 .env 文件）
# 方式 1 - 使用 Anthropic API:
# ANTHROPIC_API_KEY=your_key_here
#
# 方式 2 - 使用阿里云代理:
# ANTHROPIC_BASE_URL=https://coding.dashscope.aliyuncs.com/apps/anthropic
# ANTHROPIC_API_KEY=your_aliyun_token

# 启动服务
python run.py
# 或
uvicorn src.main:app --reload
```

## 服务器部署

服务器上 `venv/` 目录不存在是正常的（被 `.gitignore` 排除）。按以下步骤部署：

```bash
# 1. 克隆代码
git clone <repo_url>
cd my-agent

# 2. 创建虚拟环境并安装依赖
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
vim .env  # 配置 API Key、云效 Token 等

# 4. 运行方式选择

# 方式 A - CLI 直接运行
python cli.py yunxiao-mr -r 3865544 -m 968 --business default

# 方式 B - 启动 API 服务
uvicorn src.main:app --host 0.0.0.0 --port 8000

# 方式 C - 后台服务（推荐生产环境）
nohup uvicorn src.main:app --host 0.0.0.0 --port 8000 > app.log 2>&1 &
```

**生产环境建议**：
- 使用 systemd 或 supervisor 管理 uvicorn 服务进程
- 配置 Nginx 反向代理（HTTPS、负载均衡）
- 日志输出到 `/var/log/my-agent/` 目录

## API 使用

### 通用审查接口

```bash
# 审查 Git diff
curl -X POST http://localhost:8000/review \
  -H "Content-Type: application/json" \
  -d '{"source": {"type": "git_diff", "base_branch": "main", "target_branch": "feature"}}'

# 审查指定文件
curl -X POST http://localhost:8000/review \
  -H "Content-Type: application/json" \
  -d '{"source": {"type": "files", "paths": ["src/main.py"]}}'

# 审查代码片段
curl -X POST http://localhost:8000/review \
  -H "Content-Type: application/json" \
  -d '{"source": {"type": "code_snippet", "code": "def hello(): print(hello)", "language": "python"}}'
```

### 云效 MR 审查接口

```bash
# 审查云效 MR 并自动添加评论
curl -X POST http://localhost:8000/review/yunxiao-mr \
  -H "Content-Type: application/json" \
  -d '{
    "repository_id": "2835387",
    "local_id": "42",
    "organization_id": "5ea86562f89c9700014a671f",
    "auto_comment": true
  }'

# 🆕 指定业务类型审查
curl -X POST http://localhost:8000/review/yunxiao-mr \
  -H "Content-Type: application/json" \
  -d '{
    "repository_id": "2835387",
    "local_id": "42",
    "organization_id": "5ea86562f89c9700014a671f",
    "auto_comment": true,
    "business_type": "frontend"
  }'

# 流式审查云效 MR (SSE)
curl -X POST http://localhost:8000/review/yunxiao-mr/stream \
  -H "Content-Type: application/json" \
  -d '{
    "repository_id": "2835387",
    "local_id": "42"
  }'
```

## CLI 使用

```bash
# 审查文件
python cli.py files src/main.py src/utils.py -d security quality

# 审查 Git diff
python cli.py diff -b main -t feature/my-feature

# 审查代码片段
python cli.py snippet -c "password='hardcoded'" -l python

# 审查云效 MR（默认业务规则）
python cli.py yunxiao-mr \
  -r 2835387 \
  -m 42 \
  -o 5ea86562f89c9700014a671f \
  -d security quality

# 🆕 指定业务类型审查
python cli.py yunxiao-mr -r 2835387 -m 42 --business frontend  # 前端项目规则
python cli.py yunxiao-mr -r 2835387 -m 42 --business backend   # 后端项目规则

# 审查云效 MR 但不自动添加评论
python cli.py yunxiao-mr -r 2835387 -m 42 --no-comment
```

## 云效 MR 审查流程

1. **获取 MR 详情** - 使用 `get_yunxiao_mr` 获取标题、描述、分支信息
2. **获取代码差异** - 使用 `get_yunxiao_mr_diff` 比较源分支和目标分支
3. **读取变更文件** - 使用 `get_yunxiao_file_content` 读取完整内容
4. **多维度审查** - 调用 security/quality/performance subagents
5. **添加评论** - 使用 `comment_on_yunxiao_mr` 在 MR 上添加审查评论

## 审查维度

| 维度 | Subagent | 工具 | 检查内容 |
|------|----------|------|----------|
| 安全 | security-reviewer | security_scan, check_secrets | SQL注入、XSS、敏感信息 |
| 质量 | quality-reviewer | analyze_complexity, check_code_duplication | 命名、结构、复杂度 |
| 性能 | performance-reviewer | analyze_complexity | N+1查询、内存泄漏 |
| 云效MR | yunxiao-mr-reviewer | get_yunxiao_mr, comment_on_yunxiao_mr | 变更影响、合并风险 |

### 🆕 业务场景配置

| 业务类型 | 继承规则 | 额外关注点 |
|---------|---------|-----------|
| default | security + quality + performance | 无 |
| frontend | security + quality | 浏览器兼容性、XSS防护、性能优化、TypeScript |
| backend | security + quality + performance | API安全、数据库性能、并发安全、依赖注入 |

可通过 YAML 配置文件扩展更多业务场景（见 `src/prompts/business/`）。

## Hooks 功能

| Hook 类型 | 功能 | 检查内容 |
|-----------|------|----------|
| PreToolUse | 验证工具调用 | 文件路径安全、危险命令拦截 |
| PostToolUse | 审计执行结果 | 安全问题记录、错误日志 |
| UserPromptSubmit | 增强用户提示 | 添加审查维度提示 |

## 环境变量

```bash
# 必需
ANTHROPIC_API_KEY=your_api_key_here

# 可选
SERVICE_PORT=8000
MAX_FILE_SIZE_KB=500
```

## 开发测试

```bash
# 运行测试
pytest tests/

# 查看审计日志
curl http://localhost:8000/audit-log
```

## 🆕 自定义业务规则

在 `src/prompts/business/` 目录下创建新的 YAML 文件即可添加业务场景：

```yaml
# src/prompts/business/mobile.yaml
name: mobile
description: "移动端项目审查配置"
extends: [security, quality]  # 继承基础规则
custom_prompt: |
  移动端项目额外关注:
  - 原生 API 调用安全
  - 内存管理（避免泄漏）
  - 网络请求优化（弱网场景）
  - UI 线程安全
```

使用方式：
```bash
# CLI
python cli.py yunxiao-mr -r 2835387 -m 42 --business mobile

# API
curl -X POST http://localhost:8000/review/yunxiao-mr \
  -d '{"repository_id": "2835387", "local_id": "42", "business_type": "mobile"}'
```