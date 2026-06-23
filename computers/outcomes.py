"""
M9 — Goal Achievement Rate.

Uses AI-analyzed session facets to determine whether Claude actually helped
developers accomplish their goals.
"""

from collections import defaultdict

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry

_ACHIEVED = {"mostly_achieved", "fully_achieved"}
_HELPFULNESS_RANK = {
    "very_helpful":      4,
    "helpful":           3,
    "somewhat_helpful":  2,
    "not_helpful":       1,
}


@registry.register
class Outcomes(MetricComputer):
    name = "outcomes"

    def compute(self, ctx: ComputeContext) -> dict:
        facets = ctx.facets
        meta_by_sid = ctx.meta_by_sid

        dev_facets: dict[str, dict[str, list]] = defaultdict(lambda: {
            "outcomes": [],
            "helpfulness": [],
            "session_types": defaultdict(int),
            "goal_categories": defaultdict(int),
            "friction_total": 0,
            "weeks": defaultdict(lambda: {"achieved": 0, "total": 0}),
        })

        for sid, f in facets.items():
            meta = meta_by_sid.get(sid, {})
            key = meta.get("developer_key") or f.get("developer_key", "")
            if not key:
                continue
            week = meta.get("week", "unknown")
            d = dev_facets[key]

            outcome = f.get("outcome")
            if outcome:
                d["outcomes"].append(outcome)
                d["weeks"][week]["total"] += 1
                if outcome in _ACHIEVED:
                    d["weeks"][week]["achieved"] += 1

            helpfulness = f.get("claude_helpfulness")
            if helpfulness:
                d["helpfulness"].append(helpfulness)

            stype = f.get("session_type")
            if stype:
                d["session_types"][stype] += 1

            for cat in f.get("goal_categories", {}).keys():
                d["goal_categories"][cat] += 1

            friction = f.get("friction_counts") or {}
            d["friction_total"] += sum(friction.values())

        results: dict[str, dict] = {}
        for key, d in dev_facets.items():
            outcomes = d["outcomes"]
            achieved = sum(1 for o in outcomes if o in _ACHIEVED)
            achievement_rate = round(achieved / len(outcomes) * 100, 1) if outcomes else None

            helpfulness_values = [_HELPFULNESS_RANK.get(h, 0) for h in d["helpfulness"]]
            helpfulness_score = (
                round(sum(helpfulness_values) / len(helpfulness_values) / 4 * 100, 1)
                if helpfulness_values else None
            )

            helpfulness_dist: dict[str, int] = defaultdict(int)
            for h in d["helpfulness"]:
                helpfulness_dist[h] += 1

            by_week = {
                w: {
                    "achievement_rate": round(v["achieved"] / v["total"] * 100, 1) if v["total"] else None,
                    "total_sessions": v["total"],
                }
                for w, v in d["weeks"].items()
            }

            results[key] = {
                "developer_key": key,
                "goal_achievement_rate": achievement_rate,
                "helpfulness_score": helpfulness_score,
                "helpfulness_distribution": dict(helpfulness_dist),
                "session_type_distribution": dict(d["session_types"]),
                "top_goal_categories": dict(d["goal_categories"]),
                "total_friction_events": d["friction_total"],
                "scored_sessions": len(outcomes),
                "by_week": by_week,
            }

        return results
