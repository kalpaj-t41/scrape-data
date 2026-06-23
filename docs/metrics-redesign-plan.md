# Metrics Redesign — Two-Layer Harness (Readiness + Orchestration) + Config Collectors

## Context

The project answers **"how AI-native is this org?"** via a weighted composite score. Today
"harness" (M6, `computers/harness.py`) measures **only runtime feature usage** (plan mode, task
agents, Workflow tool, background tasks). It ignores whether the environment is even *set up* to be
AI-native — no collector reads `CLAUDE.md`, `AGENTS.md`, or skill/agent definitions, even though they
exist on disk.

The user wants AI-nativeness evaluated as **agent work (volume, parallel/serial) × how well the
prompt/environment is harnessed**, where harness includes `CLAUDE.md`/agents/skills quality.

**Decisions (confirmed with user):**
1. **Scope:** rewrite `docs/metrics.md` AND build the new collectors now.
2. **Model:** keep the weighted-composite model; retune weights, fold in new signals.
3. **Harness → two layers:** *Environment Readiness* (static config) scored separately from
   *Orchestration Usage* (runtime, = today's harness.py).
4. **Per-project scope:** read `CLAUDE.md`/`AGENTS.md` AND project-local `.claude/skills/` +
   `.claude/agents/` at every project root (not just global account config).

Agent hours were already redesigned earlier this session (wallclock + labor + parallelism, persisted
to `busy_segments`); this plan does not touch that.

## Design

### 1. New collector: `collectors/config_assets.py`
Contract `collect(developer_map) -> dict[str, dict]` (matches `plans.py`/`app_state.py`). Per-developer,
across all their `claude_dirs`. Reads **only static config** — never opens `settings.json`/
`installed_plugins.json` (those are reused from `settings.collect`/`plugins.collect`).

Signals come from **two scopes** — global (account-level config) and per-project (each repo). Final
`skills_count`/`agents_count` are the **deduped union** across both scopes (by name/stem); per-scope
breakdown is also retained.

- **Global skills** — entries in `<claude_dir>/skills/*` (10 symlinks confirmed) + plugin skills at
  `plugins/marketplaces/<mp>/skills/<name>/` and `plugins/<plugin>/skills/<name>/`.
- **Global agents** — `<claude_dir>/agents/*.md` + `plugins/**/agents/*.md` (confirmed e.g.
  `marketplaces/caveman/agents/cavecrew-*.md`, duplicated across `.claude`/`.claude-personal`).
- **Per-project skills/agents** — for each decoded project root, scan `<root>/.claude/skills/*` and
  `<root>/.claude/agents/*.md`. These reflect how the *repo* is harnessed, not just the dev's machine.
- **projects[]** — for each `<claude_dir>/projects/<encoded>`, decode to a real path, then at that root
  check `CLAUDE.md` / `AGENTS.md` (+ score CLAUDE.md quality) AND count project-local skills/agents.
  Per-project entry: `project_path`, `has_claude_md`, `has_agents_md`, `claude_md_quality`,
  `project_skills`, `project_agents`. Roll up: `projects_with_claude_md`, `best_claude_md_quality`,
  `mean_claude_md_quality`, `projects_with_local_skills`, `projects_with_local_agents`.
- **Dedup:** `skill_names` = union(global ∪ all project-local) by name; `agent_names` =
  union(global ∪ all project-local) by stem. `skills_count`/`agents_count` are the union sizes.

**Fix the path decode (real bug).** `discover._decode_project_path` (collectors/discover.py:46-66) does
`dir_name[1:].replace("-", "/")`, so `-home-kalpaj-Documents-Think41-scrape-data` →
`/home/kalpaj/Documents/Think41/scrape/data` (wrong — real dir is `scrape_data`). New
`_decode_project_root` in config_assets.py:
1. naive decode; accept if the path exists (`exact`).
2. else walk segment-by-segment from `/`, greedily matching each real child dir whose name
   normalized (`-`/`_`→`-`) prefixes the remaining encoded tail; accept only if it lands on an
   existing dir (`fuzzy`) — this resolves `scrape_data`.
3. else skip — never fabricate a path. (Filesystem-validated, so we never invent a CLAUDE.md location.)

### 2. CLAUDE.md quality rubric — `_score_claude_md(text) -> 0..1` (offline, no LLM, read capped 64KB)
Weighted sum: length `min(1, len/1500)` ×0.25 · heading count `min(1, headings/5)` ×0.20 ·
harness-keyword coverage (skill/agent/hook/mcp/plan/workflow/subagent, matched/7) ×0.20 ·
fenced code blocks `min(1, fences/3)` ×0.20 · build/test/run command present (regex) ×0.15.
Documented as a heuristic (gameable) in the doc.

### 3. New computer: `computers/readiness.py` — Environment Readiness (0–100)
Components normalized 0–100, then weighted (saturation consts at module top):
- project_context (`best_claude_md_quality×100`, +10 if any AGENTS.md, cap 100) ×0.30
- skills_defined `min(100, count/8×100)` ×0.20
- agents_defined `min(100, count/4×100)` ×0.15
- hooks_configured `min(100, hook_count/4×100)` ×0.15  *(from settings summary)*
- mcp_configured `100 if has_mcp else 0` ×0.10  *(from settings summary)*
- plugins_installed `min(100, count/3×100)` ×0.10  *(from plugins summary)*

Per-developer + `team_summary()` (CTO view). Empty environment → 0 (intended).

### 4. `computers/harness.py` → Orchestration Usage
**Logic fixed for accuracy** (see `docs/orchestration-usage-accuracy-plan.md`). Three components
(~33.3 pts each), scored per ISO week from accurate raw signals, not proxies:
- **Plan mode** — sessions with `permission_mode == 'plan'` in `ctx.turn_events` (fallback to saved
  `plans/*.md` count only in `--daily` mode). Replaces the old plan-file-count proxy.
- **Sub-agent** — sessions delegating (`agent_tasks.tasks` ∪ `uses_task_agent` ∪ sidechain segment).
- **Background** — agent-less `queue-operation` enqueues, per week.
The **Workflow** component (0 real usage) and the lifetime `hasUsedBackgroundTask` flag are **dropped**.
Output key `harness_score` → `orchestration_score` with a one-release `harness_score` alias; adds a
`by_week` breakdown. **Collector tweak (small):** `collectors/agent_tasks.py` now also captures
agent-less enqueues as a `background_tasks` field (previously discarded).

### 5. `computers/composite.py` — new weights (sum = 1.0)
```
adoption 0.18, agent_hours 0.23, parallel_agents 0.10, depth 0.15,
readiness 0.07, orchestration_usage 0.05, trust 0.08,
outcomes 0.05, velocity 0.04, consistency 0.05
```
Harness family 0.08 → 0.12 (readiness 0.07 + orchestration 0.05), funded by −0.02 adoption,
−0.02 agent_hours. `compute()` gains `readiness_data`, renames `harness_data`→`orchestration_data`
(reads `orchestration_score`, falls back to `harness_score`); drops the old `harness` component key.

### 6. Storage + wiring (minimal)
- **One new dev-keyed table `config_assets`** cloning the `plans`/`app_state` upsert-by-developer_key
  pattern in `central_store.py` (SQLite: `developer_key PK, pushed_at, data`; Postgres: JSONB). Add to
  both DDLs; clone the plans push/pull/stats blocks for both backends.
- **Distributed-mode fix:** `--from-store` has no local files. In `push.py`, **merge the `settings`
  and `plugins` summaries into each `config_assets` payload** before pushing, so `readiness.compute`
  reads everything from the single pulled dict (avoids 3 extra tables).
- `batch_runner.py`: `_collect()` capture+merge config_assets; `_compute()` add `metrics["readiness"]`
  and `metrics["orchestration"]`; `_score_week()` wire both into the composite call + team payload.
- `metrics_store.py`: no change.

### 7. New `docs/metrics.md` structure (headings, kept weighted-composite model)
M1 Composite (new weights) · M2 Adoption · M3 Agent Hours (wallclock/labor/parallelism) ·
M4 Parallel Agents · M5 Session Depth · **M6 Environment Readiness (NEW — incl. rubric table)** ·
**M7 Orchestration Usage (was M6 Harness)** · M8 Skill Invocation · M9 Trust · M10 Goal Achievement ·
M11 Code Velocity · M12 Prompt Quality · M13 Multi-Account · M14 Consistency · M15 Equity/Gini ·
M16 Trajectory. Also update: Audience/Metric map, CTO dashboard mock (add Readiness), Formula-Fixes
table, architecture tree.

## Critical files
- `collectors/config_assets.py` (new), `collectors/discover.py:46` (decode reference)
- `computers/readiness.py` (new), `computers/harness.py`, `computers/composite.py`
- `central_store.py` (config_assets table, both backends), `push.py`, `batch_runner.py`
- `docs/metrics.md`

## Verification
- **Unit:** run `config_assets.collect` on the real dev map → assert global `skills_count ≈ 10`,
  cavecrew agents present (deduped), `scrape_data` resolves to `has_claude_md=true` with correct decode
  (`scrape_data`, not `scrape/data`). Drop a temp `.claude/skills/<x>` + `.claude/agents/<y>.md` into a
  known project root and assert it appears in that project's `project_skills`/`project_agents` and in
  the unioned `skills_count`/`agents_count` (and is deduped if it shares a name with a global one).
- **SQLite E2E:** `python push.py --central /tmp/x.db --since 3650d --force` then
  `python batch_runner.py --from-store /tmp/x.db --all-weeks --report` → config_assets rows > 0;
  no KeyError; composite `components` contains `readiness` + `orchestration_usage`, no `harness`.
- **validate.py extensions:** independent skills-glob ground-truth check; `abs(sum(_WEIGHTS)-1)<1e-9`;
  readiness ∈ [0,100] per dev; `orchestration_score == old harness_score` (rename is behavior-preserving).
- **No-regression:** diff composite components before/after on same DB — only composite shifts (new
  weights), other components byte-identical.

## Risks
- **Decode ambiguity** → filesystem-validate, skip-don't-guess.
- **Distributed mode** has no local config files → mitigated by merging settings/plugins into pushed payload.
- **Skill/agent double-count** across accounts AND across scopes (global vs project-local) → single
  dedup-by-name/stem union; per-scope counts retained for reporting.
- **Per-project scan cost** — scanning `.claude/skills`/`.claude/agents` at every decoded project root
  adds IO proportional to project count; cheap (dir listing), but skip roots that fail to decode.
- **Composite weight change makes `history.jsonl` non-comparable** across cutover → document the cutover
  week; expect a one-time trajectory step.
- **Rubric is gameable** → documented as a heuristic, not ground truth.
- **Postgres DDL** mirrored but unverified locally (SQLite is the tested path) → needs a PG smoke test.

---

# Compute-layer registry refactor (class-based, singleton)

## Context

Every metric in `computers/` is a loose module with a bare `compute(...)` whose signature differs per
file (`adoption.compute(sessions_by_dev, total_developers)`,
`parallel_agents.compute(te, meta_by_sid, agent_tasks)`, `velocity.compute(sessions_by_dev, hours)`, …).
`batch_runner._compute()` hardwires all 11 calls in a fixed order with per-metric args, and
`_score_week()` hardwires `composite`/`equity`/`team_composite`. Adding a metric (e.g. `readiness` above)
means editing `batch_runner` in 3 places and knowing the manual ordering (velocity after agent_hours,
equity after composite). Standardize behind a **class-based registry (singleton)**: each computer is a
`MetricComputer` subclass declaring name + deps; a singleton `MetricRegistry` topo-orders and runs them
against one shared `ComputeContext`.

Decisions: **all 12 computers** converted · **`__new__` singleton + module-level handle** · logic
byte-identical (pure mechanical wrap).

## Two phases (key finding)

The 11 metric computers emit a `by_week` breakdown **once per run**; `composite` (per-developer),
`equity` (needs developer_scores list), `team_composite` (aggregates list) run **once per ISO week**
(`_score_week` is called per week in `--all-weeks` while metric results are reused). So computers carry a
`phase` attribute:
- `phase="metric"` — run once: `adoption, agent_hours, parallel_agents, depth, harness, skills, trust,
  outcomes, velocity, consistency`.
- `phase="score"` — run per `ctx.week`: `composite`, `equity`. `team_composite` stays a method on the
  composite class; `team_summary()` (agent_hours/skills/velocity) stay methods used at payload assembly.

## Dependency DAG

```
metric (once):  adoption, parallel_agents, depth, harness, skills, trust, outcomes, consistency, agent_hours  [no deps]
                velocity  deps=(agent_hours,)
score (per wk): composite deps=(adoption, agent_hours, parallel_agents, depth, harness, trust, outcomes, velocity, consistency)
                equity    deps=(composite, agent_hours)
```

## Pieces

1. **`computers/base.py`** (new) — `@dataclass ComputeContext` holding prebuilt indexes
   (`sessions_by_dev, meta_by_sid, turns_by_session, skill_events, busy_segments, turn_events, facets,
   plans, app_state, agent_tasks`) + run params (`team_size, week, weekly_history, dev_name_map`) +
   `results: dict` (accumulated outputs) with `get(name)`. `MetricComputer(ABC)`: class attrs
   `name`, `deps=()`, `phase="metric"`; abstract `compute(ctx)->dict`; default
   `team_summary(results, ctx)->None`.
2. **`computers/registry.py`** (new) — `MetricRegistry` with `__new__` singleton guard,
   `register(cls)` decorator (instantiates, dedupes by name), `_ordered(phase)` topo sort (visits deps
   across phases, emits only matching-phase nodes; raises on cycle), `run_metrics(ctx)`,
   `run_scores(ctx)`. Module-level `registry = MetricRegistry()` handle used as `@registry.register`.
3. **All 12 computers** — keep existing pure funcs (rename public→`_compute`/`_team_summary`), add thin
   registered class adapting `ctx`→old args. No metric logic changes. Constants
   (`_WEIGHTS`, `TARGET_HOURS`, `_TOOL_WEIGHTS`, …) stay. `composite` folds the per-dev loop + team
   assembly that lives in `_score_week` today; `equity` reads `ctx.get("composite")`.
4. **`computers/__init__.py`** — import all 12 modules (fire `@register` side-effects) + re-export
   `registry`, `ComputeContext`.
5. **`batch_runner.py`** — `_compute` builds same indexes → `ComputeContext` → `registry.run_metrics(ctx)`,
   returns `ctx`. `_score_week` sets `ctx.week`, calls `registry.run_scores(ctx)`, assembles payload from
   `ctx.results` (`composite` scores + `team_composite(scores, equity)` + the 3 `team_summary` calls).
   `run`/`run_all_weeks` carry `ctx` instead of `(metrics, sessions_by_dev)`.

## Payoff

New metric = new `computers/<m>.py` registered class + one import line. Zero `batch_runner` edits. Feeding
composite = add name to `Composite.deps` + entry to `composite._WEIGHTS` (the single touch the readiness
section already needs).

## Verification

- **Byte-identical regression (primary):** `batch_runner --from-store /tmp/x.db --all-weeks` output before
  vs after — `diff` empty except `generated_at`.
- **Registry units (`validate.py`):** 12 names registered; `_ordered("metric")` puts `agent_hours`
  before `velocity`; `_ordered("score")` puts `composite` before `equity`; injected cycle raises
  `ValueError`.
- **Smoke:** `batch_runner --since 30d --report` clean, score unchanged.
- **Extensibility proof:** scratch computer auto-appears in `run_metrics` output with no `batch_runner`
  edit; remove after.
