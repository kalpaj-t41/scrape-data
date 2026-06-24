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

# Tunable: below this many tool calls an agent's efficiency is "nothing failed in a
# tiny run", not "impressively efficient". Surfaced via efficiency_signal so a reader
# can tell a trivial 1.0 from a genuine one. (62% of agents on live data are at 1.0;
# this separates the ~1/3 that are trivially clean from the ~2/3 that genuinely are.)
_MIN_CALLS_FOR_SIGNAL = 5


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
            n_calls = 0
            n_failures = 0
            for s in sigs:
                w, rl, _ = _analyze_segment(s)
                wasted_ms += w
                run_lengths.extend(rl)
                if _is_useful(s):
                    useful += 1
                if _has_strong_signal(s):
                    strong_ms += _segment_ms(s)
                for c in s.get("tool_calls", []):
                    n_calls += 1
                    if c.get("is_error") is True:
                        n_failures += 1
            n = len(sigs)
            cov = (strong_ms / busy_ms) if busy_ms > 0 else 0.0
            # Distinguish a trustworthy efficiency from a low-signal one: too few tool
            # calls means 1.0 just reflects a tiny clean run, not real efficiency.
            efficiency_signal = "low" if n_calls < _MIN_CALLS_FOR_SIGNAL else "ok"
            agents[aid] = {
                "agent_id":          aid,
                "agent_kind":        first.get("agent_kind"),
                "agent_type":        first.get("agent_type"),
                "workflow_run_id":   first.get("workflow_run_id"),
                "parent_session":    first.get("session_id"),
                "developer_key":     first.get("developer_key"),
                "week":              _week(_parse(first.get("start_ts"))),
                "busy_hours":        round(busy_ms / 3_600_000, 3),
                "n_segments":        n,
                "n_calls":           n_calls,
                "n_failures":        n_failures,
                "efficiency":        round(max(0.0, 1.0 - wasted_ms / busy_ms), 3) if busy_ms > 0 else 1.0,
                "efficiency_signal": efficiency_signal,
                "thrash_index":      round(sum(run_lengths) / len(run_lengths), 2) if run_lengths else 0.0,
                "usefulness":        round(useful / n, 3) if n else None,
                "coverage_band":     {"pct": round(cov, 3), "label": _band(cov)},
            }

        return {
            "agents":          agents,
            "by_workflow_run": _rollup(agents.values(), "workflow_run_id"),
            "by_agent_type":   _rollup(agents.values(), "agent_type"),
        }
