"""
M7 — Skill Invocation Rate.

Tracks which Claude Code skills (slash commands) are used, by whom, how often.
High-value signals: /deep-research, /code-review, /security-review, /run, /verify.
"""

from collections import defaultdict
from datetime import datetime

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry

HIGH_VALUE_SKILLS = {
    "/deep-research",
    "/code-review",
    "/security-review",
    "/run",
    "/verify",
    "/simplify",
    "/review",
}


def _week_from_ts(ts: str) -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    except Exception:
        return None


@registry.register
class Skills(MetricComputer):
    name = "skills"

    def compute(self, ctx: ComputeContext) -> dict:
        dev_skills: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

        for event in ctx.skill_events:
            key = event["developer_key"]
            command = event.get("command", "")
            week = _week_from_ts(event.get("ts", "")) or "unknown"
            dev_skills[key][week].append(command)

        results: dict[str, dict] = {}
        for key, weeks in dev_skills.items():
            all_commands = [cmd for cmds in weeks.values() for cmd in cmds]
            unique = set(all_commands)
            high_value_count = sum(1 for c in all_commands if c in HIGH_VALUE_SKILLS)
            high_value_pct = round(high_value_count / len(all_commands) * 100, 1) if all_commands else 0.0

            by_skill: dict[str, int] = defaultdict(int)
            for cmd in all_commands:
                by_skill[cmd] += 1

            by_week = {
                w: {
                    "invocations": len(cmds),
                    "unique_skills": len(set(cmds)),
                    "skills": list(set(cmds)),
                }
                for w, cmds in weeks.items()
            }

            results[key] = {
                "developer_key": key,
                "total_invocations": len(all_commands),
                "unique_skills_used": sorted(unique),
                "high_value_pct": high_value_pct,
                "by_skill": dict(by_skill),
                "by_week": by_week,
            }

        return results

    def team_summary(self, results: dict, ctx: ComputeContext) -> dict:
        week = ctx.week
        invocations = sum(
            r["by_week"].get(week, {}).get("invocations", 0) for r in results.values()
        )
        all_skills: set[str] = set()
        for r in results.values():
            all_skills.update(r["by_week"].get(week, {}).get("skills", []))
        devs_using = sum(1 for r in results.values() if r["by_week"].get(week, {}).get("invocations", 0) > 0)
        return {
            "week": week,
            "total_invocations": invocations,
            "unique_skills": sorted(all_skills),
            "developers_using_skills": devs_using,
        }
