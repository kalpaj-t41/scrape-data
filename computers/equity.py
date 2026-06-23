"""
Team-level equity and trajectory metrics.

Gini coefficient of agent hours: answers "is AI adoption spread evenly or
concentrated in a few people?"
  0.0 = perfectly equal (everyone logs the same hours)
  1.0 = one person does all the AI work

Trajectory slope: linear trend of the team's AI Native Score over the past
4 weekly snapshots.  Positive = improving, negative = regressing.
"""

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry


def _gini(values: list[float]) -> float:
    """Gini coefficient of a list of non-negative values (Lorenz-curve formula)."""
    n = len(values)
    if n == 0:
        return 0.0
    total = sum(values)
    if total == 0:
        return 0.0
    s = sorted(values)
    cumsum = sum((2 * (i + 1) - n - 1) * v for i, v in enumerate(s))
    return round(cumsum / (n * total), 3)


def _slope(pairs: list[tuple[str, float]]) -> float | None:
    """Least-squares linear slope over (ordinal-x, score-y) pairs."""
    n = len(pairs)
    if n < 2:
        return None
    xs = list(range(n))
    ys = [s for _, s in pairs]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    return round(num / den, 2) if den else 0.0


@registry.register
class Equity(MetricComputer):
    name = "equity"
    phase = "score"
    deps = ("composite", "agent_hours")

    def compute(self, ctx: ComputeContext) -> dict:
        developer_scores = ctx.get("composite")
        agent_hours_results = ctx.get("agent_hours")
        week = ctx.week
        weekly_history = ctx.weekly_history

        hours_list = [
            agent_hours_results.get(d["developer_key"], {})
            .get("by_week", {})
            .get(week, {})
            .get("agent_hours", 0.0)
            for d in developer_scores
        ]

        gini_coeff = _gini(hours_list)
        equity_label = (
            "Concentrated"  if gini_coeff > 0.60 else
            "Uneven"        if gini_coeff > 0.35 else
            "Distributed"   if gini_coeff > 0.15 else
            "Equal"
        )

        # Trajectory: last 4 weeks of team score history
        slope = None
        trajectory_label = "Insufficient data"
        if weekly_history:
            pairs = [
                (row["week"], float(row["team_score"]))
                for row in weekly_history
                if row.get("team_score") is not None
            ][-4:]
            slope = _slope(pairs)
            if slope is not None:
                trajectory_label = (
                    "Improving"  if slope > 1.0  else
                    "Regressing" if slope < -1.0 else
                    "Stable"
                )

        mean_h = round(sum(hours_list) / len(hours_list), 2) if hours_list else 0.0

        return {
            "gini_coefficient": gini_coeff,
            "equity_label": equity_label,
            "trajectory_slope_per_week": slope,
            "trajectory_label": trajectory_label,
            "hours_distribution": {
                "min":  round(min(hours_list), 2) if hours_list else 0.0,
                "max":  round(max(hours_list), 2) if hours_list else 0.0,
                "mean": mean_h,
            },
        }
