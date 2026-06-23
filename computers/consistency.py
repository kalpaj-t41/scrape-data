"""
M13 — Consistency Score (0–100).

Measures how evenly AI usage is spread across working days.
A developer who logs 8 hrs on Monday and nothing else scores the same
agent-hours total as one doing 1.6 hrs/day — but the latter pattern
is far healthier. Coefficient of variation (std_dev / mean) captures
this: low CV = consistent daily habit.

Score of 100 = perfectly uniform daily use.
Score of 0   = all usage bunched into one day.
"""

import math
from collections import defaultdict
from datetime import datetime

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry


def _date_from_ts(ts: str) -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except Exception:
        return None


@registry.register
class Consistency(MetricComputer):
    name = "consistency"

    def compute(self, ctx: ComputeContext) -> dict:
        results: dict[str, dict] = {}

        for key, sessions in ctx.sessions_by_dev.items():
            day_hours: dict[str, float] = defaultdict(float)
            for meta in sessions:
                date_str = _date_from_ts(meta.get("start_time") or "")
                if not date_str:
                    continue
                duration_s = (meta.get("duration_minutes") or 0) * 60
                user_idle_s = sum(meta.get("user_response_times") or [])
                day_hours[date_str] += max(0.0, duration_s - user_idle_s) / 3600

            values = list(day_hours.values())
            n = len(values)
            if n == 0:
                continue

            mean_h = sum(values) / n
            std_dev = math.sqrt(sum((v - mean_h) ** 2 for v in values) / n) if n > 1 else 0.0
            cv = (std_dev / mean_h) if mean_h > 0 else 1.0
            consistency = max(0.0, 1.0 - min(1.0, cv / 2.0))

            label = (
                "Very consistent" if consistency >= 0.75 else
                "Consistent"      if consistency >= 0.50 else
                "Irregular"       if consistency >= 0.25 else
                "Bursty"
            )

            results[key] = {
                "developer_key":       key,
                "consistency_score":   round(consistency * 100, 1),
                "consistency_label":   label,
                "mean_daily_hours":    round(mean_h, 2),
                "std_dev_daily_hours": round(std_dev, 2),
                "active_days":         n,
                "daily_hours":         {d: round(h, 2) for d, h in sorted(day_hours.items())},
            }

        return results
