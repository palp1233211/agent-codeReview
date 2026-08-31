# Code Review Agent

支持 Claude Agent SDK 与 OpenAI SDK 双运行时的智能代码审查 CLI。云效 MR 审查统一走 Yunxiao MCP：Claude 模式由 Claude Agent SDK 管理 stdio MCP，OpenAI 模式由项目内 HTTP/SSE MCP client 转成普通 function tools。

## 功能特性

- **多维度审查**: 安全漏洞、代码质量、性能问题
- **云效 MR 审查**: 自动审查云效平台的 Merge Request 并添加评论
- **自定义 Tools**: Git diff、复杂度分析、Bandit 安全扫描
- **Yunxiao MCP**: MR 详情、patch set、diff、文件读取和评论发布都通过 `mcp__yunxiao__*` 工具完成
- **Hooks 系统**: PreToolUse 验证、PostToolUse 审计
- **CLI 工具**: 命令行快速审查
- **飞书长连接 Bot**: 可选运行 `lark-bot`，把群消息转发到 Dify，并支持知识缺口流程

## 项目结构

```
my-agent/
├── src/
│   ├── cli/
│   │   ├── __init__.py
│   │   └── main.py             # CLI 入口（支持 Claude/OpenAI 双 SDK）
│   ├── agents/
│   │   ├── reviewer.py         # Code Review Agent（动态加载提示词）
│   │   ├── runtime.py          # Claude/OpenAI provider-neutral runtime
│   │   └── mcp_client.py       # OpenAI 模式 HTTP/SSE MCP client
│   ├── prompts/                # 提示词配置模块
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
│   │   ├── git_tools.py        # Git diff Tools
│   │   ├── complexity.py       # 代码复杂度 Tools
│   │   └── linter.py           # Bandit 安全扫描 Tools
│   ├── hooks/
│   │   └── validation.py       # PreToolUse/PostToolUse Hooks
│   ├── lark/                   # 飞书 WebSocket Bot 和消息发送
│   ├── dify/                   # Dify Chatflow / Dataset client
│   ├── knowledge/              # 知识缺口填充与同步流程
│   └── storage/                # MySQL conversation / KB 状态存储
├── cli.py                      # CLI 入口脚本
├── .env                        # 环境变量配置（API Key 等）
├── tests/
└── requirements.txt
```

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量（编辑 .env 文件）
# 默认使用 Claude Agent SDK
# AGENT_PROVIDER=claude
# ANTHROPIC_API_KEY=your_key_here
#
# 切换到 OpenAI SDK
# AGENT_PROVIDER=openai
# OPENAI_API_KEY=your_key_here
# OPENAI_MODEL=gpt-5.4
# OPENAI_BASE_URL=https://api.openai.com/v1
# OPENAI_API_MODE=responses  # responses 或 chat_completions
# YUNXIAO_MCP_URL=https://openapi-rdc.aliyuncs.com/ai/mcp?toolsets=code-management
# OpenAI 模式由 my-agent 直接连接 HTTP MCP，不要求模型供应商原生支持 type=mcp

# 审查云效 MR
python cli.py yunxiao-mr -r 3865544 -m 968 --business default

# 本地文件审查
python cli.py files src/agents/reviewer.py -d security quality
```

## 服务器部署

服务器上 `venv/` 目录不存在是正常的（被 `.gitignore` 排除）。按以下步骤部署：

```bash
# 1. 克隆代码
git clone <repo_url>
cd my-agent

# 2. 本机部署：创建虚拟环境并安装依赖
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
vim .env  # 配置 API Key、云效 Token 等

# 4. CLI 直接运行
python cli.py yunxiao-mr -r 3865544 -m 968 --business default

# 5. 可选：启动飞书长连接 Bot
python cli.py lark-bot
```

### Docker 镜像选择

```bash
# OpenAI-only：不安装 Node、Claude CLI 或 npm 云效 MCP server
docker build --target openai-runtime -t my-agent:openai .

# 完整镜像：保留 Claude provider 和 Claude stdio Yunxiao MCP 能力
docker build --target full-runtime -t my-agent:full .

# 未指定 --target 时默认构建最后一个 target，即 full-runtime
docker build -t my-agent:full .
```

OpenAI 模式通过 Python 直接连接 HTTP/SSE Yunxiao MCP，因此 OpenAI-only 镜像不需要 Node.js 和 Claude CLI。Claude 模式仍需要完整镜像里的 Node 22、`@anthropic-ai/claude-code` 和 `alibabacloud-devops-mcp-server`。

**生产环境建议**：
- 使用 systemd 或 supervisor 管理 `python cli.py lark-bot` 等长驻进程
- 日志输出到 `/var/log/my-agent/` 目录

## CLI 使用

```bash
# 审查文件
python cli.py files src/agents/reviewer.py src/agents/runtime.py -d security quality

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

# 指定业务类型审查
python cli.py yunxiao-mr -r 2835387 -m 42 --business frontend  # 前端项目规则
python cli.py yunxiao-mr -r 2835387 -m 42 --business backend   # 后端项目规则

# 审查云效 MR 但不自动添加评论
python cli.py yunxiao-mr -r 2835387 -m 42 --no-comment
```

## 云效 MR 审查流程

1. **获取 MR 详情** - 使用 `mcp__yunxiao__get_change_request` 获取标题、描述、分支信息
2. **定位源/目标版本** - 使用 `mcp__yunxiao__list_change_request_patch_sets` 获取 patch set
3. **获取代码差异** - 使用 `mcp__yunxiao__compare` 比较源分支和目标分支
4. **读取变更文件** - 必要时使用 `mcp__yunxiao__get_file_blobs` 读取完整内容
5. **多维度审查** - 调用 security/quality/performance subagents
6. **添加评论** - 使用 `mcp__yunxiao__create_change_request_comment` 在 MR 上添加审查评论

## 审查维度

| 维度 | Subagent | 工具 | 检查内容 |
|------|----------|------|----------|
| 安全 | security-reviewer | security_scan, check_secrets | SQL注入、XSS、敏感信息 |
| 质量 | quality-reviewer | analyze_complexity, check_code_duplication | 命名、结构、复杂度 |
| 性能 | performance-reviewer | analyze_complexity | N+1查询、内存泄漏 |
| 云效MR | yunxiao-mr-reviewer | mcp__yunxiao__get_change_request, mcp__yunxiao__create_change_request_comment | 变更影响、合并风险 |

### 业务场景配置

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
AGENT_PROVIDER=claude  # claude 或 openai

# Claude 模式
ANTHROPIC_API_KEY=your_api_key_here
ANTHROPIC_BASE_URL=

# OpenAI 模式
OPENAI_API_KEY=your_api_key_here
OPENAI_MODEL=gpt-5.4
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_MODE=responses  # responses 或 chat_completions

# 云效 MR 审查
YUNXIAO_ACCESS_TOKEN=your_yunxiao_token_here
YUNXIAO_TOKEN=your_yunxiao_token_fallback
YUNXIAO_ORG_ID=5ea86562f89c9700014a671f
YUNXIAO_MCP_TRANSPORT=http  # http、streamable_http 或 sse；OpenAI 不支持 stdio
YUNXIAO_MCP_URL=https://openapi-rdc.aliyuncs.com/ai/mcp?toolsets=code-management
YUNXIAO_TOOLSETS=code-management

# 本地工具限制
MAX_FILE_SIZE_KB=500
```

## OpenAI-compatible 配置矩阵

| 场景 | 必需配置 | 说明 |
|------|----------|------|
| 官方 OpenAI | `AGENT_PROVIDER=openai`, `OPENAI_API_KEY`, `OPENAI_MODEL` | 默认使用 `OPENAI_API_MODE=responses` |
| OpenAI-compatible | `AGENT_PROVIDER=openai_sdk`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL` | 适合只兼容 OpenAI SDK 的第三方 endpoint |
| Chat Completions only | `OPENAI_API_MODE=chat_completions` | 当 provider 不支持 Responses API function calling 时使用 |
| 云效 MR 审查 | `YUNXIAO_MCP_URL`, `YUNXIAO_ACCESS_TOKEN` 或 `YUNXIAO_TOKEN` | OpenAI 模式支持 HTTP/SSE MCP，不支持 stdio |

云效 MCP 默认发送 `Authorization: Bearer <token>`、`X-Yunxiao-Token: <token>` 和 `X-Devops-Toolsets`。`YUNXIAO_TOOLSETS` 为空时回退到 `code-management`。

## OpenAI-compatible 故障排查

- 缺少 `OPENAI_API_KEY` 或 `OPENAI_MODEL`：启动前会直接失败，先补齐 OpenAI runtime 必需配置。
- `AGENT_PROVIDER=openai_sdk` 缺少 `OPENAI_BASE_URL`：该模式面向第三方兼容 endpoint，必须显式配置 base URL。
- 云效 MR 审查缺少 `YUNXIAO_MCP_URL` 或 token：只有 `yunxiao-mr` 命令会要求云效 MCP 配置，本地 `files` / `diff` 审查不需要。
- provider 不支持 Responses API function calling：设置 `OPENAI_API_MODE=chat_completions` 后重试。
- 云效 MCP 鉴权失败：确认 token 同时可用于 `Authorization` 和 `X-Yunxiao-Token`，并确认 `YUNXIAO_TOOLSETS` 包含 `code-management`。

## 真实验证步骤

```bash
# 1. 基础环境检查
python cli.py files src/agents/reviewer.py -d quality

# 2. OpenAI-compatible 本地工具调用 smoke
AGENT_PROVIDER=openai_sdk OPENAI_API_MODE=responses \
  python cli.py files src/agents/reviewer.py -d quality

# 3. 云效 MR 只读审查，不发评论
AGENT_PROVIDER=openai_sdk OPENAI_API_MODE=responses \
  python cli.py yunxiao-mr -r <repo_id> -m <mr_id> --no-comment
```

记录 provider、`OPENAI_API_MODE`、模型名、云效 MCP 工具清单、退出码和关键错误日志；不要在自动化 smoke test 中发布 MR 评论。

## 开发测试

```bash
# 运行测试
venv/bin/python -m pytest -q -p no:cacheprovider
```

## 自定义业务规则

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
python cli.py yunxiao-mr -r 3865544 -m 42 --business mobile
```
