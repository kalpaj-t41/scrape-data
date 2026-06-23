"""
M10 — Code Velocity per AI Hour.

Lines changed per agent hour. Normalizes output across different session lengths.

Two inflation guards (raw aggregate line counts over-state real iteration):
  - removed lines weighted 0.5 (deletes are cheaper than writes).
  - per-session rate cap: a session writing faster than _MAX_LINES_PER_MIN is
    treated as machine-generated scaffold (project init, lockfiles, vendored /
    generated code) and its counted lines are clamped to that rate. session-meta
    only gives aggregate lines_added/removed — no per-file data — so we cannot
    filter generated *files*; the rate cap is the best available proxy. Capped
    sessions are surfaced (capped_sessions) so the clamp is transparent, not hidden.
"""

from collections import defaultdict

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry

# Sustained lines/active-minute above this = generated, not hand-iterated.
# 60/min ≈ one line/sec for the whole session — already implausibly fast for
# AI-assisted code a human is reviewing. Tunable.
_MAX_LINES_PER_MIN = 60.0


def _counted_lines(meta: dict) -> tuple[int, bool]:
    """Velocity-counted lines for one session, plus whether it was rate-capped."""
    added = meta.get("lines_added") or 0
    removed = meta.get("lines_removed") or 0
    raw = added + int(removed * 0.5)

    duration_min = meta.get("duration_minutes") or 0
    if duration_min <= 0:
        return raw, False
    ceiling = int(_MAX_LINES_PER_MIN * duration_min)
    if raw > ceiling:
        return ceiling, True
    return raw, False


@registry.register
class Velocity(MetricComputer):
    name = "velocity"
    deps = ("agent_hours",)

    def compute(self, ctx: ComputeContext) -> dict:
        sessions_by_dev = ctx.sessions_by_dev
        agent_hours_data = ctx.get("agent_hours")

        dev_lines: dict[str, dict[str, int]] = {}
        dev_capped: dict[str, int] = defaultdict(int)
        for key, sessions in sessions_by_dev.items():
            lines_by_week: dict[str, int] = defaultdict(int)
            for meta in sessions:
                week = meta.get("week") or "unknown"
                counted, capped = _counted_lines(meta)
                lines_by_week[week] += counted
                if capped:
                    dev_capped[key] += 1
            dev_lines[key] = dict(lines_by_week)

        results: dict[str, dict] = {}
        all_devs = set(dev_lines.keys()) | set(agent_hours_data.keys())

        for key in all_devs:
            lines_by_week = dev_lines.get(key, {})
            hours_by_week = {
                w: v.get("agent_hours", 0.0)
                for w, v in agent_hours_data.get(key, {}).get("by_week", {}).items()
            }

            total_lines = sum(lines_by_week.values())
            total_hours = sum(hours_by_week.values())
            overall_velocity = round(total_lines / total_hours, 1) if total_hours else 0.0

            all_weeks = set(lines_by_week.keys()) | set(hours_by_week.keys())
            by_week = {}
            for w in all_weeks:
                l = lines_by_week.get(w, 0)
                h = hours_by_week.get(w, 0.0)
                by_week[w] = {
                    "lines_changed": l,
                    "agent_hours": round(h, 2),
                    "velocity": round(l / h, 1) if h else 0.0,
                }

            results[key] = {
                "developer_key": key,
                "velocity_lines_per_hour": overall_velocity,
                "total_lines_changed": total_lines,
                "total_agent_hours": round(total_hours, 2),
                "capped_sessions": dev_capped.get(key, 0),
                "by_week": by_week,
            }

        return results

    def team_summary(self, results: dict, ctx: ComputeContext) -> dict:
        """Per-week team velocity (reads ctx.week — was a global all-weeks total)."""
        week = ctx.week
        week_lines = sum(
            r["by_week"].get(week, {}).get("lines_changed", 0) for r in results.values()
        )
        week_hours = sum(
            r["by_week"].get(week, {}).get("agent_hours", 0.0) for r in results.values()
        )
        capped = sum(r.get("capped_sessions", 0) for r in results.values())
        return {
            "week": week,
            "team_velocity": round(week_lines / week_hours, 1) if week_hours else 0.0,
            "total_lines_changed": week_lines,
            "total_agent_hours": round(week_hours, 2),
            "capped_sessions": capped,
        }
