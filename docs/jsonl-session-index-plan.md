# Plan — JSONL-Derived Session Index (session-meta becomes enrichment, not source of truth)

> Status: **IMPLEMENTED — JSONL-primary.** Built `collectors/session_index.py` +
> `merge_jsonl_primary`, wired into `batch_runner._collect` and `push.py`; `session_metas` push is
> force-aware (REPLACE on `--force`) so cutover refreshes existing rows.
>
> **JSONL is the source of truth** (usage-data has clear coverage gaps). Every real session uses its
> JSONL-derived record (one consistent methodology); telemetry only fills orphan sessions (usage-data
> exists but JSONL gone) + fields JSONL can't derive (`user_interruptions`). This is a deliberate
> one-time cutover vs the old telemetry numbers (R2 accepted, not avoided).
>
> Verified on sample data: 38 JSONL-primary + 4 telemetry orphans = 42; all 14 overlapping sessions
> now JSONL-sourced; 6 enriched with telemetry interruptions; subagents excluded (38 real, not 106);
> central.db force-repushed to JSONL-primary. Remaining: Postgres parity; R5 sub-agent line undercount
> documented. `merge_gap_fill` kept (deprecated) for reference.

## Context

**The problem.** Almost every per-session metric (adoption, depth, trust, velocity, harness
denominator, outcomes) keys off `session_meta`, collected from `~/.claude*/usage-data/session-meta/
*.json`. That directory is **usage telemetry — optional and frequently absent**. Measured on the
current machine:

| Account | JSONL transcripts | session-meta | usage-data dir |
|---|---|---|---|
| `.claude` | 9 | **0** | **missing** |
| `.claude-personal` | 24 | 8 | yes |
| `.claude-work` | 72 | 10 | yes |
| **Total** | **105** | **18 (17%)** | — |

Here, 18 of ~40 **real** sessions have a session-meta file (~55% miss); on another laptop with
telemetry off / different CC version / fresh install it can be **0%** → that developer scores
AI-absent despite heavy real use. Only `agent_hours` is robust today, because it reads `busy_segments`
built from **JSONL** (`projects/**/*.jsonl`), which Claude Code **always** writes.

**Coverage math (corrected).** The 105/106 `*.jsonl` files under `projects/` are NOT all sessions:
**66 are sub-agent transcripts under `subagents/`**, leaving **~40 real sessions**. So the realistic
coverage gain is **18 → ~40 (≈2.2×)**, not 5.8×. Excluding `subagents/` is mandatory — see Risks.

**Key finding — JSONL is a complete superset of session-meta.** Real-transcript inspection shows
every session-meta field is reconstructable from JSONL:

| session-meta field | JSONL source |
|---|---|
| start_time / week / date / duration_minutes | message `timestamp`, `durationMs` |
| project_path | `cwd` (or decoded project dir) |
| user/assistant_message_count | count `type=='user'` / `'assistant'` |
| tool_counts, uses_task_agent/mcp/web_search/web_fetch | `tool_use` names in assistant content |
| **lines_added / lines_removed** | `toolUseResult.structuredPatch` +/- line arrays (**968 results have it**) |
| **files_modified** | distinct `toolUseResult.filePath` |
| git_commits / git_pushes | Bash `tool_use` inputs matching `git commit` / `git push` |
| input_tokens / output_tokens | assistant `message.usage` |
| first_prompt | first user text |
| user_response_times | gaps between assistant-end and next user prompt |
| tool_errors | `toolUseResult.status`/`is_error` |
| languages | file extensions of `filePath`s touched |
| account_type | from `claude_dir` |

So usage-data/session-meta is **pure redundant cache** — nothing only it can provide.

**Intended outcome.** Build the session universe from JSONL (always present, `subagents/` excluded).
Treat `usage-data/session-meta` as optional enrichment/fallback. Coverage goes 18 → ~40 real
sessions; metrics degrade gracefully instead of collapsing when usage-data is missing.

**This is a source-switch, not just a gap-fill.** Even the 18 already-covered sessions get their
`lines_added`/`tool_counts`/`tokens` recomputed from JSONL, which will differ from telemetry values.
Every metric's numbers move and history steps at cutover — must be a documented cutover, not a silent
swap (see Risks).

## Decisions to confirm before building

1. **JSONL-primary, meta-fallback** (recommended): JSONL-derived value wins; usage-data session-meta
   fills a field only when JSONL can't produce it. (Alternative: meta-primary — rejected, defeats the
   purpose.)
2. Keep the **session_meta dict shape byte-identical** so NO downstream computer changes — the new
   collector is a drop-in replacement for `session_meta.collect` in `batch_runner._collect`.
3. git_commits/pushes derived heuristically from Bash command text (documented as heuristic).

## Design

### New collector: `collectors/session_index.py`
Contract `collect(developer_map, since=None) -> list[dict]` — same return type/shape as
`collectors/session_meta.collect` (`collectors/session_meta.py:78`). One pass per **top-level**
`projects/<encoded>/*.jsonl` across all `claude_dirs`, aggregating a full session_meta-shaped record:

- **Exclude `subagents/` (mandatory).** 66 of 106 `*.jsonl` are sub-agent transcripts; including them
  fabricates ~66 fake sessions and triples lines/tokens/tool_counts. Glob only the project root's
  direct `*.jsonl`, never `**/subagents/*.jsonl` — mirror how `collect_segments` already separates
  them (`collectors/sessions.py:265` main vs `:272` subagents).
- **Group by internal `sessionId`, not file stem.** Resumes/forks create new files for one logical
  session; key aggregation on the `sessionId` field so one session = one record.

- **Reuse the existing single-pass JSONL reader.** `collectors/sessions.py` already parses these
  files (`_process_jsonl`, `collect_segments._segments_from_jsonl`) and `_classify` (sessions.py:158)
  already distinguishes human/agent/tool_result. Extend that pass to also accumulate per-session
  aggregates rather than writing a second parser.
- **structuredPatch math:** for each `toolUseResult` with `structuredPatch`, sum lines starting `'+'`
  as added and `'-'` as removed (skip context lines); collect `filePath` for files_modified +
  languages (by extension).
- **tokens:** sum `message.usage.input_tokens` / `output_tokens` across assistant messages.
- **git:** count Bash `tool_use` inputs whose command matches `\bgit\s+commit\b` / `\bgit\s+push\b`.
- Emit identical keys to `session_meta._parse_one` (session_meta.py:45-75): `session_id,
  developer_key, claude_dir, account_type, project_path, start_time, week, date, duration_minutes,
  user_message_count, assistant_message_count, tool_counts, languages, lines_added, lines_removed,
  files_modified, git_commits, git_pushes, first_prompt, user_interruptions, user_response_times,
  tool_errors, uses_task_agent, uses_mcp, uses_web_search, uses_web_fetch, input_tokens, output_tokens`.

### Enrichment overlay (optional, lossless)
After building JSONL records, load `usage-data/session-meta/*.json` (reuse `session_meta.collect`)
and, per session_id, fill only fields the JSONL record left empty/None. Telemetry, where present,
becomes a tie-breaker — never the gate.

### Wiring (minimal)
- `batch_runner.py:_collect` — swap `session_meta.collect(...)` for `session_index.collect(...)`
  (then enrichment overlay). Everything downstream (`sessions_by_dev`, `meta_by_sid`) is unchanged.
- `push.py` — same swap so the central store's `session_metas` table is populated from JSONL
  (coverage 18 → ~105). **No schema change** — identical row shape.
- `central_store.py` — no change (same `session_metas` shape).
- No computer changes (depth, trust, velocity, harness, adoption, outcomes all read the same shape).

## Critical files
- `collectors/session_index.py` (new), reusing `collectors/sessions.py` parse helpers.
- `collectors/session_meta.py` (kept, now an enrichment source).
- `batch_runner.py:_collect` (one swap), `push.py` (one swap).

## Reuse (don't reinvent)
- JSONL iteration + `_classify` + `_parse_iso` + week label — `collectors/sessions.py`.
- Existing field shape + account_type/project decode — `collectors/session_meta.py:12,27,45`.
- Project path decode bug + `_decode_project_root` fix is already described in
  `docs/metrics-redesign-plan.md` §1 — reuse it (don't re-fix).

## Risks (ranked — verified against real data)

🔴 **R1 — Sub-agent files as fake sessions (HIGH, must-fix).** 66 of 106 `*.jsonl` live under
`subagents/`. A naive `projects/**/*.jsonl` glob inflates the session count to 106 and triples
lines/tokens/tool_counts. **Mitigation:** glob project-root `*.jsonl` only, exclude `subagents/`
(see Design). This also corrects the coverage claim to ~40, not 105.

🔴 **R2 — Source-switch moves existing numbers, steps history (HIGH).** Switching `lines_added`/
`tool_counts`/`tokens` from telemetry to JSONL recomputes the 18 already-covered sessions too — their
velocity/depth values change. Every trend line steps at cutover. **Mitigation:** treat as a versioned
cutover — stamp the cutover week in `history.jsonl`, expect a one-time discontinuity, don't compare
across it. Same caveat the weight-change redesign already carries.

🔴 **R3 — Lines inflation amplified (HIGH).** `structuredPatch` counts every edit, re-edit churn, and
generated-file Write; telemetry may net/dedup. Velocity (the 820 problem) gets worse. **Mitigation:**
the per-session lines rate-cap in `computers/velocity.py` becomes load-bearing, not optional; re-tune
its ceiling against JSONL-derived numbers.

🟡 **R4 — session_id identity / orphans (MED).** 15/18 telemetry ids match a JSONL stem; 3 telemetry
sessions have no JSONL. Key strictly by `session_id`. On store cutover, `session_metas` uses
INSERT-OR-IGNORE → stale telemetry rows persist → mixed-source store. **Mitigation:** push with
`--force` (or REPLACE) at cutover; keep the 3 telemetry-only orphans as enrichment-only records.

🟡 **R5 — Sub-agent code under-attribution (MED).** Lines edited *by* sub-agents live in the excluded
`subagents/` files. Telemetry rolled them into the parent; JSONL-derived (subagents excluded) will
under-count a delegating session's output — inconsistent with how `agent_hours` credits sidechains.
**Mitigation:** decide explicitly — either fold sub-agent `structuredPatch` back into the parent
session (match agent_hours), or document the parent-only undercount. Recommend folding for consistency.

🟡 **R6 — Field stability across CC versions (MED).** `structuredPatch`/`usage`/`durationMs` are
internal, undocumented fields; schema drift across Claude Code versions → silent zeros. This *moves*
the fragility rather than removing it. **Mitigation:** defensive parsing + a validate.py check that
flags sessions where derived lines/tokens are all-zero but tool_use exists.

🟡 **R7 — Approximated fields (MED).** `user_response_times`, `user_interruptions`, `duration_minutes`
are timestamp-reconstructed, not telemetry-exact. Document; spot-check against overlapping telemetry.

🟢 **R8 — Perf at team scale (LOW-MED).** Full-content parse (tool outputs, attachments) over thousands
of large transcripts is heavier than agent_hours' timestamp-only pass. **Mitigation:** fold into the
existing single JSONL pass; honor `since` mtime filtering.

🟢 **R9 — git heuristic (LOW).** Bash-text matching counts `git commit`/`push` attempts, not confirmed
results.

## Verification
1. `session_index.collect(dm)` → assert session count ≈ real-session count (**~40**, NOT 106 — proves
   `subagents/` exclusion works), not 18; every account represented incl `.claude` (0 usage-data).
2. Field parity spot-check: pick a session that HAS a usage-data session-meta; assert JSONL-derived
   `lines_added`/`tool_counts`/`tokens` are in a *documented* tolerance of telemetry (they will NOT
   match exactly — R2/R3 — the test asserts the magnitude of drift is understood, not zero).
3. `python3 push.py --central /tmp/x.db --since 3650d --force` then
   `python3 batch_runner.py --from-store /tmp/x.db --all-weeks --report` → session_metas rows ≈ 40;
   harness/depth/velocity now score the previously-invisible weeks (W23/W25/W26 etc).
4. Robustness: temporarily hide a `usage-data` dir → metrics still populate from JSONL (no collapse).
