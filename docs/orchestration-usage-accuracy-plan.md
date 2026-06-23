# Plan — Accurate Orchestration Usage (fixes M7 in the metrics redesign)

> Supplies the missing "fix the measurement" piece for the *M7 Orchestration Usage* step in
> `docs/metrics-redesign-plan.md` (which currently keeps the runtime logic unchanged / rename-only).
> Status: **approved, deferred** — execute later.

## Context

**Why this change:** The Harness Utilization Score answers *"is this developer using Claude
Code's power features (Plan mode, sub-agents, background tasks) rather than just basic chat?"*
It feeds the composite AI-Native score (current weight 0.08).

The metric measures those features with **inaccurate proxies**, even though the real evidence is
already collected:

| Component | Today (proxy) | Problem | Accurate signal (already collected, unused) |
|---|---|---|---|
| Plan mode | count of saved `plans/*.md` (`new_plans_since_last_run`) | Plan mode often used without saving; mtime-delta drifts; old files linger | `permission_mode == 'plan'` on turn events (**46 real hits**) |
| Background tasks | lifetime binary `hasUsedBackgroundTask` → 25 pts forever | Never resets, not time-bound, no intensity | `queue-operation` enqueues **without** agent_name (**~56**) |
| Task agents | `uses_task_agent` ratio | OK-ish; ignores `agent_tasks` + sidechain segments | `agent_tasks.tasks` + `busy_segments.is_sidechain` + `uses_task_agent` |
| Workflow tool | `"Workflow" in tool_counts` ratio | **0 occurrences** — dead check, only drags scores down | — (drop) |

**Relationship to `docs/metrics-redesign-plan.md`:** that doc plans a two-layer harness — a NEW
*Environment Readiness* metric (static config) + renaming today's `harness.py` to *M7 Orchestration
Usage*. But it explicitly keeps the runtime **logic unchanged** ("No collector change (lowest risk)",
lines 77-79) — preserving exactly the inaccurate proxies above. **This plan makes M7 accurate, not
just renamed.**

**Decisions (confirmed with user):**
1. **Fold into the existing redesign** as the real content of its "M7 Orchestration Usage" step:
   rename `harness_score → orchestration_score` (keep `harness_score` alias one release) **and**
   fix the measurement. The separate *Environment Readiness* layer + `config_assets.py` collector
   stay deferred to that larger redesign — out of scope here.
2. **3 components**, each ~33.3 pts: **Plan mode · Sub-agent delegation · Background tasks.**
   Drop Workflow. No parallel-depth component — `parallel_agents` (M4, weight 0.10) already owns
   parallelism; a 4th here would double-count.
3. Plan-mode primary signal = raw `permission_mode == 'plan'`; plan-file count kept only as a
   degraded fallback for `--daily` runs.

## The 3 components — signals & formulas

Per developer, per ISO week (new `by_week` breakdown). Each component 0–33.34; sum capped 100.

1. **Plan mode (0–33.3)** — from `ctx.turn_events`.
   - Plan-mode session = a session whose turns include any `permission_mode == 'plan'`.
   - Score = `plan_sessions / total_sessions * 33.34`.
   - **Fallback** when `ctx.turn_events` empty (daily mode): `new_plans_since_last_run` from
     `ctx.plans`, scored `min(33.34, n * 6.5)`. Keeps daily runs from zeroing.

2. **Sub-agent delegation (0–33.3)** — from `ctx.agent_tasks` + `ctx.sessions_by_dev` +
   `ctx.busy_segments`.
   - Delegating session = has a named task in `ctx.agent_tasks[sid].tasks`, OR
     `uses_task_agent == true`, OR any `busy_segments` for that session with `is_sidechain == true`.
   - Score = `delegating_sessions / total_sessions * 33.34`.

3. **Background tasks (0–33.3)** — from `ctx.agent_tasks` (needs the small collector tweak below).
   - Background-task event = `queue-operation` enqueue **without** an agent_name
     (e.g. `"Starting Claude Code on the web…"`), counted per week.
   - Score = `min(33.34, bg_events_this_week * PER_EVENT)` (start PER_EVENT ≈ 11 → ~3 events = full).

`orchestration_score = plan + subagent + background` (0–100), plus `harness_score` alias, a
`by_week` map, and the raw counts behind each component for transparency.

## Files to modify

- **`computers/harness.py`** (primary rewrite — the doc's "M7 Orchestration Usage" step).
  - Keep registration contract: `@registry.register`, `name = "harness"`, `phase = "metric"`,
    `compute(self, ctx)`. (Registry `name` rename is deferred with the broader rename to avoid
    touching `composite.deps`; only the **output keys** change now.)
  - Read `ctx.turn_events`, `ctx.agent_tasks`, `ctx.busy_segments`, `ctx.sessions_by_dev`; keep
    `ctx.plans` for the plan-mode fallback only. **Drop** the `ctx.app_state` /
    `hasUsedBackgroundTask` path and the Workflow component.
  - Emit `orchestration_score` (+ `harness_score` alias), `components`
    (`plan_mode`, `subagent`, `background`), raw counts, and
    `by_week: {week: {orchestration_score, components}}`.
  - Reuse: week label per `agent_hours._week` (`computers/agent_hours.py:39`).

- **`collectors/agent_tasks.py`** (small tweak). Enqueue parser (lines 121-134) currently **drops**
  queue-operations with no `agent_name` (line 133). Add a parallel capture of agent-less enqueues as
  `background_tasks: list[{enqueued_at, week}]` on the session record — do **not** mix into `tasks`
  (sub-agent logic depends on it). One new field; existing `tasks` behaviour unchanged.

- **`computers/composite.py`** (minimal). `_score_developer` (~line 78) reads flat `harness_score`.
  Change to read per-week orchestration with fallbacks — same pattern already used for `agent_hours`:
  `harness_data["by_week"].get(week, {}).get("orchestration_score", harness_data.get("orchestration_score",
  harness_data.get("harness_score", 0.0)))`. **No weight change** (the larger redesign owns
  reweighting); the `components` key stays `"harness"` for now.

- **`docs/metrics-redesign-plan.md`** (amend, not rewrite). Update its §4 "Orchestration Usage" from
  "Logic unchanged… No collector change" to: 3 accurate components + the `agent_tasks` background tweak,
  referencing this plan.

- **No change** to `computers/__init__.py` / registry (same class name/registration).

## Reuse (don't reinvent)

- Week label: `agent_hours._week` (`computers/agent_hours.py:39`).
- Sidechain segments already tagged by `collectors/sessions.collect_segments`
  (`is_sidechain=True`, sessions.py:272-278).
- `permission_mode` already on every turn event (`collectors/sessions.py:118`).
- Background enqueues already parsed (just discarded) in `collectors/agent_tasks.py:121`.

## Degradation contract (must hold)

`--daily` runs skip JSONL → `ctx.turn_events` / `ctx.busy_segments` empty. Plan mode falls back to
plan-file count; sub-agent falls back to `uses_task_agent`; background scores 0 (documented, not
crashed). Full accuracy only on `--all-weeks` / store-backed runs.

## Verification

1. `python3 batch_runner.py --from-store central.db --all-weeks --since 90d`
2. Per-week orchestration components from `~/.claude-metrics/metrics.json`:
   ```
   python3 -c "import json;d=json.load(open('/home/kalpaj/.claude-metrics/metrics.json'));
   [print(w['week'], w['developers'][0].get('components',{}).get('harness')) for w in d['weeks']]"
   ```
3. Cross-check signals against raw data (roughly match exploration counts):
   - Plan: `grep -rl '"permissionMode": *"plan"' ~/.claude*/projects/**/*.jsonl | wc -l`.
   - Background: count `queue-operation` enqueues without agent_name vs component-3 events.
4. Confirm Workflow component gone; no week penalised by it; `orchestration_score == harness_score` alias.
5. `--daily` run still produces a non-crashing score via fallbacks.
6. Sanity: scores vary week-to-week (W24/W25 — busy weeks — higher), not flat.

## Out of scope (owned by `docs/metrics-redesign-plan.md`)

- Environment Readiness metric + `config_assets.py` collector.
- Composite weight rebalancing (harness/parallel family) and the registry `name` →
  `orchestration_usage` rename.
