"""
M4 — Parallel Agents in Use.

Measures how often developers orchestrate multiple agents simultaneously.
Sources:
  - agent_colors_in_session > 1 → multiple parallel streams in that session
  - uses_task_agent = true → at least one sub-agent spawned
  - isSidechain = true messages → sidechain (sub-agent) activity
  - Overlapping session time windows → concurrent sessions
"""

from collections import defaultdict
from datetime import datetime

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry


def _concurrent_peak(sessions: list[dict]) -> int:
    """Find the maximum number of sessions active at the same moment."""
    events = []
    for s in sessions:
        start_raw = s.get("start_time")
        dur = s.get("duration_minutes", 0) or 0
        if not start_raw:
            continue
        try:
            start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            end_ts = start.timestamp() + dur * 60
            events.append((start.timestamp(), +1))
            events.append((end_ts, -1))
        except Exception:
            continue

    events.sort()
    peak = current = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


@registry.register
class ParallelAgents(MetricComputer):
    name = "parallel_agents"

    def compute(self, ctx: ComputeContext) -> dict:
        turn_events = ctx.turn_events
        meta_by_sid = ctx.meta_by_sid
        agent_tasks = ctx.agent_tasks or {}

        # Per session: max agent colors, has sidechain, uses_task_agent
        session_colors: dict[str, int] = {}
        session_sidechain: dict[str, int] = defaultdict(int)
        session_dev: dict[str, str] = {}

        for event in turn_events:
            if event.get("event_type") == "skill":
                continue
            sid = event.get("session_id", "")
            key = event.get("developer_key", "")
            session_dev[sid] = key
            colors = event.get("agent_colors_in_session", 0)
            session_colors[sid] = max(session_colors.get(sid, 0), colors)
            if event.get("is_sidechain"):
                session_sidechain[sid] += 1

        # Pull developer_key and session_id from agent_tasks for sessions not in turn_events
        for sid, at in agent_tasks.items():
            key = at.get("developer_key", "")
            if key and sid not in session_dev:
                session_dev[sid] = key

        dev_sessions: dict[str, list[dict]] = defaultdict(list)
        all_session_ids = set(session_dev.keys()) | set(meta_by_sid.keys())

        for sid in all_session_ids:
            meta = meta_by_sid.get(sid, {})
            dev_key = session_dev.get(sid) or meta.get("developer_key", "")
            if not dev_key:
                continue

            colors = session_colors.get(sid, 0)
            sidechain_turns = session_sidechain.get(sid, 0)
            uses_task = meta.get("uses_task_agent", False)
            # Also treat sessions with recorded agent tasks as agentive
            has_agent_tasks = bool(agent_tasks.get(sid, {}).get("tasks"))
            is_agentive = colors > 1 or uses_task or sidechain_turns > 0 or has_agent_tasks

            dev_sessions[dev_key].append({
                "session_id": sid,
                "week": meta.get("week"),
                "start_time": meta.get("start_time"),
                "duration_minutes": meta.get("duration_minutes", 0),
                "agent_colors": colors,
                "sidechain_turns": sidechain_turns,
                "uses_task_agent": uses_task,
                "is_agentive": is_agentive,
            })

        results: dict[str, dict] = {}
        for dev_key, sessions in dev_sessions.items():
            total = len(sessions)
            agentive = [s for s in sessions if s["is_agentive"]]
            agentive_count = len(agentive)
            parallel_pct = round(agentive_count / total * 100, 1) if total else 0.0

            avg_colors = (
                round(sum(s["agent_colors"] for s in agentive) / len(agentive), 2)
                if agentive else 0.0
            )

            total_turns = sum(
                1 for e in turn_events
                if e.get("developer_key") == dev_key and e.get("event_type") != "skill"
            )
            sidechain_turns = sum(
                1 for e in turn_events
                if e.get("developer_key") == dev_key
                and e.get("is_sidechain")
                and e.get("event_type") != "skill"
            )
            sidechain_pct = round(sidechain_turns / total_turns * 100, 1) if total_turns else 0.0

            concurrent_peak = _concurrent_peak(sessions)

            # By week
            by_week: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "agentive": 0})
            for s in sessions:
                w = s.get("week") or "unknown"
                by_week[w]["sessions"] += 1
                if s["is_agentive"]:
                    by_week[w]["agentive"] += 1

            results[dev_key] = {
                "developer_key": dev_key,
                "total_sessions": total,
                "agentive_sessions": agentive_count,
                "parallel_sessions_pct": parallel_pct,
                "avg_parallel_agents": avg_colors,
                "sidechain_turns_pct": sidechain_pct,
                "concurrent_session_peak": concurrent_peak,
                "by_week": {
                    w: {
                        "sessions": v["sessions"],
                        "agentive": v["agentive"],
                        "parallel_pct": round(v["agentive"] / v["sessions"] * 100, 1) if v["sessions"] else 0.0,
                    }
                    for w, v in by_week.items()
                },
            }

        return results
