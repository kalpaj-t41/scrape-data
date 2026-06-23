"""
M5 — Session Depth Score (0–100).

Distinguishes real work sessions from quick queries.
High depth = Claude took meaningful actions (tool calls, code changes, commits).
"""

from collections import defaultdict

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry

_TOOL_WEIGHTS = {
    "Workflow":  5,
    "Agent":     4,
    "Bash":      3,
    "Write":     2,
    "Edit":      2,
    "Read":      1,
    "WebFetch":  1,
    "WebSearch": 1,
}
_DEFAULT_TOOL_WEIGHT = 1


def _session_depth(meta: dict) -> float:
    tool_counts = meta.get("tool_counts") or {}
    total_tools = sum(tool_counts.values())
    tool_score = sum(
        count * _TOOL_WEIGHTS.get(name, _DEFAULT_TOOL_WEIGHT)
        for name, count in tool_counts.items()
    )

    lines = (meta.get("lines_added") or 0) + (meta.get("lines_removed") or 0)
    code_score = min(50, lines / 10)

    duration_min = meta.get("duration_minutes") or 0
    time_score = min(20, duration_min / 3)

    commit_score = (meta.get("git_commits") or 0) * 5 + (meta.get("git_pushes") or 0) * 3

    # Tool density: tool calls per minute — guards against long idle sessions
    # inflating the time_score without real activity
    density = total_tools / max(1.0, duration_min)
    density_bonus = min(10.0, density * 2)

    return min(100.0, tool_score + code_score + time_score + commit_score + density_bonus)


def _label(score: float) -> str:
    if score >= 71:
        return "deep_development"
    if score >= 41:
        return "meaningful_work"
    if score >= 16:
        return "light_assistance"
    return "quick_query"


@registry.register
class Depth(MetricComputer):
    name = "depth"

    def compute(self, ctx: ComputeContext) -> dict:
        results: dict[str, dict] = {}

        for key, sessions in ctx.sessions_by_dev.items():
            weeks: dict[str, list[float]] = defaultdict(list)
            for meta in sessions:
                weeks[meta.get("week") or "unknown"].append(_session_depth(meta))

            all_scores = [s for scores in weeks.values() for s in scores]
            avg = round(sum(all_scores) / len(all_scores), 1) if all_scores else 0.0

            distribution = {"quick_query": 0, "light_assistance": 0, "meaningful_work": 0, "deep_development": 0}
            for s in all_scores:
                distribution[_label(s)] += 1

            by_week = {
                w: {
                    "avg_depth":     round(sum(scores) / len(scores), 1),
                    "session_count": len(scores),
                    "max_depth":     round(max(scores), 1),
                }
                for w, scores in weeks.items()
            }

            results[key] = {
                "developer_key":        key,
                "avg_depth_score":      avg,
                "depth_label":          _label(avg),
                "session_distribution": distribution,
                "total_sessions":       len(all_scores),
                "by_week":              by_week,
            }

        return results
