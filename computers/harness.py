"""
M7 — Orchestration Usage Score (0–100). (Formerly "Harness Utilization".)

Measures real use of Claude Code's orchestration features, scored from accurate
raw signals instead of proxies. Three components, ~33.3 pts each:

  1. Plan mode      — sessions that actually entered Plan mode
                      (turn_events with permission_mode == 'plan').
                      Fallback (daily mode, no JSONL): saved plans/*.md count.
  2. Sub-agent      — sessions that delegated to sub-agents
                      (agent_tasks.tasks, or uses_task_agent, or sidechain segment).
  3. Background     — async/background tasks queued
                      (agent_tasks.background_tasks — agent-less queue-operation enqueues).

Output key is `orchestration_score` with a back-compat `harness_score` alias.
The Workflow component and the lifetime `hasUsedBackgroundTask` flag are dropped
(Workflow had zero real usage; the flag was a non-resetting binary).
"""

from collections import defaultdict

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry

_PLAN_MODE = "plan"
_COMPONENT_MAX = 100.0 / 3.0          # 33.34 per component
_BG_PER_EVENT = 11.0                  # ~3 background tasks/week → full component
_PLAN_FALLBACK_PER_FILE = 6.5         # daily-mode degraded scoring per saved plan


def _score_bucket(b: dict, plan_fallback: float, turns_present: bool) -> tuple[float, float, float]:
    """(plan, subagent, background) sub-scores for one {total, plan, deleg, bg} bucket."""
    total = b["total"]
    if total == 0:
        return 0.0, 0.0, 0.0
    plan_s = (b["plan"] / total * _COMPONENT_MAX) if turns_present else plan_fallback
    sub_s = b["deleg"] / total * _COMPONENT_MAX
    bg_s = min(_COMPONENT_MAX, b["bg"] * _BG_PER_EVENT)
    return plan_s, sub_s, bg_s


@registry.register
class Harness(MetricComputer):
    name = "harness"

    def compute(self, ctx: ComputeContext) -> dict:
        sessions_by_dev = ctx.sessions_by_dev
        turn_events = ctx.turn_events or []
        agent_tasks = ctx.agent_tasks or {}
        segments = ctx.busy_segments or []
        plans = ctx.plans or {}
        turns_present = bool(turn_events)

        # ── session-level signal sets ─────────────────────────────────────────
        # Sessions that entered Plan mode (any non-skill turn with permission_mode='plan').
        plan_sessions = {
            e.get("session_id")
            for e in turn_events
            if e.get("event_type") != "skill" and e.get("permission_mode") == _PLAN_MODE
        }
        # Sessions with sub-agent (sidechain) busy segments.
        sidechain_sessions = {
            seg.get("session_id") for seg in segments if seg.get("is_sidechain")
        }

        def _delegated(sid: str, meta: dict) -> bool:
            if agent_tasks.get(sid, {}).get("tasks"):
                return True
            if meta.get("uses_task_agent"):
                return True
            return sid in sidechain_sessions

        results: dict[str, dict] = {}
        for dev, metas in sessions_by_dev.items():
            # Per-week tally: total sessions, plan-mode sessions, delegating sessions, bg events.
            weeks: dict[str, dict] = defaultdict(
                lambda: {"total": 0, "plan": 0, "deleg": 0, "bg": 0}
            )
            for meta in metas:
                sid = meta.get("session_id")
                wk = meta.get("week") or "unknown"
                b = weeks[wk]
                b["total"] += 1
                if sid in plan_sessions:
                    b["plan"] += 1
                if _delegated(sid, meta):
                    b["deleg"] += 1
                b["bg"] += len(agent_tasks.get(sid, {}).get("background_tasks", []))

            plan_fallback = min(
                _COMPONENT_MAX,
                plans.get(dev, {}).get("new_plans_since_last_run", 0) * _PLAN_FALLBACK_PER_FILE,
            )

            # Per-week scores.
            by_week: dict[str, dict] = {}
            overall = {"total": 0, "plan": 0, "deleg": 0, "bg": 0}
            for wk, b in weeks.items():
                plan_s, sub_s, bg_s = _score_bucket(b, plan_fallback, turns_present)
                by_week[wk] = {
                    "orchestration_score": round(min(100.0, plan_s + sub_s + bg_s), 1),
                    "components": {
                        "plan_mode": round(plan_s, 1),
                        "subagent": round(sub_s, 1),
                        "background": round(bg_s, 1),
                    },
                }
                for k in overall:
                    overall[k] += b[k]

            # Overall (across all weeks) score for the flat alias.
            o_plan, o_sub, o_bg = _score_bucket(overall, plan_fallback, turns_present)
            orch = round(min(100.0, o_plan + o_sub + o_bg), 1)

            results[dev] = {
                "developer_key": dev,
                "orchestration_score": orch,
                "harness_score": orch,            # back-compat alias
                "components": {
                    "plan_mode": round(o_plan, 1),
                    "subagent": round(o_sub, 1),
                    "background": round(o_bg, 1),
                },
                "plan_mode_sessions": overall["plan"],
                "subagent_sessions": overall["deleg"],
                "background_events": overall["bg"],
                "by_week": by_week,
            }

        return results
