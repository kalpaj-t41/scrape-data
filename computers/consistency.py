"""
M13 — Consistency Score (0–100).

Measures how evenly AI usage is spread across working days. A developer who logs
8 hrs on Monday and nothing else scores the same agent-hours total as one doing
1.6 hrs/day — but the latter pattern is far healthier. Coefficient of variation
(std_dev / mean) of daily hours captures this: low CV = consistent daily habit.

Source of truth (U7/KTD7): daily hours are the **union of busy segments per day**
(ctx.busy_segments — the same JSONL source agent_hours uses), NOT session-meta
`duration − idle` telemetry. Each segment is attributed to the UTC day of its start
and counted once (a segment spanning midnight is not double-counted); within a day,
overlapping segments are unioned so parallel sub-agents don't inflate the total.

Score of 100 = perfectly uniform daily use.  Score of 0 = all usage in one day.
"""

import math
from collections import defaultdict
from datetime import datetime

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _union_hours(intervals: list[tuple[datetime, datetime]]) -> float:
    """Hours covered by the merged (overlap-collapsed) intervals."""
    total_ms = 0.0
    cur_s = cur_e = None
    for s, e in sorted(intervals):
        if cur_s is None:
            cur_s, cur_e = s, e
        elif s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total_ms += (cur_e - cur_s).total_seconds() * 1000
            cur_s, cur_e = s, e
    if cur_s is not None:
        total_ms += (cur_e - cur_s).total_seconds() * 1000
    return total_ms / 3_600_000


@registry.register
class Consistency(MetricComputer):
    name = "consistency"

    def compute(self, ctx: ComputeContext) -> dict:
        # dev -> UTC-day -> list of (start, end) busy intervals
        seg_by_dev_day: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for seg in ctx.busy_segments:
            dev = seg.get("developer_key")
            s, e = _parse(seg.get("start_ts")), _parse(seg.get("end_ts"))
            if not dev or not s or not e or e <= s:
                continue
            seg_by_dev_day[dev][s.date().isoformat()].append((s, e))

        results: dict[str, dict] = {}
        for dev, days in seg_by_dev_day.items():
            day_hours = {d: _union_hours(ivs) for d, ivs in days.items()}
            values = [h for h in day_hours.values() if h > 0]
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

            results[dev] = {
                "developer_key":       dev,
                "consistency_score":   round(consistency * 100, 1),
                "consistency_label":   label,
                "mean_daily_hours":    round(mean_h, 2),
                "std_dev_daily_hours": round(std_dev, 2),
                "active_days":         n,
                "daily_hours":         {d: round(h, 2) for d, h in sorted(day_hours.items())},
            }

        return results
