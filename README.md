# scrape_data

Offline batch pipeline that reads raw `.claude*` directory files produced by Claude Code, computes AI-nativeness metrics, and writes structured output for dashboarding.

**Core question answered:** *How AI native is this team?*

---

## Architecture

```
Per-machine (push.py)               Central server (batch_runner.py)
──────────────────────              ────────────────────────────────
collectors/                         central_store.pull_raw()
  sessions.py      ─┐                        │
  session_meta.py   │                        ▼
  facets.py         ├─▶ push.py ──▶  scrape_data.*  ──▶  computers/
  agent_tasks.py    │   (PostgreSQL             │            │
  app_state.py      │    or SQLite)             │            ▼
  plans.py         ─┘                           └──▶  metrics.json
```

No Claude hooks or plugins required — reads files Claude Code already writes.

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
python batch_runner.py --from-store $POSTGRES_URL --week 2026-W24 --report
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
| `--since` | `7d` | Period to analyse |
| `--week` | current ISO week | Score a specific week (e.g. `2026-W24`) |
| `--team-size` | auto | Total developer count for adoption % |
| `--from-store` | — | Read from central store instead of local files |
| `--report` | off | Print human-readable report to stdout |
| `--output` | auto | Custom path for output JSON |
| `--daily` | off | Fast daily update (skips JSONL parsing) |

---

## Data sources (read-only)

| Path | What it provides |
|---|---|
| `~/.claude*/projects/**/*.jsonl` | Full conversation transcripts — turns, tool calls, agent events, skills |
| `~/.claude*/usage-data/session-meta/*.json` | Per-session summaries — duration, token counts, lines changed |
| `~/.claude*/usage-data/facets/*.json` | AI-analysed outcomes — goal, achievement, friction |
| `~/.claude*/.claude.json` | App state — startup count, background task flag |
| `~/.claude*/plans/*.md` | Plan files — harness/planning-mode adoption |

---

## PostgreSQL schema (`scrape_data`)

| Table | Rows | Key columns |
|---|---|---|
| `session_metas` | one per session | `start_time`, `duration_minutes`, `tool_counts`, `ai_title`, `agent_names` |
| `turn_events` | one per turn | `user_ts`, `agent_ms`, `is_sidechain`, `prompt_text`, `tool_uses` |
| `facets` | one per session | `outcome`, `session_type`, `claude_helpfulness`, `goal_categories` |
| `app_state` | one per developer | `total_startups`, `has_used_background_task` |
| `plans` | one per developer | `total_plans`, `new_plans_since_last_run` |
| `agent_tasks` | one per agent invocation | `agent_name`, `task_description`, `status`, `enqueued_at` |

---

## Metrics

| ID | Metric | Source |
|---|---|---|
| M1 | AI Adoption Index | active days, project breadth |
| M2 | Agent Hours | `agent_ms` per turn, summed per week |
| M3 | Parallel Agents | `agent_colors_in_session`, `is_sidechain` |
| M4 | Session Depth | tool call density, code volume, duration |
| M5 | Harness Utilization | plan mode, task agents, Workflow tool |
| M6 | Skill Invocation | slash commands (`/deep-research`, `/code-review`, …) |
| M7 | Trust Index | interruption rate, permission mode |
| M8 | Goal Achievement | facet outcome ratings |
| M9 | Code Velocity | lines per agent hour |
| M10 | Consistency Score | coefficient of variation of daily agent hours |
| — | Gini Coefficient | hours inequality across the team |
| — | Trajectory Slope | team score trend over last 4 weeks |
| — | AI Native Score | weighted composite (0–100) |

**Thresholds:**

| Score | Label |
|---|---|
| 80–100 | AI Native |
| 60–79 | AI First |
| 40–59 | AI Aware |
| 20–39 | AI Assisted |
| 0–19 | AI Curious |

Agent hours target: **80 hrs/week per developer**. Below 20 hrs → `stuck`.

---

## Directory layout

```
scrape_data/
  collectors/          one file per data source (read-only, no side effects)
    discover.py        finds all ~/.claude* dirs and maps to developer keys
    session_meta.py    reads usage-data/session-meta/*.json
    sessions.py        parses JSONL transcripts → turn events + prompt text
    agent_tasks.py     parses JSONL → agent names, task descriptions, ai-titles
    facets.py          reads usage-data/facets/*.json
    app_state.py       reads .claude.json
    plans.py           counts plans/*.md files
    plugins.py         reads installed_plugins.json
    settings.py        reads settings.json
  computers/           pure functions — input dicts, output dicts, no I/O
    adoption.py        M1
    agent_hours.py     M2
    parallel_agents.py M3
    depth.py           M4
    harness.py         M5
    skills.py          M6
    trust.py           M7
    outcomes.py        M8
    velocity.py        M9
    consistency.py     M10
    equity.py          Gini + trajectory slope
    composite.py       weighted AI Native Score
  batch_runner.py      orchestrates collect → index → compute → write
  central_store.py     SQLite / PostgreSQL dual backend
  metrics_store.py     local output JSON + history tracking
  push.py              per-machine collector → central store
  backfill_prompts.py  one-time backfill of prompt_text for historical turns
```
