"""
M3 — Agent Hours per Person per Week.

Two complementary measures per developer per week:
  - agent_hours_wallclock : real elapsed time at least one agent was busy
                            (overlapping turns / sub-agents merged into a union).
  - agent_hours_labor     : total agent work summed across every parallel stream
                            (sub-agents counted separately — can exceed wall-clock).
  - parallelism           : labor / wallclock (1.0 = no parallelism).

`agent_hours` is kept as an alias of wallclock for backward compatibility with
composite / velocity / equity / batch_runner.

Primary input: busy segments from collectors/sessions.collect_segments()
  [{session_id, developer_key, start_ts, end_ts, is_sidechain}]
Backward-compat: also accepts old per-turn events {user_ts, assistant_ts, ...},
treated as tiny [user_ts, end_ts] segments (degraded — no tool runtime / sub-agents).
Fallback: session_duration - sum(user_response_times) from session-meta, for
sessions with no JSONL on this machine (labelled separately as estimated).
"""

from collections import defaultdict
from datetime import datetime

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry

TARGET_HOURS = 80.0
STUCK_THRESHOLD = 20.0


def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _week(dt: datetime) -> str | None:
    if not dt:
        return None
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _intervals(items: list[dict]):
    """Yield (developer_key, start_dt, end_dt, session_id) from segments or turns."""
    for it in items:
        if it.get("event_type") == "skill":
            continue
        dev = it.get("developer_key")
        if not dev:
            continue
        if "start_ts" in it:                      # busy segment (accurate path)
            s, e = _parse(it.get("start_ts")), _parse(it.get("end_ts"))
        else:                                     # legacy per-turn event
            s, e = _parse(it.get("user_ts")), _parse(it.get("assistant_ts"))
        if not s or not e or e <= s:
            continue
        yield dev, s, e, it.get("session_id")


def _union_ms(intervals: list[tuple[datetime, datetime]]) -> float:
    """Sum of merged (overlap-collapsed) interval lengths, in ms."""
    total = 0.0
    cur_s = cur_e = None
    for s, e in sorted(intervals):
        if cur_s is None:
            cur_s, cur_e = s, e
        elif s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += (cur_e - cur_s).total_seconds() * 1000
            cur_s, cur_e = s, e
    if cur_s is not None:
        total += (cur_e - cur_s).total_seconds() * 1000
    return total


def _status(hours: float) -> str:
    if hours >= TARGET_HOURS:
        return "ai_native"
    if hours >= 40:
        return "on_track"
    if hours >= STUCK_THRESHOLD:
        return "underutilized"
    return "stuck"


@registry.register
class AgentHours(MetricComputer):
    name = "agent_hours"

    def compute(self, ctx: ComputeContext) -> dict:
        segments = ctx.busy_segments or ctx.turn_events
        sessions_by_dev = ctx.sessions_by_dev

        # Per dev → week → list of (start, end) intervals; plus labor sum and coverage.
        seg_by_dev_week: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        labor_ms:        dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        covered:         dict[str, set] = defaultdict(set)

        for dev, s, e, sid in _intervals(segments):
            wk = _week(s)
            if not wk:
                continue
            seg_by_dev_week[dev][wk].append((s, e))
            labor_ms[dev][wk] += (e - s).total_seconds() * 1000
            if sid:
                covered[dev].add(sid)

        # Fallback: sessions present in session-meta but not covered by JSONL segments.
        # Single stream → added equally to labor and wall-clock, tracked as estimated.
        est_ms: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for dev, sessions in sessions_by_dev.items():
            cov = covered.get(dev, set())
            for meta in sessions:
                if meta.get("session_id") in cov:
                    continue
                wk = meta.get("week")
                if not wk:
                    continue
                duration_s  = (meta.get("duration_minutes") or 0) * 60
                user_idle_s = sum(meta.get("user_response_times") or [])
                est_ms[dev][wk] += max(0.0, duration_s - user_idle_s) * 1000

        all_devs = set(seg_by_dev_week) | set(est_ms)
        results: dict[str, dict] = {}
        for dev in all_devs:
            weeks_set = set(seg_by_dev_week.get(dev, {})) | set(est_ms.get(dev, {}))
            weeks: dict[str, dict] = {}
            for wk in weeks_set:
                est       = est_ms.get(dev, {}).get(wk, 0.0) / 3_600_000
                wall      = _union_ms(seg_by_dev_week.get(dev, {}).get(wk, [])) / 3_600_000
                labor     = labor_ms.get(dev, {}).get(wk, 0.0) / 3_600_000
                wall_tot  = round(wall + est, 2)
                labor_tot = round(labor + est, 2)
                parallel  = round(labor_tot / wall_tot, 2) if wall_tot > 0 else 0.0
                weeks[wk] = {
                    "agent_hours":            wall_tot,   # back-compat alias = wall-clock
                    "agent_hours_wallclock":  wall_tot,
                    "agent_hours_labor":      labor_tot,
                    "agent_hours_estimated":  round(est, 2),
                    "parallelism":            parallel,
                    "status":                 _status(wall_tot),
                }
            results[dev] = {"developer_key": dev, "by_week": weeks}

        return results

    def team_summary(self, results: dict, ctx: ComputeContext) -> dict:
        week = ctx.week
        hours_list = [
            results[k]["by_week"].get(week, {}).get("agent_hours", 0.0)
            for k in results
        ]
        if not hours_list:
            return {}
        labor_list = [
            results[k]["by_week"].get(week, {}).get("agent_hours_labor", 0.0)
            for k in results
        ]
        avg = round(sum(hours_list) / len(hours_list), 2)
        return {
            "week": week,
            "avg_agent_hours": avg,
            "total_agent_hours": round(sum(hours_list), 2),
            "total_agent_hours_labor": round(sum(labor_list), 2),
            "developers_at_target": sum(1 for h in hours_list if h >= TARGET_HOURS),
            "developers_stuck": sum(1 for h in hours_list if h < STUCK_THRESHOLD),
            "status": _status(avg),
        }
