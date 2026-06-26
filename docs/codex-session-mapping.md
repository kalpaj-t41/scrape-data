# Codex session → `scrape_data` DB mapping

How a Codex (OpenAI / GPT-5.x) rollout `.jsonl` is normalized into the same
records the Claude collectors emit, and exactly which `scrape_data` column each
Codex field lands in. Implemented by [`collectors/codex_sessions.py`](../collectors/codex_sessions.py).

The guiding rule: **emit the identical record shapes the Claude collectors
produce** so `central_store.push()` and every computer work unchanged. Codex
fields with no Claude/DB equivalent are dropped or left at their pipeline
default — we never fabricate (see the no-synthetic-data project rule).

---

## 1. Source & identity

| Aspect | Claude Code | Codex |
|---|---|---|
| Files | `~/.claude*/projects/**/*.jsonl` | `~/.codex*/sessions/**/*.jsonl` |
| Session id | filename stem / `sessionId` | `session_meta.payload.id` |
| Project path | encoded dir name | `session_meta.payload.cwd` |
| `developer_key` | `sha256(git email)` | **same** — `sha256(git email)` resolved from global git config; a dev's Codex + Claude work merge under one key |
| `source` column | `null` / `telemetry` / jsonl | `"codex"` (set on every row) |

`source="codex"` is the sole **provider** discriminator. `account_type` is the
orthogonal **account** axis (`work`/`personal`/`primary`/`other`), derived from
the `.codex*` dir name exactly like the Claude side — NOT overloaded with the
provider. The existing `claude_dir` column holds the `.codex` directory path
(column reused, not renamed).

## 2. Record-type → pipeline stage

| Codex line `type` (→ `payload.type`) | Consumed for |
|---|---|
| `session_meta` | session id, start time, project path |
| `turn_context` | `approval_policy`, `collaboration_mode` → `permission_mode` |
| `event_msg` → `user_message` | turn boundary (human), `first_prompt`, response times |
| `event_msg` → `agent_message` | first-reply timestamp, `final_answer` for response-time |
| `event_msg` → `token_count` | `input_tokens` / `output_tokens` (cumulative max) |
| `response_item` → `message` (assistant) | `assistant_message_count` |
| `response_item` → `function_call` | `tool_counts`, `tool_uses`, lines/git extraction |
| `response_item` → `function_call_output` | `tool_errors` (non-zero exit code) |
| `response_item` → `reasoning` | busy-segment activity only (content is encrypted) |

---

## 3. `scrape_data.session_metas`

| Column | Codex source | Notes |
|---|---|---|
| `session_id` | `session_meta.payload.id` | |
| `developer_key` | `sha256(git email)` | merges with Claude identity |
| `claude_dir` | `.codex` dir path | column reused |
| `account_type` | derived from `.codex*` dir name | `work`/`personal`/`primary`/`other` — account axis, not provider |
| `source` | `"codex"` | existing column; the provider discriminator |
| `project_path` | `session_meta.payload.cwd` | |
| `start_time` | `session_meta.payload.timestamp` | falls back to first record ts |
| `week` / `date` | derived from `start_time` | ISO week |
| `duration_minutes` | last ts − first ts | wall clock of the rollout |
| `user_message_count` | count of `event_msg/user_message` | |
| `assistant_message_count` | count of `response_item/message` role=assistant | |
| `tool_counts` (JSONB) | `Counter(function_call.name)` | e.g. `{"exec_command":2,"apply_patch":1}` |
| `input_tokens` / `output_tokens` | `token_count.info.total_token_usage.*` | cumulative max |
| `lines_added` / `lines_removed` | parsed from `apply_patch` arguments | best-effort, real (+/- body lines) |
| `files_modified` | `*** {Add,Update,Delete} File:` markers | best-effort |
| `git_commits` / `git_pushes` | `exec_command` cmd matches `git commit` / `git push` | counts invocations, not successes |
| `first_prompt` | first `user_message.message` | |
| `tool_errors` | `function_call_output` with non-zero `exited with code N` | |
| `user_response_times` (JSONB) | gaps: `final_answer` ts → next `user_message` ts | real human latency, seconds |
| `uses_mcp` | any `function_call.name` starts with `mcp` | heuristic |
| `uses_web_search` | name contains `web_search`/`browser_search` | heuristic |
| `uses_web_fetch` | name contains `web_fetch`/`fetch` | heuristic |
| `uses_task_agent` | name in `{spawn_agent,task,agent}` | **~always False** — Codex has no sub-agent primitive |
| `languages` (JSONB) | — | `{}` — not recorded by Codex |
| `user_interruptions` | — | `0` — no Codex equivalent |

## 4. `scrape_data.turn_events` (`event_type = 'turn'`)

A turn = `user_message` → first agent reply; tool calls in between are its `tool_uses`.

| Column | Codex source | Notes |
|---|---|---|
| `session_id` / `developer_key` | session-level | |
| `event_type` | `"turn"` | (no `"skill"` rows — see gaps) |
| `user_ts` | `user_message` timestamp | |
| `assistant_ts` | first `agent_message`/assistant `message` after it | |
| `agent_ms` | `assistant_ts − user_ts` (ms) | time-to-first-token; bounded 0–10min like Claude |
| `permission_mode` | mapped from `turn_context` | see §6 |
| `tool_uses` (JSONB) | `function_call.name`s within the turn | |
| `prompt_text` | `user_message.message` | |
| `is_sidechain` | `false` | Codex has no sidechains |
| `agent_colors_in_session` | `0` | no parallel-agent streams |

## 5. `scrape_data.busy_segments`

Built exactly like `sessions._segments_from_jsonl`: human prompt → last agent
activity, split on any >10-min stall.

| Column | Codex source |
|---|---|
| `session_id` / `developer_key` | session-level |
| `start_ts` | the `user_message` timestamp opening the segment |
| `end_ts` | last agent activity (tool call/output, message, reasoning, token_count) |
| `is_sidechain` | `false` |
| `week` | derived from `start_ts` by `central_store` |

**human** = `user_message`; **agent** = every other timestamped item (messages,
reasoning, function_call/output, agent_message, token_count); meta lines
(`session_meta`, `turn_context`, `task_started`) are ignored.

---

## 6. `permission_mode` mapping

Codex has no single `permissionMode`; it is derived per turn from
`collaboration_mode` + `approval_policy` so the trust computer (M8) reads it unchanged:

| Codex | → Claude `permission_mode` | Rationale |
|---|---|---|
| `collaboration_mode.mode == "plan"` | `plan` | plan mode |
| `approval_policy == "never"` | `bypassPermissions` | auto-runs everything (≈ blind accept) |
| `approval_policy == "on-failure"` | `acceptEdits` | runs, asks only on error |
| `approval_policy == "on-request"` | `default` | asks before acting |

---

## 7. Tables with NO Codex source

| Table | Why empty | Possible future source |
|---|---|---|
| `facets` | Claude writes AI-analyzed `facets/*.json`; Codex has none | run the same LLM facet pass over Codex transcripts |
| `agent_tasks` / `background_tasks` | Codex has no Task/Agent or background-task primitive in the rollout | n/a today |
| `plans` | Claude writes `plans/*.md`; Codex `update_plan` lives only inline in the rollout | parse `update_plan` calls into a plans equivalent |
| `app_state` | from Claude's `.claude.json`; no Codex analog | `~/.codex/config` (startups not tracked) |

