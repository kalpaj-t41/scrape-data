# AI Nativeness Metrics — Definition, Formulas & Implementation Plan

## Purpose

Quantify how deeply a team integrates AI into their daily engineering workflow.
"AI native" is not about having access to AI — it is about AI being the default
way work gets done, not an occasional aid.

> **Implemented.** The collectors, the class-based computer registry, both store backends
> (SQLite + PostgreSQL), `push.py`, and `batch_runner.py` are built and running.
>
> **Session data source of truth = JSONL.** Per-session metrics are reconstructed from
> `projects/**/*.jsonl` (`collectors/session_index.py`), because `usage-data/session-meta` is
> optional telemetry with large coverage gaps. Telemetry is used only to fill orphan sessions and
> a few fields JSONL can't derive (`user_interruptions`). Each session record carries a `source`
> field (`jsonl` | `telemetry`). See `docs/jsonl-session-index-plan.md`.

---

## Audience & Metric Map

| Metric | CXO | CTO | Team Lead |
|---|:---:|:---:|:---:|
| AI Native Score | ✓ | ✓ | |
| AI Adoption Index | ✓ | ✓ | ✓ |
| Agent Hours per Person per Week | ✓ | ✓ | ✓ |
| Parallel Agents in Use | | ✓ | ✓ |
| Session Depth Score | | ✓ | ✓ |
| Harness Utilization Score | | ✓ | ✓ |
| Skill Invocation Rate | | ✓ | ✓ |
| Trust Index | | | ✓ |
| Goal Achievement Rate | ✓ | ✓ | |
| Code Velocity per AI Hour | ✓ | ✓ | |
| Prompt Quality Index | | | ✓ |
| Multi-Account Coverage | | ✓ | ✓ |

---

## Metric Definitions & Formulas

---

### M1 — AI Native Score (Composite)
**Audience:** CXO, CTO
**Description:** Single 0–100 score that rolls up all dimensions of AI nativeness.

```
AI_Native_Score =
  (Adoption_Index          × 0.20) +
  (Agent_Hours_Score       × 0.25) +
  (Parallel_Agent_Score    × 0.15) +
  (Session_Depth_Score     × 0.15) +
  (Harness_Score           × 0.10) +
  (Trust_Index             × 0.10) +
  (Goal_Achievement_Rate   × 0.05) +
  (Code_Velocity_Score     × 0.05) * 100
```

**Benchmarks:**
| Score | Label |
|---|---|
| 0–25 | AI Absent |
| 26–50 | AI Aware |
| 51–70 | AI Assisted |
| 71–85 | AI Augmented |
| 86–100 | AI Native |

---

### M2 — AI Adoption Index
**Audience:** CXO, CTO, Team Lead
**Description:** Are people using it consistently, across the whole team?

```
active_days_pct       = days_with_≥1_session / total_working_days_in_period

team_coverage_pct     = developers_with_≥1_session_this_week / total_developers

multi_project_factor  = min(1.0, unique_projects_touched / 3)
                        # caps at 3 — using AI across multiple repos = integrated

Adoption_Index (0–100) =
  (active_days_pct  × 0.50) +
  (team_coverage_pct × 0.30) +
  (multi_project_factor × 0.20)
  × 100
```

**Data sources:**
- `session-meta.start_time` → active days per developer
- `session-meta.project_path` → unique project count
- Developer roster (external input) → total_developers

**Targets:**
- `active_days_pct` ≥ 0.80 (4 of 5 working days)
- `team_coverage_pct` = 1.0 (everyone)

---

### M3 — Agent Hours per Person per Week
**Audience:** CXO, CTO, Team Lead
**Description:** Hours the AI agent was actively processing per developer per week.
This is the AI equivalent of "hours billed." The north-star metric.

```
For each session JSONL file, for each conversation turn:
  agent_turn_ms = first_assistant_message.timestamp - user_message.timestamp

session_agent_seconds = Σ agent_turn_ms / 1000  (across all turns in session)

Fallback (when JSONL unavailable):
  session_agent_seconds ≈ (duration_minutes × 60) - Σ(user_response_times)

weekly_agent_hours_per_developer =
  Σ session_agent_seconds (past 7 days, all .claude* dirs, same developer) / 3600
```

**Targets:**
| Hours/week | Status |
|---|---|
| ≥ 80 | AI Native |
| 40–79 | On track |
| 20–39 | Underutilized |
| < 20 | Stuck — intervention needed |

**Notes:**
- Aggregate across ALL `~/.claude*` directories for the developer (work + personal accounts)
- Identity matching: use SHA-256(git_user_email) as developer key across directories

---

### M4 — Parallel Agents in Use
**Audience:** CTO, Team Lead
**Description:** Are developers orchestrating multiple agents simultaneously?

```
Per session:
  distinct_agent_colors  = count of unique agentColor values in session JSONL
  sidechain_threads      = count of distinct parent chains where isSidechain=true
  has_task_agent         = session-meta.uses_task_agent

parallel_sessions_pct =
  sessions_where(distinct_agent_colors > 1 OR has_task_agent) / total_sessions × 100

avg_parallel_agents =
  Σ max(distinct_agent_colors, 1 if has_task_agent else 0) / sessions_with_agents
```

**Concurrent session detection (cross-session parallelism):**
```
For developer D on day X:
  sessions sorted by start_time
  overlapping_pairs = count pairs where session_A.end overlaps session_B.start
  max_concurrent = max number of sessions active at the same moment
```

**Targets:**
- `parallel_sessions_pct` ≥ 30% (AI-native teams run agents in parallel regularly)
- `avg_parallel_agents` ≥ 2 in agentive sessions

---

### M5 — Session Depth Score
**Audience:** CTO, Team Lead
**Description:** Distinguishes real work sessions from quick questions.

```
Per session:
  tool_score = (bash_count × 3) + (edit_count × 2) + (write_count × 2) +
               (read_count × 1) + (agent_count × 4) + (workflow_count × 5)

  code_score  = min(50, (lines_added + lines_removed) / 10)

  time_score  = min(20, duration_minutes / 3)

  commit_score = git_commits × 5 + git_pushes × 3

Session_Depth_Score (0–100) =
  min(100, tool_score + code_score + time_score + commit_score)

Team_Avg_Depth = mean(Session_Depth_Score) across all sessions in period
```

**Interpretation:**
| Score | Session type |
|---|---|
| 0–15 | Quick query — no real work |
| 16–40 | Light assistance |
| 41–70 | Meaningful work session |
| 71–100 | Deep AI-driven development |

---

### M6 — Harness Utilization → Orchestration Usage Score  *(IMPLEMENTED — accurate rewrite)*
**Audience:** CTO, Team Lead
**Description:** Are developers using Claude Code's orchestration capabilities beyond basic chat?

> **Rewritten for accuracy** (`computers/harness.py`, see `docs/orchestration-usage-accuracy-plan.md`).
> The old proxies below were replaced by raw JSONL signals. Output key is `orchestration_score`
> with a `harness_score` alias. The Workflow component (0 real usage) and the lifetime
> `hasUsedBackgroundTask` flag were dropped. Per ISO week.

```
Three components (each 0–33.3 points), scored from raw JSONL:

  Plan_Mode =
    (sessions with permission_mode == 'plan' / total sessions) × 33.34
    (fallback in --daily mode only: min(33.34, new_plans_since_last_run × 6.5))

  Sub_Agent =
    (delegating sessions / total) × 33.34
    delegating = agent_tasks.tasks  OR  uses_task_agent  OR  sidechain busy-segment

  Background =
    min(33.34, background_task_events_this_week × 11)
    (agent-less queue-operation enqueues — collectors/agent_tasks.background_tasks)

Orchestration_Score (0–100) = Plan_Mode + Sub_Agent + Background
```

Deprecated proxies (pre-rewrite, kept for history): plan-file count, `uses_task_agent` only,
lifetime `hasUsedBackgroundTask` flag, `Workflow` in tool_counts.

> Composite still consumes this under the `harness` component key (weight unchanged here; the
> readiness/orchestration reweighting is owned by `docs/metrics-redesign-plan.md`).

---

### M7 — Skill Invocation Rate
**Audience:** CTO, Team Lead
**Description:** Are developers using Claude Code skills (slash commands) to leverage
pre-built AI workflows?

```
From session JSONL, type="system", subtype="local_command":
  extract command from content field (e.g. /deep-research, /code-review, /run)

skill_invocations_per_week =
  count of skill-type slash commands per developer per week

unique_skills_used =
  count of distinct skill names used in the period

skill_coverage_pct =
  developers_using_≥1_skill / total_developers × 100
```

**High-value skills to track separately:**
| Skill | Signal |
|---|---|
| `/deep-research` | Using AI for knowledge work, not just code |
| `/code-review` | AI in the review loop |
| `/run` / `/verify` | AI validating its own output |
| `/security-review` | AI in the security workflow |

---

### M8 — Trust Index
**Audience:** Team Lead
**Description:** How much do developers trust Claude's output? Low trust = high friction.

```
Per session:
  interruption_factor =
    1 - (user_interruptions / max(1, total_turns))

  response_speed_factor =
    min(1.0, 60 / max(1, mean(user_response_times)))
    # <60s avg response = engaged and trusting; longer = carefully reviewing everything

  permission_factor = {
    "bypassPermissions": 1.0,
    "autoEdit":          0.75,
    "default":           0.40
  }[most_common_permissionMode_in_session]

Session_Trust =
  (interruption_factor  × 0.40) +
  (response_speed_factor × 0.30) +
  (permission_factor     × 0.30)

Trust_Index (0–100) = mean(Session_Trust) × 100 over period
```

---

### M9 — Goal Achievement Rate
**Audience:** CXO, CTO
**Description:** Does AI actually help developers accomplish what they set out to do?

```
From facets/*.json:
  achieved_sessions = count where outcome IN ("mostly_achieved", "fully_achieved")
  total_scored_sessions = count where facets file exists

Goal_Achievement_Rate = achieved_sessions / total_scored_sessions × 100

Helpfulness_Distribution =
  group sessions by claude_helpfulness field → pie chart
  ("very_helpful", "helpful", "somewhat_helpful", "not_helpful")
```

---

### M10 — Code Velocity per AI Hour
**Audience:** CXO, CTO
**Description:** How much code output is produced per hour of AI agent time?
Normalizes productivity across team sizes.

```
weekly_lines_changed =
  Σ (lines_added + lines_removed) across all sessions in week

weekly_agent_hours = M3 value

Code_Velocity = weekly_lines_changed / max(1, weekly_agent_hours)

Team_Velocity = Σ weekly_lines_changed / Σ weekly_agent_hours (team-level ratio)
```

---

### M11 — Prompt Quality Index
**Audience:** Team Lead
**Description:** Are developers learning to work with AI effectively over time?
Requires PromptLens plugin data if available; otherwise use proxy.

```
With plugin data:
  Prompt_Quality_Index = mean(quality_score) per developer per week

Without plugin (proxy):
  quality_proxy = (goal_achievement_rate × 0.6) +
                  ((1 - friction_rate) × 0.4)
  where friction_rate = sessions_with_friction / total_sessions

Trend = slope of Prompt_Quality_Index over past 4 weeks
  Positive slope → developer improving at working with AI
```

---

### M12 — Multi-Account Coverage
**Audience:** CTO, Team Lead
**Description:** Ensures metrics reflect the full picture, not just the primary account.
Also signals how deeply AI is embedded (work + personal use).

```
claude_dirs_per_developer =
  count of ~/.claude* directories on developer's machine

sessions_across_all_accounts =
  Σ session counts across all claude* dirs

coverage_ratio =
  sessions_across_all_accounts / sessions_primary_account_only

If coverage_ratio > 1.3 → significant usage outside primary account
→ without multi-account scan you are undercounting by ≥30%
```

---

### M13 — Consistency Score
**Audience:** Team Lead
**Description:** How evenly is AI usage spread across working days? A developer
who logs 8 hrs on Monday and nothing else is counted the same in agent-hours as
one doing 1.6 hrs/day — but the latter pattern shows genuine daily integration.

```
Per developer:
  daily_agent_hours[date] = Σ session_agent_hours on that date

  mean_h  = mean(daily_agent_hours.values())
  std_dev = population std dev of daily_agent_hours.values()
  cv      = std_dev / mean_h          # coefficient of variation
            (capped at 2.0 — beyond that = maximally inconsistent)

Consistency_Score (0–100) = max(0, 1 - min(1, cv / 2)) × 100
```

| Score | Label |
|---|---|
| 75–100 | Very consistent |
| 50–74 | Consistent |
| 25–49 | Irregular |
| 0–24 | Bursty |

---

### M14 — Team Equity (Gini Coefficient)
**Audience:** CXO, CTO
**Description:** Are AI hours spread evenly across the team, or concentrated in
a few power users? A team where one person does 80% of the AI work has a high
AI Native Score but is fragile — most of the team is not actually AI native.

```
Gini coefficient of weekly_agent_hours per developer:

  gini = Σᵢ Σⱼ |hours_i - hours_j| / (2 × n² × mean_hours)

  0.0 = perfectly equal (everyone logs the same hours)
  1.0 = one person does all the AI work
```

| Gini | Label |
|---|---|
| 0.00–0.15 | Equal |
| 0.16–0.35 | Distributed |
| 0.36–0.60 | Uneven |
| > 0.60 | Concentrated |

---

### M15 — Trajectory (Week-over-Week Slope)
**Audience:** CXO, CTO
**Description:** Is the team's AI nativeness improving over time? A snapshot
score of 60 means nothing without knowing if it was 40 last month or 75.

```
weekly_snapshots = last 4 entries from history.jsonl [{week, team_score}]

slope = least-squares linear fit over ordinal week index vs team_score
        (score points gained per week)

slope > 1.0  → Improving
slope < -1.0 → Regressing
else         → Stable
```

---

## Formula Fixes (applied in code)

| Metric | Original | Fixed | Reason |
|---|---|---|---|
| M1 composite weights | summed to 1.05 | normalised to 1.00 | Silent over-scoring at upper end |
| M10 velocity | `lines_added + lines_removed` | `lines_added + lines_removed × 0.5` | Refactoring sessions penalised vs verbose additions |
| M5 depth | duplicate `Bash` key in tool weights | deduplicated | Python silently dropped one entry |
| M5 depth | time_score only | + tool_density bonus (max 10 pts) | Long idle sessions inflated time_score without real work |
| M8 trust | response_speed_factor (60s threshold) | session_completion_rate | Fast response ≠ trust; a sceptical dev can rapid-fire reject |

---

## Summary Dashboard View

### CXO View (Weekly)
```
┌─────────────────────────────────────────────────────────────┐
│  AI NATIVE SCORE         74 / 100   ▲ +6 from last week    │
├─────────────────────────────────────────────────────────────┤
│  Team Adoption            87%  (13/15 devs active)          │
│  Avg Agent Hours/Dev      52 hrs  ↑  (target: 80)          │
│  Goal Achievement         79%  of sessions hit their goal   │
│  Code Velocity            342 lines / agent hour            │
└─────────────────────────────────────────────────────────────┘
```

### CTO View (Weekly)
```
┌─────────────────────────────────────────────────────────────┐
│  Parallel Agent Sessions  34%  of sessions use multi-agent  │
│  Avg Parallel Agents      2.3  per agentive session         │
│  Orchestration Usage      58 / 100  (Plan+SubAgent+Backgrnd)│
│  Skill Invocations        127 / week  (9 unique skills)     │
│  Session Depth            Avg 48 / 100                      │
│  Multi-Account Coverage   1.4×  (40% sessions in alt acct) │
└─────────────────────────────────────────────────────────────┘
```

### Team Lead View (Per Developer, Weekly)
```
┌─────────────────────────────────────────────────────────────┐
│  Developer       Agent Hrs  Depth  Trust  Sessions  Skills  │
│  kalpaj.p        64 hrs     61     82     23        /review │
│  dev2            18 hrs     22     45      8        none    │  ← intervention
│  dev3            91 hrs     78     88     31        /run    │  ← AI native
└─────────────────────────────────────────────────────────────┘
```

---

## Implementation (built)

### Architecture

```
scrape_data/
  collectors/
    discover.py          ← find all .claude* dirs, build developer identity map
    sessions.py          ← parse JSONL: turn events + busy segments (agent hours)
    session_index.py     ← JSONL → session_meta-shaped records (SOURCE OF TRUTH)
    session_meta.py      ← usage-data/session-meta telemetry (enrichment / orphans)
    agent_tasks.py       ← queue-operation tasks + background_tasks (M6 orchestration)
    facets.py            ← parse usage-data/facets/*.json (M9)
    app_state.py         ← parse .claude*.json root files (numStartups, flags)
    plans.py             ← count plan files per account (M6 fallback)
    plugins.py           ← parse installed_plugins.json
    settings.py          ← parse settings.json (hooks, permission modes)
  computers/             ← MetricComputer subclasses on a singleton registry
    base.py              ← ComputeContext + MetricComputer ABC
    registry.py          ← topo-orders deps; metric/score phases
    agent_hours.py M3 · adoption.py M2 · parallel_agents.py M4 · depth.py M5
    harness.py M6(orchestration) · skills.py M7 · trust.py M8 · outcomes.py M9
    velocity.py M10 · consistency.py M13 · composite.py M1 · equity.py M14/M15
  batch_runner.py        ← orchestrator (local or --from-store)
  push.py                ← per-machine collect → central store (no compute)
  central_store.py       ← SQLite + PostgreSQL backends (push / pull_raw / stats)
  metrics_store.py       ← incremental state + output writer
  validate.py            ← sanity checks
  docs/
    metrics.md                          ← this file
    metrics-redesign-plan.md            ← parent plan (harness split + registry)
    orchestration-usage-accuracy-plan.md← child plan (M6 accuracy) — done
    jsonl-session-index-plan.md         ← JSONL-primary sessions — done
  CLAUDE.md
```

> Computers register via `@registry.register` and declare `name` / `deps` / `phase`. The registry
> topologically sorts them (`velocity` after `agent_hours`, `equity` after `composite`) and runs two
> phases: `metric` (once) and `score` (per ISO week). Adding a metric = a new registered class + one
> import line in `computers/__init__.py` — no `batch_runner` changes.

---

### Task Breakdown

#### Phase 1 — Data Collection (Week 1)

- [ ] **T1.1** `collectors/discover.py`
  - Glob `~/.claude*` for directories
  - Read `.claude.json` / `settings.json` per dir to get account identity
  - Derive `developer_key` = SHA-256(git_user_email) or hostname hash
  - Output: `{developer_key: [claude_dir, ...], ...}`

- [ ] **T1.2** `collectors/session_meta.py`
  - Read all `usage-data/session-meta/*.json` across all claude dirs
  - Merge by `developer_key`, tag with `account_type` (work/personal/unknown)
  - Output: list of session meta dicts with `developer_key` added

- [ ] **T1.3** `collectors/sessions.py`
  - Read session JSONL files **incrementally** (skip files with mtime < last_run)
  - Extract per-turn: `(session_id, turn_index, user_ts, assistant_ts, message_type, isSidechain, agentColor, permissionMode, tool_name)`
  - Output: flat list of turn events

- [ ] **T1.4** `collectors/facets.py`
  - Read all `usage-data/facets/*.json`
  - Output: `{session_id: {outcome, session_type, claude_helpfulness, friction_counts}}`

- [ ] **T1.5** `collectors/app_state.py`
  - Read `~/.claude*/.claude.json` files
  - Extract: `numStartups`, `hasUsedBackgroundTask`, `installMethod`
  - Output: `{developer_key: {startups, has_used_background_task, ...}}`

- [ ] **T1.6** `collectors/plans.py`
  - Count `.md` files in `plans/` per claude dir
  - Diff against last run to get `new_plans_this_week`

---

#### Phase 2 — Metric Computation (Week 2)

- [ ] **T2.1** `computers/agent_hours.py` → M3
  - Input: turn events from T1.3
  - Per turn: `agent_ms = assistant_ts - user_ts`
  - Fallback: session_meta duration − Σ(user_response_times)
  - Aggregate per developer per week
  - Flag developers below 20-hr threshold

- [ ] **T2.2** `computers/parallel_agents.py` → M4
  - Input: turn events (agentColor, isSidechain) + session_meta (uses_task_agent)
  - Count distinct agentColors per session
  - Detect overlapping sessions (concurrent use)
  - Compute `parallel_sessions_pct`, `avg_parallel_agents`

- [ ] **T2.3** `computers/adoption.py` → M2
  - Input: session_meta (start_time, project_path) + developer roster
  - Compute `active_days_pct`, `team_coverage_pct`, `multi_project_factor`
  - Output: Adoption_Index per developer and team total

- [ ] **T2.4** `computers/depth.py` → M5
  - Input: session_meta (tool_counts, lines_added, lines_removed, duration_minutes, git_commits)
  - Apply scoring formula, output per-session and per-developer averages

- [ ] **T2.5** `computers/harness.py` → M6
  - Input: plans count (T1.6), session_meta (uses_task_agent, tool_counts), app_state (has_used_background_task)
  - Compute 4-component Harness_Score

- [ ] **T2.6** `computers/skills.py` → M7
  - Input: turn events where type=system, subtype=local_command
  - Parse command name from content field
  - Count per developer per week, list unique skills

- [ ] **T2.7** `computers/trust.py` → M8
  - Input: session_meta (user_interruptions, user_response_times), turn events (permissionMode)
  - Compute Trust_Index per session, average per developer

- [ ] **T2.8** `computers/outcomes.py` → M9
  - Input: facets data (outcome, claude_helpfulness)
  - Compute Goal_Achievement_Rate, Helpfulness_Distribution

- [ ] **T2.9** `computers/velocity.py` → M10
  - Input: session_meta (lines_added, lines_removed) + M3 agent hours
  - Compute Code_Velocity = lines_changed / agent_hours

- [ ] **T2.10** `computers/composite.py` → M1
  - Input: all M2–M10 values
  - Apply weighted formula, output AI_Native_Score
  - Map to benchmark label (AI Absent → AI Native)

---

#### Phase 3 — Batch Runner & Storage (Week 3)

- [ ] **T3.1** `metrics_store.py`
  - SQLite or JSON file at `~/.claude-metrics/store.json`
  - Schema: `{developer_key, week_start, metric_name, value}`
  - Tracks `last_run_timestamp` per directory for incremental processing

- [ ] **T3.2** `batch_runner.py`
  - CLI: `python batch_runner.py --since 7d --output metrics.json`
  - Runs all collectors → computers → store in sequence
  - Logs what was processed, what was skipped (unchanged files)
  - Error handling: any one developer's failure must not abort the full run

- [ ] **T3.3** Incremental logic
  - Skip JSONL files with `mtime < last_run_timestamp`
  - Skip session-meta files already in store
  - Re-compute composite scores even if only some inputs changed

---

#### Phase 4 — Output & Reporting (Week 4)

- [ ] **T4.1** JSON output schema
  ```json
  {
    "period": "2026-W25",
    "team": {
      "ai_native_score": 74,
      "adoption_index": 87,
      "avg_agent_hours_per_dev": 52,
      "goal_achievement_rate": 79,
      "parallel_sessions_pct": 34
    },
    "developers": [
      {
        "developer_key": "abc123",
        "name": "kalpaj.p",
        "agent_hours": 64,
        "depth_score": 61,
        "trust_index": 82,
        "sessions": 23,
        "skills_used": ["/code-review"]
      }
    ]
  }
  ```

- [ ] **T4.2** Markdown report generator
  - Weekly summary in the CXO / CTO / Team Lead format shown above
  - Per-developer table with flag for <20 hrs (intervention needed)

- [ ] **T4.3** HTML report (optional)
  - Simple self-contained HTML with charts using Chart.js (no server required)
  - Weekly trend lines for AI_Native_Score and Agent_Hours

---

### Batch Processing Design

```
Daily batch (lightweight):
  - Collect new session-meta + facets files (fast, small JSON reads)
  - Update adoption, depth, outcomes metrics
  - Run: ~30 seconds

Weekly batch (full):
  - Parse JSONL files for agent_hours + parallel_agents + trust + skills
  - Recompute composite score
  - Generate report
  - Run: ~5 minutes depending on history size

Incremental state file: ~/.claude-metrics/last_run.json
  {
    "last_daily": "2026-06-16T23:00:00Z",
    "last_weekly": "2026-06-15T23:00:00Z",
    "processed_sessions": ["session_id_1", ...]
  }
```

---

### Identity Resolution (Multi-Account)

```python
# Priority order for developer_key:
# 1. SHA-256(git_user_email) — stable across machines
# 2. SHA-256(socket.gethostname()) — machine-stable fallback

# Merging accounts:
# - If two .claude* dirs on same machine have same developer_key → merge
# - If different keys on same machine → treat as separate accounts, sum contributions
#   (developer may have personal + work accounts)
# - Tag each session with account_type:
#   "work"     if project_path contains org-name patterns from git remote
#   "personal" otherwise
```

---

### Open Questions / Decisions Needed

1. **Developer roster** — where does the list of total_developers come from?
   Options: manual config file, GitHub org API, LDAP/directory.

2. **Cross-machine aggregation** — this plan assumes one machine per developer.
   For teams using multiple machines, a shared collection endpoint is needed.

3. **Privacy** — session JSONL contains full prompt text and file paths.
   Decide before running: read only metadata (session-meta + facets) or full transcripts?
   Full transcripts needed for: agent_hours (M3), parallel agents (M4), skills (M7), trust (M8).

4. **Baseline period** — first 2 weeks of data collection should be treated as baseline,
   not scored against targets.
