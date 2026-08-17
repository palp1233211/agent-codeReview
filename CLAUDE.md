# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A CLI-only agent that reviews Yunxiao (云效) Merge Requests via the Claude Agent SDK and posts a single Chinese-language Markdown comment back to the MR. Also includes a standalone Feishu bot (long-connection/WebSocket mode) under `src/lark/`.

## Build and Test Commands

```bash
pip install -r requirements.txt

# Review a Yunxiao MR (auto-comment)
python cli.py yunxiao-mr -r <repo_id> -m <mr_id>

# Review without posting a comment
python cli.py yunxiao-mr -r <repo_id> -m <mr_id> --no-comment

# Start the Feishu long-connection bot (blocks until interrupted)
python cli.py lark-bot

# Tests
pytest tests/
```

## Environment Setup

Copy `.env.example` to `.env` and configure:
- `ANTHROPIC_API_KEY`: Anthropic API key or Alibaba Cloud token
- `ANTHROPIC_BASE_URL`: Optional proxy endpoint (e.g. Alibaba Cloud DashScope)
- `YUNXIAO_ACCESS_TOKEN`: Yunxiao platform access token
- `YUNXIAO_ORG_ID`: Default organization ID for Yunxiao MR operations
- `LARK_APP_ID` / `LARK_APP_SECRET`: Feishu app credentials, used by `LarkClient` (bi-weekly-doc task) and the `lark-bot` WebSocket bot
- `DIFY_BASE_URL` / `DIFY_API_KEY`: local Dify instance + app API key, used by `DifyClient` to forward `lark-bot` messages to a Dify Chatflow
- `DB_HOST` / `DB_PORT` / `DB_USERNAME` / `DB_PASSWORD` / `DB_DATABASE` / `DB_CHARSET`: MySQL connection used by `ConversationStore` to log every `lark-bot` question/answer into `lark_bot_conversations`

## Architecture

### Core Components

- **`src/agents/reviewer.py`**: `CodeReviewAgent` — sole entry point `review_yunxiao_mr()`, builds the prompt from `YUNXIAO_MR_AGENT` and runs `claude_agent_sdk.query` against the yunxiao MCP server.
- **`src/prompts/yunxiao_mr.yaml`**: prompt + allowed tool list for the MR reviewer.
- **`src/prompts/__init__.py`**: thin YAML loader exporting `YUNXIAO_MR_AGENT`.
- **`src/hooks/validation.py`**: PreToolUse / PostToolUse / UserPromptSubmit hooks (path validation, audit log, prompt enrichment).
- **`src/cli/main.py`**: CLI entry — subcommands `yunxiao-mr`, `files`, `diff`, `bi-weekly-doc`, `lark-bot`.
- **`src/lark/client.py`**: `LarkClient` — thin wrapper over the `lark-oapi` SDK (docx copy/update, send text message), built from `LARK_APP_ID`/`LARK_APP_SECRET` via `LarkClient.from_env()`.
- **`src/lark/ws_bot.py`**: `run_ws_bot()` — Feishu long-connection (WebSocket) client. Subscribes to `im.message.receive_v1`; text messages are forwarded to `DifyClient.chat()` and the `answer` is sent back via `LarkClient.send_text_message`. No public callback URL needed; requires the Feishu app's event subscription to be set to "长连接接收事件" with `im.message.receive_v1` added.
  - **Important**: `lark_oapi.ws.Client` dispatches every event on a single asyncio loop and calls the Python handler synchronously (see `_handle_data_frame` in the SDK) — a slow handler delays the WS ACK frame, which makes Feishu's server think delivery failed and **retry-push the same event**, causing duplicate replies. `on_message_receive` therefore only does dedup (`_RecentMessageIds`, keyed by `message_id`) and then hands the actual Dify call + reply off to a background thread so the callback returns immediately. Keep this pattern for any future slow work triggered from an event handler.
- **`src/dify/client.py`**: `DifyClient` — thin wrapper over Dify's `/chat-messages` API (advanced-chat / Chatflow apps only, not `/workflows/run`). Keeps an in-process `{user_id: conversation_id}` map so each Feishu sender's `open_id` gets its own multi-turn conversation; state is lost on process restart. `chat()` returns a `ChatReply(answer, conversation_id)`, with `<think>...</think>` blocks already stripped from `answer` via `strip_think_tags()`.
- **`src/storage/conversation_store.py`**: `ConversationStore` — logs every `lark-bot` Q&A into MySQL table `lark_bot_conversations` (auto-created via `CREATE TABLE IF NOT EXISTS` on `from_env()`). Opens a fresh connection per `log()` call (simple, thread-safe by construction — no pooling needed at this scale); `message_id` has a `UNIQUE KEY` so retried/duplicate events are silently ignored via `INSERT IGNORE`, on top of the in-memory `_RecentMessageIds` dedup in `ws_bot.py`.

### Yunxiao MR Review Flow

The agent runs as a single-layer query (no sub-agent dispatch) so all yunxiao MCP tools are called directly. Tool sequence (via `mcp__yunxiao__*`):

1. `get_change_request` → MR details
2. `list_change_request_patch_sets` → find latest `MERGE_SOURCE` patch set
3. `compare` → branch-to-branch diff
4. `get_file_blobs` → full file content for changed files
5. `create_change_request_comment` → publish review comment (GLOBAL_COMMENT, single call)

### MCP Server Architecture

- **External server**: `yunxiao` — stdio process via `npx alibabacloud-devops-mcp-server`, configured at runtime in `_get_yunxiao_mcp_config()` so `YUNXIAO_ACCESS_TOKEN` is read from the live environment.

## Important Patterns

### Comment Strategy for Yunxiao MR

Per `YUNXIAO_MR_AGENT` prompt requirements:
- **Only ONE** `create_change_request_comment` call per review
- Must use `commentType="GLOBAL_COMMENT"`
- All findings merged into a single Markdown-formatted comment
- **Language**: all output must be in Chinese (中文)

### Permission Mode

`CodeReviewAgent._get_options()` uses `permission_mode="bypassPermissions"` so tool calls run unattended. This requires running as a non-root user.

### Prompt Configuration

`src/prompts/yunxiao_mr.yaml` defines `description`, `prompt` (Chinese), and `tools`. To adjust review behaviour edit the YAML — no code changes needed.
