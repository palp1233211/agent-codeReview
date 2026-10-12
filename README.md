# Code Review Agent

支持 Claude Agent SDK 与 OpenAI SDK 双运行时的智能代码审查 CLI。云效 MR 审查统一走 Yunxiao MCP：Claude 模式由 Claude Agent SDK 管理 stdio MCP，OpenAI 模式由项目内 HTTP/SSE MCP client 转成普通 function tools。

## 功能特性

- **多维度审查**: 安全漏洞、代码质量、性能问题
- **云效 MR 审查**: 自动审查云效平台的 Merge Request 并添加评论
- **自定义 Tools**: Git diff、复杂度分析、Bandit 安全扫描
- **Yunxiao MCP**: MR 详情、patch set、diff、文件读取和评论发布都通过 `mcp__yunxiao__*` 工具完成
- **Hooks 系统**: PreToolUse 验证、PostToolUse 审计
- **CLI 工具**: 命令行快速审查
- **两套飞书机器人**: `lark-bot` 仅处理 Dify 问答；`lark-code-review-bot` 仅处理固定群内 @Code Review 机器人的 Yunxiao MR 链接
- **妥投证据审核 Agent**: 提供同步 HTTP 接口，支持图片审核与录音/录屏对话审核，返回结构化证据结果

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

# 直接使用云效 MR 地址（可省略 -m）
python cli.py yunxiao-mr \
  -r 'https://code.aliyun.com/<org>/<repo>/change/968' \
  --business default

# 本地文件审查
python cli.py files src/agents/reviewer.py -d security quality

# 启动妥投图片与通话审核 HTTP 服务（另一个终端运行）
python -m uvicorn src.delivery_image.api:app --host 0.0.0.0 --port 8000
```

PHP 调用方逐条请求 `POST /v1/delivery-image/reviews`，使用 `Authorization: Bearer $DELIVERY_IMAGE_SERVICE_TOKEN`。

请求包含 `request_id`、`pno`、`country`、`platform`、`direction` 和 `images`；`forward_pno` 为可选正向运单号（退件单与客户沟通时可能使用，TT 退件必须提供）；`recipient_phone` 为可选收件手机号（客户确认类图片需要此参数核对身份）；每个 `images` 元素包含唯一的 `image_id` 和 HTTPS `url`。图片按内容识别格式，支持 JPEG、PNG、GIF、WebP 和 BMP（BMP 由 ffmpeg 转成 PNG 后再送模型；转换失败即该图 `uncertain`）；SVG 是矢量图，不作为证据，会得到明确的"SVG 不支持"原因。图片列表至少包含一张，不限制图片数量。`platform` 支持 `TT`、`SHOPEE`、`LAZADA`、`SHEIN`、`TMALL`；`LAZADA` 采用当前生效的新版规则；`direction` 为 `forward`（正向）或 `return`（退件）。为匹配 DNR 规则，调用方应提供平台和方向；缺失时整体结论返回 `uncertain`。服务按数组顺序逐张审核，每次只向模型发送一张图；遇到一张单独满足要求的图片后立即通过整单并停止审核后续图片。响应 `image_results` 逐图包含 `evidence_type`、`policy_match`、`review_status`、`conclusion` 和 `findings`；未审核图片标记为 `skipped`。

**多图联合复核**：有些举证要求单张图无法满足（LAZADA 现场照 AWB/背景/GPS 三选二、SHOPEE LINE/微信还需客户回复已收到，或现场照与面单照片需要互相关联）。逐张审核结束后，如果没有任何一张单独通过，且存在可联合核验的候选图，服务会自动把候选图（最多 5 张）一起送模型做一次联合复核，模型逐项输出 `condition_checks` 并指明每个条件由哪张图支持；通过与否由代码裁决：校验引用的图片存在、运单号/手机号比对一致、满足规则表阈值。响应的 `joint_review` 记录主证据、候选图、逐项核对和未通过原因。`conditional` 状态的图片永远不能单张通过；TT 和退件方向的客服回访录音需要录屏加录音，图片接口无法满足，不会触发联合复核。规则表见 `src/delivery_image/policy.py`。`findings` 会保留图片中实际识别到的运单号、手机号原文及其比对状态；字段模糊时记录可辨认字符和不确定位置。

录音和录屏使用独立接口 `POST /v1/delivery-conversation/reviews`，不能把视频 URL 放入图片的 `images` 数组。请求体示例：

```json
{
  "request_id": "local-test-001",
  "pno": "THT01435EPYZ56Z",
  "country": "TH",
  "platform": "SHOPEE",
  "direction": "forward",
  "recipient_phone": "0991812244",
  "media_files": [
    {
      "media_id": "media-1",
      "media_type": "video",
      "url": "https://fle-asset.example.com/callback.mp4"
    }
  ]
}
```

`media_type` 为 `audio` 或 `video`。服务从传入的 HTTPS 媒体地址下载文件；若为视频，先在服务端提取音轨并转换为 16 kHz 单声道 WAV，再调用 `qwen-audio-3.1-asr-flash` 同步接口，启用 `speaker_diarization_enabled`，携带目标运单热词并保留说话人标签。随后由图片审核配置的模型分析说话人角色、目标运单号和客户收货表达；只有号码匹配且客户本人明确说已收到包裹时才返回 `supports_delivery`。响应逐项返回 `evidence_type`（`courier_call_video`/`courier_call_audio`）、`policy_match`（按平台和方向准入矩阵由代码给出：`accepted`、`rejected`、`conditional`，缺平台或方向为 `unknown`；TT 退件视频未传 `forward_pno` 时为 `conditional`）、转写、识别到的号码、匹配状态、客户收货原话和时间点，`evidence_type`/`policy_match` 与图片结果字段对齐；说话人不明、号码听不清或模型失败时返回 `uncertain`。该流程仅处理 5 分钟以内的媒体，容器需要安装 `ffmpeg`/`ffprobe`。按文件内容（而非扩展名）识别格式，音频支持 WAV、MP3、FLAC、OGG、AMR（NB/WB），视频支持 MP4/MOV/3GP（`ftyp` 容器）、WebM/MKV 和 AVI；其他格式返回 `uncertain`。`DELIVERY_AUDIO_MODEL` 默认是 `qwen-audio-3.1-asr-flash`，`DELIVERY_AUDIO_BASE_URL` 默认是 `https://maas.qianwenaiapi.com/api/v1`；语义审核复用图片审核的模型、API 地址、Token 和 API 模式；转写 Token 仍由 Qwen ASR 配置提供。媒体下载默认最多等待 90 秒，可通过 `DELIVERY_MEDIA_DOWNLOAD_TIMEOUT` 调整；每个请求最多20个媒体文件，源文件及提取后的音频各自不超过24 MiB；Qwen 转写默认最多等待 600 秒，每单默认总时限 900 秒，可分别通过 `DELIVERY_AUDIO_TIMEOUT` 和 `DELIVERY_CONVERSATION_TOTAL_TIMEOUT` 调整。

音视频接口的 `platform` 与图片接口使用相同代码，支持 `TT`、`SHOPEE`、`LAZADA`、`SHEIN`、`TMALL`，大小写不敏感。`LAZADA` 使用当前生效的新版规则；通话视频在除 `LAZADA` 外的平台可采纳，通话音频仅在 `SHOPEE`、`SHEIN`、`TMALL` 可采纳。省略 `platform` 时仍处理转写，但不会自动判定妥投。

音视频接口同样接收 `direction`（`forward` 正向 / `return` 退件），取值与图片接口一致；缺失 `direction` 返回 `uncertain`（仍会转写并如实记录号码和收货表达）。准入矩阵以飞书《DNR举证要求》为准，按方向区分：
- 正向：电话视频除 `LAZADA` 外可采纳，电话音频仅 `SHOPEE`/`SHEIN`/`TMALL` 可采纳（`LAZADA` 执行 0910 新规则）。
- 退件：`LAZADA` 沿用原规则，不适用 0910 修改。电话视频五个平台均可采纳（`TT` 须按正向单号沟通），电话音频除 `TT` 外可采纳。

退件单与客户沟通时可能使用正向运单号，音视频和图片接口都可传可选参数 `forward_pno`：退件方向下通话/截图中的运单号与 `pno` 或 `forward_pno` 任一完整一致即视为运单一致，`forward_pno` 同时作为 ASR 热词；`TT` 退件的电话视频、短信、WhatsApp 必须与 `forward_pno` 一致，未传 `forward_pno` 时返回 `uncertain`。正向方向不使用 `forward_pno`。

本地视频如果只审核对话，可先在本机提取音轨并压缩成单声道 MP3，再上传到审核容器可访问的 HTTPS 对象存储，把返回的直连地址作为 `media_files[].url`，并将 `media_type` 设为 `audio`。例如：

```bash
ffmpeg -hide_banner -i "$HOME/Downloads/call.mp4" -map 0:a:0 -vn -ac 1 -ar 16000 -c:a libmp3lame -b:a 64k "$HOME/Downloads/call-audio.mp3"
```

不要把本机路径当作 URL 传入；服务运行在容器中，无法直接读取宿主机文件。对象存储域名必须在 `DELIVERY_IMAGE_ALLOWED_HOSTS` 中，音频需不超过 24 MiB、时长不超过 5 分钟。只上传音频后，审核无法检查视频画面中的号码或其他视觉信息。

视频手机号识别复用图片审核的模型、地址及 Token 配置，从已下载视频的全程均匀抽帧（约每 4 秒一帧，至少 3 帧、至多 6 帧，首尾必取），一次发送给视觉模型。通话界面可能在任意时刻出现，所以不只取首、中、尾。比对规则：以画面中完整可读的通话对方号码为准；转场或遮挡造成的不可读帧，只要可见数字与完整号码不矛盾就忽略，矛盾或出现多个不同完整号码则为 `uncertain`。请求可传 `recipient_phone`（与图片接口同名）。每个 `media_results` 返回 `detected_phone_numbers`（实际读到的原文）、`phone_observations`（画面时间点、通话对方标记、可读性）、`phone_sample_times` 和 `phone_match`。泰国本地号码与 `+66` 国际格式可归一化比对；其他国家当前只比较完整国际号码。

`phone_match` 为 `matched`、`mismatch`、`uncertain` 或纯音频的 `not_applicable`。遮挡、缺少本单手机号、多个不同通话号码、未识别到通话对方号码均无法核验；不会用参数补全识别原文。有限采样可能错过短暂出现的号码。此阶段手机号结果独立输出，尚未接入整单 `decision` 的通过条件；画面识别失败会保留已有转写结果。

## 服务器部署

生产环境推荐使用 Docker 的 OpenAI runtime：它包含云效 MCP 审查、飞书 Bot、Dify 和 MySQL 功能，但不安装 Claude SDK、Claude CLI 或 Node.js。服务器宿主机不需要安装 Python 或执行 `pip install`。

```bash
# 1. 拉取代码并配置运行时变量（.env 不进入镜像）
git clone <repo_url> /opt/my-agent
cd /opt/my-agent
cp .env.example .env
vim .env

# 2. 构建完整 OpenAI 应用镜像
docker build --target openai-runtime -t my-agent:openai .

# 3. 启动三个常驻容器：两个飞书应用和妥投证据审核 HTTP 服务
docker run -d \
  --name my-agent-dify \
  --restart unless-stopped \
  --env-file /opt/my-agent/.env \
  -v /opt/my-agent:/app \
  my-agent:openai \
  python cli.py lark-bot

docker run -d \
  --name my-agent-code-review \
  --restart unless-stopped \
  --env-file /opt/my-agent/.env \
  -v /opt/my-agent:/app \
  my-agent:openai \
  python cli.py lark-code-review-bot

docker run -d \
  --name my-agent-delivery-image \
  --restart unless-stopped \
  --env-file /opt/my-agent/.env \
  -p 8000:8000 \
  -v /opt/my-agent:/app \
  my-agent:openai \
  python -m uvicorn src.delivery_image.api:app --host 0.0.0.0 --port 8000

# 4. 查看服务状态和日志
docker ps --filter name=my-agent
docker logs --tail 100 -f my-agent-dify
docker logs --tail 100 -f my-agent-code-review
curl http://127.0.0.1:8000/health
```

### Docker 镜像选择

```bash
# 完整应用功能 + OpenAI：不安装 Node、Claude CLI 或 Claude SDK
docker build --target openai-runtime -t my-agent:openai .

# 完整镜像：保留 Claude provider 和 Claude stdio Yunxiao MCP 能力
docker build --target full-runtime -t my-agent:full .

# 未指定 --target 时默认构建最后一个 target，即 full-runtime
docker build -t my-agent:full .
```

OpenAI 模式通过 Python 直接连接 HTTP/SSE Yunxiao MCP，因此 OpenAI runtime 不需要 Node.js 和 Claude CLI；它仍保留飞书、Dify、MySQL、知识库和本地审查功能。Claude 模式仍需要完整镜像里的 Node 22、`@anthropic-ai/claude-code` 和 `alibabacloud-devops-mcp-server`。

### 服务器更新与生效流程

生产环境默认将宿主机项目目录挂载到容器 `/app`。这样镜像只提供 Python 和依赖，代码、提示词及 `.env` 由 `/opt/my-agent` 直接提供。

| 变更类型 | 操作 | 是否重新构建镜像 | 是否重启容器 |
|---|---|---:|---:|
| 只执行一次云效 MR 审查 | `docker exec my-agent python cli.py yunxiao-mr ...` | 否 | 否 |
| 修改 Python、提示词或 `/opt/my-agent/.env` | 后续 `docker exec` 自动读取新文件；三个常驻服务都要重启重新加载 | 否 | 是 |
| 修改 `requirements-openai.txt`、`Dockerfile` 或系统依赖 | 重新 `docker build`，再删除并重新创建容器 | 是 | 是 |

修改 Python、提示词或 `.env` 后：

```bash
cd /opt/my-agent

# 下一次 docker exec 审查会直接使用挂载目录中的新代码和 .env。
# 两个飞书 Bot 都是已启动的 Python 进程，需要重启以加载新代码/配置。
docker restart my-agent-dify my-agent-code-review my-agent-delivery-image
docker logs --tail 100 -f my-agent-dify
docker logs --tail 100 -f my-agent-code-review
```

修改 `requirements-openai.txt`、Dockerfile 或系统依赖后：

```bash
cd /opt/my-agent
docker build --target openai-runtime -t my-agent:openai .
docker rm -f my-agent-dify my-agent-code-review my-agent-delivery-image
docker run -d \
  --name my-agent-dify \
  --restart unless-stopped \
  --env-file /opt/my-agent/.env \
  -v /opt/my-agent:/app \
  my-agent:openai \
  python cli.py lark-bot
docker run -d \
  --name my-agent-code-review \
  --restart unless-stopped \
  --env-file /opt/my-agent/.env \
  -v /opt/my-agent:/app \
  my-agent:openai \
  python cli.py lark-code-review-bot
docker run -d \
  --name my-agent-delivery-image \
  --restart unless-stopped \
  --env-file /opt/my-agent/.env \
  -p 8000:8000 \
  -v /opt/my-agent:/app \
  my-agent:openai \
  python -m uvicorn src.delivery_image.api:app --host 0.0.0.0 --port 8000
```

重建后的验证：

```bash
docker ps --filter name=my-agent
docker logs --tail 100 my-agent-dify
docker logs --tail 100 my-agent-code-review
docker logs --tail 100 my-agent-delivery-image
curl http://127.0.0.1:8000/health

# 云效只读审查：确认正常后再去掉 --no-comment 发布评论
docker exec my-agent \
  python cli.py yunxiao-mr -r <repo_id> -m <mr_id> --no-comment
```

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

# 直接传 MR 地址；仓库和 MR 编号会自动解析
python cli.py yunxiao-mr \
  -r 'https://code.aliyun.com/<org>/<repo>/change/42' \
  -o 5ea86562f89c9700014a671f

# 指定业务类型审查
python cli.py yunxiao-mr -r 3865544 -m 42 --business frontend  # 前端项目规则
python cli.py yunxiao-mr -r 3865544 -m 42 --business backend   # 后端项目规则

# 审查云效 MR 但不自动添加评论
python cli.py yunxiao-mr -r 3865544 -m 42 --no-comment
```

## 云效 MR 审查流程

1. **获取 MR 详情** - 使用 `mcp__yunxiao__get_change_request` 获取标题、描述、分支信息
2. **定位源/目标版本** - 使用 `mcp__yunxiao__list_change_request_patch_sets` 获取 patch set
3. **获取代码差异** - 使用 `mcp__yunxiao__compare`，显式指定 `straight="false"`，从 merge-base 比较到 MR 源版本，避免将目标分支独有变更算入本次 MR
4. **读取变更上下文** - 必要时使用 `mcp__yunxiao__get_file_blobs` 读取变更文件；历史代码仅帮助理解，不单独报告历史缺陷
5. **多维度审查** - 调用 security/quality/performance subagents
6. **添加评论** - 使用 `mcp__yunxiao__create_change_request_comment` 在 MR 上添加审查评论

OpenAI Responses / Chat Completions 运行时会强制上述 compare 参数，并在输出报告及调用评论接口前校验每条正式问题的文件、行号、old/new 侧和变更行原文。不匹配本次 diff 时拒绝输出或发布。该校验保障证据位置，问题是否由本次修改引入仍需模型结合前后逻辑判断。`--no-comment` 会禁用评论工具；评论回执及工具错误不会被读取内容预算截断。

## Code Review 群反馈

用户必须直接引用机器人发出的 Review 结果消息来反馈，普通文本或引用其他消息不会进入该流程。机器人依据被引用消息的 `parent_id` 精确关联 `lark_code_review_tasks.review_message_id`，随后使用当前配置的 AI Provider（不调用 Dify）判断原 Review 是 `correct`、`false_positive` 或 `uncertain`。

反馈原文、判断结论、置信度及误报原因会写入独立的 `lark_code_review_feedback` 表；只有判为 `false_positive` 时才记录误报分类和原因摘要，供后续优化审查提示词。分析完成后，机器人会直接回复该用户的反馈消息，并将“用户原话 + AI 反馈分析”作为原 Yunxiao MR Review 评论的子回复同步。系统按保存的原评论正文精确匹配目标评论，匹配不到或命中多条时不会猜测目标；只有结论为 `false_positive` 且子回复发布成功后，才会将该原评论标记为已解决。同步结果写入 `analysis_raw.yunxiao_sync`，失败原因保留在 `error_message`。

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
YUNXIAO_MCP_TIMEOUT=60
# 进入模型上下文前的 MCP 结果保护（字符数）
YUNXIAO_MCP_CONTEXT_MAX_CHARS=160000
YUNXIAO_MCP_RESULT_MAX_CHARS=60000

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

MR 审查优先使用 diff；读取完整文件时，默认单个 MCP 结果最多保留 60000 个字符，所有 MCP 结果最多保留 160000 个字符。超过限制的内容会带有截断标记，Agent 应继续基于已有 diff 审查。可通过 `YUNXIAO_MCP_CONTEXT_MAX_CHARS` 和 `YUNXIAO_MCP_RESULT_MAX_CHARS` 调整。

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

音视频对话语义分析现直接复用图片审核的 `DELIVERY_IMAGE_MODEL`、`DELIVERY_IMAGE_BASE_URL`、`DELIVERY_IMAGE_API_TOKEN` 和 `DELIVERY_IMAGE_API_MODE`；原 `DELIVERY_CONVERSATION_ANALYSIS_*` 配置不再控制该步骤。响应 `analysis_model` 返回实际使用的图片模型名。语音转写仍使用独立的 Qwen ASR 配置。

音视频下载单次读取等待上限为 30 秒；连接失败、连接超时、读取失败或读取超时最多重试两次（共三次尝试），退避为 0.5 秒、1 秒。所有尝试共用 `DELIVERY_MEDIA_DOWNLOAD_TIMEOUT`，并受整单剩余时限约束；HTTP 状态错误、文件过大和格式错误不重试。
