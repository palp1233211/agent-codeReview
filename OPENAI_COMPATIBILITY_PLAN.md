# OpenAI-compatible SDK 后续执行计划

更新日期：2026-08-28

## 目标

在保留 Claude runtime 的前提下，让本项目能够稳定使用 OpenAI SDK 及 OpenAI-compatible 服务，覆盖本地代码审查、子 Agent、工具调用和云效 MCP 审查链路。

## 执行约定

- 按优先级顺序推进；完成一项后将复选框改为 `[x]`，并补充验证证据。
- 新测试必须调用真实项目实现，不能在测试中复制被测逻辑。
- 涉及外部服务的测试分为可重复的 mock/协议测试和真实只读 smoke test。
- 不在自动化测试中向云效提交评论；真实验证默认使用只读或 `--no-comment` 模式。

## Phase 1：OpenAI 多轮工具调用与 MCP 协议测试（当前阶段）

- [x] Responses API：覆盖模型请求工具、执行工具、回传结果、模型生成最终文本的完整多轮链路。
- [x] Responses API：覆盖连续多个工具调用及 function-call 上下文重放。
- [x] Chat Completions：覆盖 assistant tool_calls、tool result、最终文本的完整多轮链路。
- [ ] 两种 API mode：覆盖工具异常、未知工具和达到 `max_turns`。（已完成 `max_turns`，其余待补）
- [x] MCP Streamable HTTP：覆盖 initialize、tools/list、tools/call、鉴权头、流式多事件、严格响应 ID 和 HTTP/JSON-RPC 错误。
- [x] MCP SSE：覆盖 endpoint 建连、notification/progress、目标响应匹配、超时、真实请求后 EOF、close 和 reconnect。
- [x] 明确 stdio 支持边界：OpenAI runtime 仅支持 Streamable HTTP/legacy SSE；Claude stdio 继续由 Claude Agent SDK 管理。
- [x] 对 OpenAI stdio 配置给出明确的不支持错误，不恢复已删除的简化 stdio client。
- [x] MCP 生命周期：验证 HTTP session、HTTP 流响应、SSE 连接/线程及 runtime 持有的 clients 均被关闭。
- [x] 修复 `.gitignore`，确保新增测试可被 Git 发现并在后续提交中进入 CI。
- [ ] 错误结果闭环：`max_turns`、MCP 失败和模型输出不完整时，CLI 不打印成功并返回非零退出码。

### Phase 1 验收标准

- `pytest` 能完成测试收集，不再引用已删除的 `src.models` 或 `clean_agent_env`。
- 新增协议测试在破坏关键状态机或响应匹配逻辑后会失败。
- Responses、Chat Completions、HTTP MCP、SSE MCP 至少各有一条成功路径和一条失败路径。
- 测试完成后不存在遗留 HTTP session 或 SSE 线程。

## Phase 2：Hooks、权限与文件边界

- [ ] 支持 hook matcher 通配符，例如 `mcp__yunxiao__*`。
- [ ] 对齐 OpenAI runtime 的 PreToolUse、PostToolUse、UserPromptSubmit 和 PermissionRequest 行为。
- [ ] 正确执行 `allowed_tools` 与 `disallowed_tools`。
- [ ] `Read`、`Grep`、`Glob` 限制在配置的 `cwd` 内。
- [ ] 覆盖绝对路径、`../` 和符号链接越界测试。

## Phase 3：真实只读兼容验证

- [ ] 启动前检查 OpenAI API、模型、Base URL 和云效 MCP 配置，缺失时 fail-fast。
- [ ] 验证 OpenAI-compatible endpoint 基础请求。
- [ ] 验证云效 MCP initialize 和 tools/list。
- [ ] 使用真实 MR 执行 `--no-comment` 审查。
- [ ] 保存脱敏验证记录：Provider、API mode、模型、工具清单、退出码和关键日志。

## Phase 4：依赖、旧代码和文档收口

- [ ] 决定默认 Provider 是否继续为 Claude。
- [ ] Claude/OpenAI SDK 改为按 Provider 延迟导入或 optional dependencies。
- [ ] 评估 OpenAI-only 镜像是否仍需 Node.js 和 Claude CLI。
- [ ] 统一 Claude/OpenAI 的云效 MCP 配置构建逻辑。
- [ ] 删除或实现 `src/tools/yunxiao_tools.py` 中的占位工具。
- [ ] 清理陈旧测试和已删除模块引用。
- [ ] 修正 README 中不存在的 FastAPI、`run.py`、`src/main.py` 和 models 描述。
- [ ] 更新 CLAUDE.md 的实际 MCP 工具链说明。
- [ ] 补充 OpenAI-compatible 配置矩阵、故障排查和真实验证步骤。

## 完成记录

| 日期 | 完成项 | 验证证据 |
|---|---|---|
| 2026-08-28 | 建立项目内持续执行计划 | 本文件已创建，后续按复选框持续更新 |
| 2026-08-28 | 明确 OpenAI MCP transport 边界 | OpenAI 使用 HTTP/SSE；Claude stdio 由 Claude Agent SDK 负责 |
| 2026-08-28 | 测试不再被版本控制规则排除 | 删除 `.gitignore` 中的 `/tests/*`；文件尚未暂存或提交 |
| 2026-08-28 | OpenAI 多轮状态机首批测试 | `venv/bin/python -m pytest -q tests/test_openai_runtime.py tests/test_mcp_client.py`：8 passed |
| 2026-08-28 | MCP 协议测试第二批 | HTTP SSE 增量解析/响应 ID/多行 data，legacy SSE notification/超时/线程停止；定向测试 17 passed，正常 EOF/重连待补 |
| 2026-08-28 | Runtime MCP 生命周期 | Responses/Chat success/max_turns/模型异常及 tools/list 异常均关闭 clients；关闭失败不会伪报成功或覆盖主异常；定向测试累计 26 passed |
| 2026-08-28 | MCP HTTP/SSE 协议收口 | 鉴权头、401/403/500、严格 ID、EOF/close/reconnect；定向测试累计 33 passed |
| 2026-08-28 | OpenAI stdio 配置边界 | reviewer 和 MCP factory 均明确拒绝 stdio/未知 transport；Claude stdio 不受影响 |
