# scrape_data — Claude Code Context

## What this project is

A data collection and metrics computation layer that reads raw `.claude*` directory files
produced by Claude Code across a developer's machine, computes AI-nativeness metrics,
and outputs structured data for dashboarding.

No Claude hooks. No plugin. Pure offline batch processing of files Claude Code already writes.

**Status: built and running.** Collectors, the class-based computer registry, both store
backends (SQLite + PostgreSQL), push, and batch runner are implemented.

## Goal

Answer the question: **How AI native is this team?**
Metrics are designed for three audiences: CXO, CTO, Team Lead.

## Key design decisions

- **JSONL is the source of truth**: `projects/**/*.jsonl` transcripts are always written by
  Claude Code, so per-session metrics are reconstructed from them (`collectors/session_index.py`).
  `usage-data/session-meta/*.json` is optional telemetry (often absent — one account here has none)
  and is used only as enrichment / for orphan sessions. See `docs/jsonl-session-index-plan.md`.
- **Multi-account aware**: scans all `~/.claude*` directories, not just `~/.claude/`.
- **No instrumentation required**: reads files Claude Code writes natively.
- **Incremental**: tracks last-processed timestamp / pushed session-ids to avoid re-parsing.
- **Identity merging**: same developer may have work + personal accounts — merged by git email hash.
- **Agent hours is the north star metric**: target 80 hrs/week per developer; <20 is the stuck
  threshold. Computed from JSONL busy-segments (wall-clock vs labor vs parallelism), not telemetry.
- **Two run modes**: single-machine (read local files) and distributed (push to a central store,
  compute with `--from-store`).

## Directory layout

```
scrape_data/
  docs/
    metrics.md                          ← metric definitions, formulas, dashboards
    metrics-redesign-plan.md            ← PARENT plan (two-layer harness + config + registry)
    orchestration-usage-accuracy-plan.md← child plan (M7 accuracy) — IMPLEMENTED
    jsonl-session-index-plan.md         ← plan (JSONL-primary sessions) — IMPLEMENTED
  collectors/         ← one file per data source
    discover.py         build developer_map (identity merge)
    sessions.py         JSONL parse: turn events + busy segments (agent hours)
    session_index.py    JSONL → session_meta-shaped records (source of truth)
    session_meta.py     usage-data/session-meta telemetry (enrichment / fallback)
    agent_tasks.py      queue-operation tasks + background_tasks (harness M7)
    facets.py app_state.py plans.py plugins.py settings.py
  computers/          ← one MetricComputer subclass per metric, on a singleton registry
    base.py             ComputeContext + MetricComputer ABC
    registry.py         MetricRegistry: topo-orders deps, runs metric/score phases
    agent_hours.py adoption.py parallel_agents.py depth.py harness.py skills.py
    trust.py outcomes.py velocity.py consistency.py composite.py equity.py
  batch_runner.py     ← orchestrates collect → compute → score; local or --from-store
  push.py             ← per-machine: collect local data → central store (no compute)
  central_store.py    ← SQLite + PostgreSQL backends (push / pull_raw / stats)
  metrics_store.py    ← writes output JSON + weekly history/state
  validate.py         ← sanity checks
```

## Compute layer (registry)

Every metric is a `MetricComputer` subclass registered with the singleton `registry`
(`@registry.register`). It declares `name`, `deps`, and `phase`:
- `phase="metric"` — runs once per batch, produces a dev-keyed `{by_week: …}` dict.
- `phase="score"` — runs once per ISO week (`composite`, `equity`).
The registry topologically sorts by `deps` (e.g. `velocity` after `agent_hours`,
`equity` after `composite`). Adding a metric = a new registered class + one import line in
`computers/__init__.py`; no `batch_runner` changes.

## Data sources (read-only)

| File/Dir | Role |
|---|---|
| `~/.claude*/projects/**/*.jsonl` | **Source of truth** — transcripts; timestamps, tool_use, structuredPatch (lines), usage (tokens), permissionMode, sidechain, queue-operation. Sub-agent transcripts live under `subagents/` and are NOT sessions. |
| `~/.claude*/usage-data/session-meta/*.json` | Optional telemetry — enrichment / orphan fallback only |
| `~/.claude*/usage-data/facets/*.json` | AI-analyzed outcomes — goal, achievement, friction (M9) |
| `~/.claude*/.claude.json` | App state — numStartups, hasUsedBackgroundTask, flags |
| `~/.claude*/plans/*.md` | Saved plan files — plan-mode fallback signal |
| `~/.claude*/plugins/installed_plugins.json` | Installed plugins per account |
| `~/.claude*/settings.json` | Hooks and permission configuration |

## Running

```bash
# Single machine: read local ~/.claude* files, score the most recent week
python batch_runner.py --since 7d --report

# Distributed: each machine pushes to a shared store, then compute from it
python push.py --central <sqlite-path|postgres-url> --since 90d        # add --force to refresh
python batch_runner.py --from-store <sqlite-path|postgres-url> --all-weeks --since 90d --report
```

`--force` on push is a **scoped** refresh: it deletes + reinserts only the session_ids in the
current payload (this machine's sessions within `--since`); other developers' sessions are untouched.

## Key terms

- **Agent hours**: time Claude was actively processing (not waiting for user). Wall-clock (union of
  overlapping busy segments) vs labor (sum across parallel sub-agents); parallelism = labor/wall.
- **Parallel agents**: concurrent sidechain sessions (agent-color / isSidechain / overlapping windows).
- **Harness → Orchestration Usage (M7)**: real use of Plan mode (`permissionMode='plan'`), sub-agent
  delegation, and background tasks (`queue-operation` enqueues). Scored from raw JSONL signals, not
  proxies. The old plan-file-count / Workflow / lifetime-flag proxies were dropped.
- **Skill**: slash commands invoked during sessions (`/deep-research`, `/code-review`, `/run`, etc.).
- **Session depth**: composite of tool calls, code volume, and duration.
- **Source of truth**: JSONL-derived `session_index` records; `source` field = `jsonl` | `telemetry`.
