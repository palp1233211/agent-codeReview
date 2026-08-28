# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A CLI-only agent that reviews Yunxiao (云效) Merge Requests via either the Claude Agent SDK or the OpenAI SDK and posts a single Chinese-language Markdown comment back to the MR. Yunxiao review now uses the same local tool chain in both runtimes. Also includes a standalone Feishu bot (long-connection/WebSocket mode) under `src/lark/`.

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
- `AGENT_PROVIDER`: `claude` by default; set to `openai` to use the OpenAI SDK runtime.
- `ANTHROPIC_API_KEY`: Required in Claude mode.
- `ANTHROPIC_BASE_URL`: Optional Claude-compatible endpoint.
- `OPENAI_API_KEY`: Required in OpenAI mode.
- `OPENAI_BASE_URL`: Optional OpenAI-compatible endpoint.
- `OPENAI_MODEL`: Model name for OpenAI mode, defaults to `gpt-5.4`.
- `OPENAI_API_MODE`: `responses` by default; use `chat_completions` for providers that only implement Chat Completions.
- `YUNXIAO_ACCESS_TOKEN`: Yunxiao platform access token
- `YUNXIAO_ORG_ID`: Default organization ID for Yunxiao MR operations
- `YUNXIAO_MCP_URL`: HTTP or SSE MCP endpoint used directly by the OpenAI runtime.
- `YUNXIAO_TOOLSETS`: Yunxiao MCP toolsets header, defaults to `code-management`.
- `LARK_APP_ID` / `LARK_APP_SECRET`: Feishu app credentials, used by `LarkClient` (bi-weekly-doc task) and the `lark-bot` WebSocket bot
- `DIFY_BASE_URL` / `DIFY_API_KEY`: local Dify instance + app API key, used by `DifyClient` to forward `lark-bot` messages to a Dify Chatflow
- `DB_HOST` / `DB_PORT` / `DB_USERNAME` / `DB_PASSWORD` / `DB_DATABASE` / `DB_CHARSET`: MySQL connection used by `ConversationStore` to log every `lark-bot` question/answer into `lark_bot_conversations`, and by `KnowledgeGapStore` for `lark_bot_knowledge_gaps`
- `DIFY_DATASET_API_KEY` / `DIFY_DATASET_ID`: Dify **knowledge base (dataset) API** credentials. This is a *different* key from `DIFY_API_KEY` — dataset keys are created under 知识库 → Service API and carry a `dataset-`/`ds-` prefix, while `DIFY_API_KEY` is the `app-`-prefixed application key used for `/chat-messages`. Using the app key against dataset endpoints silently 401s.
- `KNOWLEDGE_DOCS_DIR` / `FBI_REPO_PATH`: local knowledge markdown root and the code root the gap-filling agent reads. Both are **mounted from the host** in Docker deployments — never hardcode either path. `FBI_REPO_PATH` may point to a parent directory containing several independent project checkouts as subdirectories (e.g. `fbi/`, `bi-common/`, `ard-api/`) rather than a single repo — the agent is prompted to discover subdirectories itself and defaults to `fbi/` when no better signal is given.
- `KB_CHUNK_MAX_TOKENS` / `KB_MAX_CONCURRENT_FILLS`: Dify chunk size limit, and the cap on concurrent agent fills spawned by `/kb fill`.

## Architecture

### Core Components

- **`src/agents/reviewer.py`**: `CodeReviewAgent` — entry point for `review_yunxiao_mr()`, `review_files()`, and `review_git_diff()`. It builds prompts from YAML and selects the runtime via `AGENT_PROVIDER`.
- **`src/agents/runtime.py`**: provider-neutral runtime layer. `ClaudeAgentRuntime` preserves Claude Agent SDK behavior, including the native `Agent` tool, hooks, and stdio MCP servers. `OpenAIAgentRuntime` uses OpenAI Responses or Chat Completions and bridges HTTP MCP tools into ordinary function tools so compatible providers do not need native `type=mcp` support.
- **`src/agents/mcp_client.py`**: HTTP/SSE MCP client used by the OpenAI runtime for `initialize`, `tools/list`, and `tools/call`.
- **`src/prompts/yunxiao_mr.yaml`**: prompt + allowed tool list for the MR reviewer.
- **`src/prompts/__init__.py`**: thin YAML loader exporting `YUNXIAO_MR_AGENT`.
- **`src/hooks/validation.py`**: PreToolUse / PostToolUse / UserPromptSubmit hooks (path validation, audit log, prompt enrichment).
- **`src/cli/main.py`**: CLI entry — subcommands `yunxiao-mr`, `files`, `diff`, `bi-weekly-doc`, `lark-bot`.
- **`src/lark/client.py`**: `LarkClient` — thin wrapper over the `lark-oapi` SDK (docx copy/update, send text message), built from `LARK_APP_ID`/`LARK_APP_SECRET` via `LarkClient.from_env()`.
- **`src/lark/ws_bot.py`**: `run_ws_bot()` — Feishu long-connection (WebSocket) client. Subscribes to `im.message.receive_v1`; text messages are forwarded to `DifyClient.chat()` and the `answer` is sent back via `LarkClient.send_text_message`. No public callback URL needed; requires the Feishu app's event subscription to be set to "长连接接收事件" with `im.message.receive_v1` added.
  - **Important**: `lark_oapi.ws.Client` dispatches every event on a single asyncio loop and calls the Python handler synchronously (see `_handle_data_frame` in the SDK) — a slow handler delays the WS ACK frame, which makes Feishu's server think delivery failed and **retry-push the same event**, causing duplicate replies. `on_message_receive` therefore only does dedup (`_RecentMessageIds`, keyed by `message_id`) and then hands the actual Dify call + reply off to a background thread so the callback returns immediately. Keep this pattern for any future slow work triggered from an event handler.
- **`src/dify/client.py`**: `DifyClient` — thin wrapper over Dify's `/chat-messages` API (advanced-chat / Chatflow apps only, not `/workflows/run`). Keeps an in-process `{user_id: conversation_id}` map so each Feishu sender's `open_id` gets its own multi-turn conversation; state is lost on process restart. `chat()` returns a `ChatReply(answer, conversation_id)`, with `<think>...</think>` blocks already stripped from `answer` via `strip_think_tags()`.
- **`src/storage/conversation_store.py`**: `ConversationStore` — logs every `lark-bot` Q&A into MySQL table `lark_bot_conversations` (auto-created via `CREATE TABLE IF NOT EXISTS` on `from_env()`). Opens a fresh connection per `log()` call (simple, thread-safe by construction — no pooling needed at this scale); `message_id` has a `UNIQUE KEY` so retried/duplicate events are silently ignored via `INSERT IGNORE`, on top of the in-memory `_RecentMessageIds` dedup in `ws_bot.py`.
- **`src/dify/intent.py`**: `parse_gap_intent()` — the Dify Chatflow returns a structured *intent envelope* (JSON, e.g. `{"intent":"unknown",...,"original_query":"..."}`) instead of prose when it can't answer from the knowledge base. Parsing is deliberately conservative: only a successful `json.loads` yielding a dict with a **string** `intent` key counts as an envelope; everything else returns `None` and is passed through verbatim. Handles ```` ```json ```` fences.
- **`src/storage/knowledge_gap_store.py`**: `KnowledgeGapStore` — the knowledge-gap queue, MySQL table `lark_bot_knowledge_gaps`, same conventions as `ConversationStore`. State machine: `pending → filling → drafted → approved → syncing → synced`, with `needs_human` / `failed` as terminal branches. `try_claim()` is a **CAS update** (`WHERE id=%s AND status=%s`, checking `rowcount`) so concurrent `/kb fill` on the same row can't double-process — and unlike an in-memory lock this holds across container restarts and replicas.
- **`src/dify/dataset_client.py`**: `DifyDatasetClient` — Dify **knowledge base** API (`/datasets/*`), distinct from `client.py`'s chat API. Note Dify's real singular/plural asymmetry, which is not a typo: create is `POST /datasets/{id}/document/create-by-text` (**singular**), update is `POST /datasets/{id}/documents/{doc}/update-by-text` (**plural**). Underscore variants (`create_by_text`) are deprecated server-side. Always **update, never delete-and-recreate** — recreating changes the `document_id` and breaks retrieval history. `create`/`update` returning HTTP 200 only means *queued*; indexing is async, so `indexing_status(batch)` must be polled before reporting success.
- **`src/storage/kb_document_store.py`**: `KbDocumentStore` — `doc_path ↔ dify_document_id` mapping in table `kb_documents`, unique on `(dify_dataset_id, doc_path)` so dev and prod datasets can't cross-contaminate. Deliberately **not** stored in `index.yaml`: document ids are environment-scoped mutable state and would cause merge conflicts in git.
- **`src/knowledge/`**: domain package (sibling of `src/agents/`, not an external-system adapter).
  - `gap_recorder.render_answer()` — the only entry point `ws_bot.py` knows about for the answer path. **Security invariant**: once an answer is identified as an envelope the raw JSON is **never** sent to the user, whatever the `intent` value; and a DB failure while enqueuing must not block the reply (it degrades to a generic message).
  - `commands.py` — `/kb list|fill|approve|sync|delete` Feishu commands, intercepted **before** forwarding to Dify. Slow actions (`fill`/`approve`/`sync`) run through an injected `executor` (daemon thread in production, synchronous in tests); `delete` is a plain synchronous DB delete — it's not slow enough to need one. Concurrency is guarded twice: a `BoundedSemaphore` caps concurrent agent fills, and `gap_store.try_claim()` CAS prevents double-processing across restarts/replicas. `delete` refuses rows in `filling`/`syncing` (a background thread is actively working the row and would hit `GapRowMissing` on completion) but otherwise removes the row unconditionally — it only deletes the local queue record, never anything already pushed to Dify. `/kb fill <id>` also accepts free trailing text (`parse_command`'s `KbCommand.hint`, split with `maxsplit=3` so the hint keeps internal whitespace) — since `FBI_REPO_PATH` may hold several project checkouts as sibling subdirectories, a human can name the project/method here to steer the agent instead of it guessing.
  - `docs_repo.py` — `DocsRepo`: `index.yaml` routing, per-`doc_path` write locks, `<!-- src: -->` anchor lookup, and path-traversal rejection (agent-produced `doc_path` is untrusted).
  - `gap_agent.py` — `GapFiller`. **The agent never writes files itself**; it returns structured JSON that Python validates (source anchors present, block under the size cap) before writing. Keeping the write authority in Python is what makes the chunking rules enforceable rather than merely suggested. If the agent can't determine the answer, the row goes to `needs_human` and **nothing** is written. `fill()`'s optional `hint` kwarg (from `/kb fill <id> <hint>`) is folded verbatim into the agent prompt by `_build_prompt`, not passed as a separate arg to `_run_agent` — the agent still only ever sees one prompt string.
  - `sync.py` — `GapSyncer`: idempotent via `content_sha256` (unchanged content skips the push), polls indexing to completion, and reports failure honestly instead of claiming success.
  - `factory.py` — `build_kb_commands()` wires everything from env. If the KB env vars are absent, `run_ws_bot` logs a warning and runs **without** `/kb` rather than refusing to boot — the core Q&A bot must not depend on the KB pipeline being configured.

### Knowledge Gap Pipeline Flow

1. Dify Chatflow can't answer → returns an intent envelope → `render_answer` enqueues it and replies in plain Chinese with the gap id.
2. User sends `/kb fill <id>` (optionally `/kb fill <id> <项目/方法线索>` when `FBI_REPO_PATH` has multiple project subdirectories and the agent needs a pointer) in the Feishu group → CAS claim → background thread runs the `kb_fill` agent against `FBI_REPO_PATH` → validated blocks appended to `knowledge/docs/menus/{topic}.md` → status `drafted`.
3. User reviews, sends `/kb approve <id>` → pushes to Dify, polls indexing → status `synced`.
4. `/kb sync <id>` is the idempotent retry path for a failed push.

Writing to local markdown needs no confirmation (git can roll it back); **pushing to the live Dify knowledge base always does**.

Run `python cli.py kb-doctor` before deploying — it verifies mounted paths, credential *types*, OpenAI runtime configuration, Dify settings, and database connectivity.

### Dify Gotchas (all verified empirically against a live instance — do not "simplify" these)

- **Chunk separator must be `"\n---\n"`, not `"---"`.** Dify does plain string matching, so a bare `---` also matches the `|---|---|` row inside every markdown table and splits tables apart. Measured: a 2-block doc containing 2 tables became **7 chunks**, 3 of which contained only `|`.
- **`doc_language` must be sent explicitly** (`"Chinese"`). Dify interpolates it into the summary prompt's `{language}` placeholder; the default is `English`, which yields English summaries for Chinese documents — and a Chinese query matches an English summary poorly.
- **Summaries survive nothing.** `update-by-text` recreates all segments with new ids, so per-segment `summary` values are wiped. If the dataset's Summary Index is enabled they are regenerated asynchronously *after* `indexing-status` already reports `completed`; if it is disabled they are simply gone. `summary` is writable only via the segment-level API (`SegmentUpdateArgs`), never via `update-by-text`.
- **Never push a block that has only a heading.** With no facts to work from, the summary model fabricates. Observed on a 20-char title-only chunk: it invented "cat=21 对应严重违规，cat=22 对应一般违规" — cat=21 is actually 客户投诉 and cat=22 does not exist. `render_for_dify` drops such blocks (`_has_body`).
- Provider behavior differs: Claude mode keeps the original Claude Agent SDK stdio MCP mechanism. OpenAI mode connects HTTP MCP itself and presents ordinary function tools to the configured provider; use `OPENAI_API_MODE=chat_completions` only when the provider does not implement Responses API function calling.

### Yunxiao MR Review Flow

The agent uses the same provider-neutral local tool chain in both runtimes. Tool sequence:

1. `get_yunxiao_mr` → MR details
2. `get_yunxiao_mr_diff` → branch diff
3. `get_yunxiao_mr_files` / `get_yunxiao_file_content` → full file content for changed files
4. `comment_on_yunxiao_mr` → publish review comment (GLOBAL_COMMENT, single call)

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
