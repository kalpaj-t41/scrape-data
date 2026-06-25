# scrape_data

Offline batch pipeline that reads raw `.claude*` directory files produced by Claude Code, computes AI-nativeness metrics, and writes structured output for dashboarding.

**Core question answered:** *How AI native is this team?*

---

## Architecture

```
Per-machine (push.py)               Central server (batch_runner.py)
──────────────────────              ────────────────────────────────
collectors/                         central_store.pull_raw()
  sessions.py       ─┐                       │
  session_index.py   │ (JSONL = truth)       ▼
  session_meta.py    ├─▶ push.py ──▶  scrape_data.*  ──▶  computers/ (registry)
  agent_tasks.py     │   (PostgreSQL            │            │
  facets.py          │    or SQLite)            │            ▼
  app_state.py       │                          └──▶  metrics.json
  plans.py          ─┘
```

No Claude hooks or plugins required — reads files Claude Code already writes.

**Session source of truth = JSONL.** `session_index.py` reconstructs session records from
`projects/**/*.jsonl` (always present); `session_meta.py` telemetry only fills gaps. See
`docs/jsonl-session-index-plan.md`.

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Python 3.11+ required. `psycopg2-binary` is only needed for PostgreSQL; SQLite works with no extra packages.

---

## Usage

### Single-machine mode

Collect and compute locally in one step:

```bash
python batch_runner.py --since 7d --report
```

### Distributed mode (multi-machine team)

**Step 1 — run on each developer's machine:**

```bash
python push.py --central postgresql://user:pass@host:5432/db --since 7d
```

**Step 2 — run centrally to compute metrics:**

```bash
python batch_runner.py --from-store postgresql://user:pass@host:5432/db --since 7d --report
```

Both scripts also read `POSTGRES_URL` from the environment:

```bash
export POSTGRES_URL=postgresql://user:pass@host:5432/db
python push.py --since 7d

# Single week (most recent with data, auto-detected):
python batch_runner.py --from-store $POSTGRES_URL --report

# Specific week:
python batch_runner.py --from-store $POSTGRES_URL --week 2026-W24 --report

# All weeks in the last 90 days:
python batch_runner.py --from-store $POSTGRES_URL --since 90d --all-weeks --report
```

### Backfill prompts for historical turns

If `push.py` was run before prompt collection was added:

```bash
python backfill_prompts.py $POSTGRES_URL
```

---

## Options

### `push.py`

| Flag | Default | Description |
|---|---|---|
| `--central` | `POSTGRES_URL` env | SQLite path or PostgreSQL URL |
| `--since` | `7d` | Collect sessions modified within this window |
| `--dry-run` | off | Show what would be pushed without writing |

### `batch_runner.py`

| Flag | Default | Description |
|---|---|---|
| `--since` | `7d` (or `365d` with `--all-weeks`) | Period to analyse |
| `--week` | most recent week with data | Score a specific week (e.g. `2026-W24`) |
| `--all-weeks` | off | Score every distinct week found in the data |
| `--team-size` | auto | Total developer count for adoption % |
| `--from-store` | — | Read from central store instead of local files |
| `--report` | off | Print human-readable report to stdout |
| `--output` | auto | Custom path for output JSON |
| `--daily` | off | Fast daily update (skips JSONL parsing) |

---

## Data sources (read-only)

| Path | Role |
|---|---|
| `~/.claude*/projects/**/*.jsonl` | **Source of truth** — transcripts: turns, tool_use, structuredPatch (lines), usage (tokens), permissionMode, sidechain, queue-operation. `subagents/` files are not sessions. |
| `~/.claude*/usage-data/session-meta/*.json` | Optional telemetry — enrichment / orphan fallback only (large coverage gaps) |
| `~/.claude*/usage-data/facets/*.json` | AI-analysed outcomes — goal, achievement, friction |
| `~/.claude*/.claude.json` | App state — startup count, background task flag |
| `~/.claude*/plans/*.md` | Plan files — plan-mode fallback signal (`--daily` mode) |

---

## PostgreSQL schema (`scrape_data`)

| Table | Rows | Key columns |
|---|---|---|
| `session_metas` | one per session | `start_time`, `duration_minutes`, `tool_counts`, `ai_title`, `agent_names`, `source` |
| `turn_events` | one per turn | `user_ts`, `agent_ms`, `is_sidechain`, `permission_mode`, `prompt_text`, `tool_uses` |
| `facets` | one per session | `outcome`, `session_type`, `claude_helpfulness`, `goal_categories` |
| `app_state` | one per developer | `total_startups`, `has_used_background_task` |
| `plans` | one per developer | `total_plans`, `new_plans_since_last_run` |
| `agent_tasks` | one per agent invocation | `agent_name`, `task_description`, `status`, `enqueued_at` |
| `background_tasks` | one per agent-less enqueue | `session_id`, `enqueued_at`, `week` (harness M6 background) |
| `busy_segments` | many per session | `start_ts`, `end_ts`, `is_sidechain` (agent hours) |

SQLite mirrors these (agent_tasks / background_tasks stored as JSON blobs). `--force` push is a
**scoped** refresh — deletes + reinserts only the session_ids in the current payload; other
developers' rows are untouched.

---

## Metrics

| ID | Metric | Source |
|---|---|---|
| M1 | AI Adoption Index | active days, project breadth |
| M2 | Agent Hours | busy segments from JSONL — wall-clock (union) vs labor (sum) vs parallelism |
| M3 | Parallel Agents | `agent_colors_in_session`, `is_sidechain` |
| M4 | Session Depth | tool call density, code volume, duration |
| M5 | Orchestration Usage (was Harness) | `permission_mode='plan'` · sub-agent delegation · background tasks (Workflow dropped) |
| M6 | Skill Invocation | slash commands (`/deep-research`, `/code-review`, …) |
| M7 | Trust Index | interruption rate, permission mode |
| M8 | Goal Achievement | facet outcome ratings |
| M9 | Code Velocity | lines per agent hour (per-week; generated-file rate cap) |
| M10 | Consistency Score | coefficient of variation of daily agent hours |
| — | Gini Coefficient | hours inequality across the team |
| — | Trajectory Slope | team score trend over last 4 weeks |
| — | AI Native Score | weighted composite (0–100) |

**Thresholds:**

| Score | Label |
|---|---|
| 86–100 | AI Native |
| 71–85 | AI Augmented |
| 51–70 | AI Assisted |
| 26–50 | AI Aware |
| 0–25 | AI Absent |

Agent hours target: **80 hrs/week per developer**. Below 20 hrs → `stuck`.

---

## Directory layout

```
scrape_data/
  collectors/          one file per data source (read-only, no side effects)
    discover.py        finds all ~/.claude* dirs and maps to developer keys
    sessions.py        parses JSONL → turn events + busy segments (agent hours)
    session_index.py   JSONL → session_meta-shaped records (SOURCE OF TRUTH)
    session_meta.py    usage-data/session-meta telemetry (enrichment / orphans)
    agent_tasks.py     JSONL → agent tasks, background_tasks, ai-titles
    facets.py          reads usage-data/facets/*.json
    app_state.py       reads .claude.json
    plans.py           counts plans/*.md files
    plugins.py         reads installed_plugins.json
    settings.py        reads settings.json
  computers/           MetricComputer subclasses on a singleton registry
    base.py            ComputeContext + MetricComputer ABC
    registry.py        topo-orders deps; runs metric + score phases
    adoption.py M2 · agent_hours.py M3 · parallel_agents.py M4 · depth.py M5
    harness.py M6 (orchestration) · skills.py M7 · trust.py M8 · outcomes.py M9
    velocity.py M10 · consistency.py M13 · equity.py M14/M15 · composite.py M1
  batch_runner.py      orchestrates collect → index → compute → write (local or --from-store)
  central_store.py     SQLite / PostgreSQL dual backend
  metrics_store.py     local output JSON + history tracking
  push.py              per-machine collector → central store
  validate.py          registry + output sanity checks
  backfill_prompts.py  one-time backfill of prompt_text for historical turns
  docs/                metrics.md + 3 plan docs (see CLAUDE.md)
```
