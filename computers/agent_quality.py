"""
Agent-quality drill-down (U12) — efficiency + usefulness at AGENT grain.

A diagnostic surface, NOT a scored dimension (KTD10): it answers "which agents or
workflows are pulling their weight?" without changing the developer-keyed composite.
Groups the U11 signal stream by agent_id and reuses the U4/U5 per-segment helpers so
agent-grain numbers can't drift from developer-grain ones. A single sub-agent is one
stream, so its busy time is just the sum of its segment spans (no union, no agent_hours
dependency). Rolls up per workflow run and per agent type alongside the per-agent rows.
"""

from collections import defaultdict

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry
from computers.efficiency import _analyze_segment, _parse, _week
from computers.usefulness import _is_useful, _has_strong_signal, _band, _segment_ms


def _rollup(records, key: str) -> dict:
    """Average efficiency/usefulness across agents sharing a key (workflow run / type)."""
    groups: dict[str, list] = defaultdict(list)
    for a in records:
        k = a.get(key)
        if k:
            groups[k].append(a)
    out: dict[str, dict] = {}
    for k, members in groups.items():
        effs = [m["efficiency"] for m in members]
        uses = [m["usefulness"] for m in members if m["usefulness"] is not None]
        out[k] = {
            "n_agents":         len(members),
            "avg_efficiency":   round(sum(effs) / len(effs), 3) if effs else None,
            "avg_usefulness":   round(sum(uses) / len(uses), 3) if uses else None,
            "total_busy_hours": round(sum(m["busy_hours"] for m in members), 3),
        }
    return out


@registry.register
class AgentQuality(MetricComputer):
    name = "agent_quality"
    deps = ("efficiency", "usefulness")

    def compute(self, ctx: ComputeContext) -> dict:
        by_agent: dict[str, list] = defaultdict(list)
        for sig in ctx.segment_signals:
            aid = sig.get("agent_id")
            if aid:
                by_agent[aid].append(sig)

        agents: dict[str, dict] = {}
        for aid, sigs in by_agent.items():
            first = sigs[0]
            busy_ms = sum(_segment_ms(s) for s in sigs)
            wasted_ms = 0.0
            run_lengths: list[int] = []
            useful = 0
            strong_ms = 0.0
            for s in sigs:
                w, rl, _ = _analyze_segment(s)
                wasted_ms += w
                run_lengths.extend(rl)
                if _is_useful(s):
                    useful += 1
                if _has_strong_signal(s):
                    strong_ms += _segment_ms(s)
            n = len(sigs)
            cov = (strong_ms / busy_ms) if busy_ms > 0 else 0.0
            agents[aid] = {
                "agent_id":        aid,
                "agent_kind":      first.get("agent_kind"),
                "agent_type":      first.get("agent_type"),
                "workflow_run_id": first.get("workflow_run_id"),
                "parent_session":  first.get("session_id"),
                "developer_key":   first.get("developer_key"),
                "week":            _week(_parse(first.get("start_ts"))),
                "busy_hours":      round(busy_ms / 3_600_000, 3),
                "n_segments":      n,
                "efficiency":      round(max(0.0, 1.0 - wasted_ms / busy_ms), 3) if busy_ms > 0 else 1.0,
                "thrash_index":    round(sum(run_lengths) / len(run_lengths), 2) if run_lengths else 0.0,
                "usefulness":      round(useful / n, 3) if n else None,
                "coverage_band":   {"pct": round(cov, 3), "label": _band(cov)},
            }

        return {
            "agents":          agents,
            "by_workflow_run": _rollup(agents.values(), "workflow_run_id"),
            "by_agent_type":   _rollup(agents.values(), "agent_type"),
        }
