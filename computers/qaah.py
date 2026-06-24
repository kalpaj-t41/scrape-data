"""
Quality-Adjusted Agent Hours (U6, capstone) — north-star-v2.

  QAAH = agent_hours_wallclock × efficiency × usefulness        (both factors in [0,1])

The displayed QAAH is "useful hours" — raw busy hours scaled by how much of that time
was productive (efficiency) and produced kept output (usefulness). It carries a coverage
band inherited from its weakest input (usefulness, the only estimated factor); efficiency
is deterministic. Below a tunable coverage threshold the week is flagged `scored=False`
so the composite (KTD5) falls back to quantity-only rather than discounting on thin data.

This computer only *emits* QAAH; the composite applies the cap-then-discount scoring
(KTD1: normalize hours to the 80-hr target first, THEN multiply by the factors).
"""

from collections import defaultdict

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry

# Tunable (KTD5/KTD6): minimum usefulness coverage for QAAH to be scored into the
# composite. Below it, the week is low-confidence and the composite uses raw hours.
_COVERAGE_MIN = 0.33


@registry.register
class QAAH(MetricComputer):
    name = "qaah"
    deps = ("agent_hours", "efficiency", "usefulness")

    def compute(self, ctx: ComputeContext) -> dict:
        ah  = ctx.get("agent_hours")
        eff = ctx.get("efficiency")
        use = ctx.get("usefulness")

        results: dict[str, dict] = {}
        for dev, ah_dev in ah.items():
            by_week: dict[str, dict] = {}
            for wk, ahw in ah_dev.get("by_week", {}).items():
                hours = ahw.get("agent_hours_wallclock", ahw.get("agent_hours", 0.0))
                ew = eff.get(dev, {}).get("by_week", {}).get(wk, {})
                uw = use.get(dev, {}).get("by_week", {}).get(wk, {})
                efficiency = ew.get("efficiency", 1.0)
                usefulness = uw.get("usefulness_base")
                band = uw.get("coverage_band") or {"pct": 0.0, "label": "low"}

                if usefulness is None:
                    # No segment signal this week — can't discount; pass hours through.
                    qaah = round(hours, 2)
                    scored = False
                else:
                    qaah = round(hours * efficiency * usefulness, 2)
                    scored = band.get("pct", 0.0) >= _COVERAGE_MIN

                by_week[wk] = {
                    "qaah":                  qaah,
                    "agent_hours_wallclock": round(hours, 2),
                    "efficiency":            efficiency,
                    "usefulness":            usefulness,
                    "coverage_band":         band,
                    "scored":                scored,
                }
            results[dev] = {"developer_key": dev, "by_week": by_week}

        return results

    def team_summary(self, results: dict, ctx: ComputeContext) -> dict:
        week = ctx.week
        vals = [
            results[k]["by_week"].get(week, {}).get("qaah")
            for k in results
            if week in results[k].get("by_week", {})
        ]
        vals = [v for v in vals if v is not None]
        if not vals:
            return {}
        scored = sum(
            1 for k in results
            if results[k]["by_week"].get(week, {}).get("scored")
        )
        return {
            "week": week,
            "avg_qaah":     round(sum(vals) / len(vals), 2),
            "total_qaah":   round(sum(vals), 2),
            "scored_weeks": scored,
        }
