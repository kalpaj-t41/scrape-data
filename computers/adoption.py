"""
M2 — AI Adoption Index (0–100).

Measures: consistent daily use, team-wide coverage, multi-project breadth.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry

WORKING_DAYS_PER_WEEK = 5


def _working_days_in_range(start: date, end: date) -> int:
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return max(1, count)


@registry.register
class Adoption(MetricComputer):
    name = "adoption"

    def compute(self, ctx: ComputeContext) -> dict:
        sessions_by_dev = ctx.sessions_by_dev
        total_developers = ctx.team_size
        period_days = 7

        now = datetime.now(tz=timezone.utc).date()
        cutoff = now - timedelta(days=period_days)
        working_days = _working_days_in_range(cutoff, now)

        per_developer: dict[str, dict] = {}

        for key, sessions in sessions_by_dev.items():
            active_days: set[str] = set()
            projects: set[str] = set()
            weeks: set[str] = set()

            for meta in sessions:
                d = meta.get("date")
                if not d:
                    continue
                try:
                    session_date = date.fromisoformat(d)
                except Exception:
                    continue
                if session_date < cutoff:
                    continue
                active_days.add(d)
                project = meta.get("project_path") or ""
                if project:
                    projects.add(project)
                week = meta.get("week") or ""
                if week:
                    weeks.add(week)

            if not active_days:
                continue

            active_days_pct = round(len(active_days) / working_days, 3)
            unique_projects = len(projects)
            multi_project_factor = min(1.0, unique_projects / 3)
            adoption_idx = round((active_days_pct * 0.50 + multi_project_factor * 0.20) * 100, 1)

            per_developer[key] = {
                "developer_key":       key,
                "active_days":         len(active_days),
                "active_days_pct":     active_days_pct,
                "unique_projects":     unique_projects,
                "multi_project_factor": round(multi_project_factor, 3),
                "adoption_index":      adoption_idx,
                "weeks_active":        len(weeks),
            }

        # Team level
        all_devs = set(per_developer.keys())
        n_devs = total_developers or len(all_devs)
        team_coverage_pct = round(len(all_devs) / n_devs, 3) if n_devs else 0.0
        avg_active_days_pct = (
            round(sum(v["active_days_pct"] for v in per_developer.values()) / len(per_developer), 3)
            if per_developer else 0.0
        )
        avg_multi_project = (
            round(sum(v["multi_project_factor"] for v in per_developer.values()) / len(per_developer), 3)
            if per_developer else 0.0
        )
        team_adoption_index = round(
            (avg_active_days_pct * 0.50 + team_coverage_pct * 0.30 + avg_multi_project * 0.20) * 100, 1
        )

        return {
            "team": {
                "adoption_index": team_adoption_index,
                "team_coverage_pct": round(team_coverage_pct * 100, 1),
                "active_developers": len(all_devs),
                "total_developers": n_devs,
                "avg_active_days_pct": round(avg_active_days_pct * 100, 1),
                "period_days": period_days,
                "working_days_in_period": working_days,
            },
            "developers": per_developer,
        }
