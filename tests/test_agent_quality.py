"""
U12 — AgentQuality computer (per-agent / per-workflow-run / per-type).

Runnable under pytest or as a plain script (python3 tests/test_agent_quality.py).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from computers.base import ComputeContext  # noqa: E402
from computers.agent_quality import AgentQuality  # noqa: E402


def _ctx(sigs):
    ctx = ComputeContext(
        sessions_by_dev={}, meta_by_sid={}, turns_by_session={}, skill_events=[],
        busy_segments=[], turn_events=[], facets={}, plans={}, app_state={},
        agent_tasks={}, segment_signals=sigs,
    )
    ctx.results = {}
    return ctx


def _call(ts, name="Bash", target="cmd", is_error=False, interrupted=False):
    return {"name": name, "target": target, "is_error": is_error,
            "interrupted": interrupted, "ts": ts}


def _seg(agent_id, start, end, tool_calls=None, churn=None, kind="subagent",
         atype="Explore", wf=None, dev="dev1", ended=False, verification=None):
    return {"session_id": "parentS", "developer_key": dev,
            "agent_kind": kind, "agent_id": agent_id, "agent_type": atype,
            "workflow_run_id": wf, "spawn_tool_use_id": None,
            "start_ts": start, "end_ts": end, "is_sidechain": kind != "main",
            "tool_calls": tool_calls or [], "verification": verification or [],
            "churn": churn or {"added": 0, "survived": 0, "reverted": 0},
            "ended_in_interrupt": ended}


def test_per_agent_distinct_efficiency_usefulness():
    """Two sub-agents with different profiles get distinct efficiency/usefulness."""
    a = _seg("agent-A", "2026-06-24T10:00:00Z", "2026-06-24T10:05:00Z",
             [_call("2026-06-24T10:00:10Z")], churn={"added": 3, "survived": 3, "reverted": 0})
    b = _seg("agent-B", "2026-06-24T10:00:00Z", "2026-06-24T10:05:00Z",
             [_call("2026-06-24T10:00:10Z", target="X", is_error=True),
              _call("2026-06-24T10:01:10Z", target="X", is_error=False)],
             churn={"added": 3, "survived": 0, "reverted": 3})
    ag = AgentQuality().compute(_ctx([a, b]))["agents"]
    assert ag["agent-A"]["efficiency"] == 1.0 and ag["agent-A"]["usefulness"] == 1.0, ag["agent-A"]
    assert ag["agent-B"]["efficiency"] < 1.0 and ag["agent-B"]["usefulness"] == 0.0, ag["agent-B"]


def test_agent_type_rollup_averages_same_type():
    a = _seg("agent-A", "2026-06-24T10:00:00Z", "2026-06-24T10:05:00Z", [_call("2026-06-24T10:00:10Z")])
    b = _seg("agent-B", "2026-06-24T10:00:00Z", "2026-06-24T10:05:00Z", [_call("2026-06-24T10:00:10Z")])
    out = AgentQuality().compute(_ctx([a, b]))
    assert out["by_agent_type"]["Explore"]["n_agents"] == 2
    assert out["by_agent_type"]["Explore"]["avg_efficiency"] == 1.0


def test_workflow_run_rollup_groups_member_agents():
    a = _seg("agent-A", "2026-06-24T10:00:00Z", "2026-06-24T10:05:00Z",
             [_call("2026-06-24T10:00:10Z")], wf="wf1", kind="workflow", atype="workflow-subagent")
    b = _seg("agent-B", "2026-06-24T10:00:00Z", "2026-06-24T10:05:00Z",
             [_call("2026-06-24T10:00:10Z")], wf="wf1", kind="workflow", atype="workflow-subagent")
    out = AgentQuality().compute(_ctx([a, b]))
    assert out["by_workflow_run"]["wf1"]["n_agents"] == 2


def test_main_and_subagent_distinguished():
    m = _seg("S1", "2026-06-24T10:00:00Z", "2026-06-24T10:05:00Z",
             [_call("2026-06-24T10:00:10Z")], kind="main", atype=None)
    s = _seg("agent-A", "2026-06-24T10:00:00Z", "2026-06-24T10:05:00Z",
             [_call("2026-06-24T10:00:10Z")], kind="subagent")
    agents = AgentQuality().compute(_ctx([m, s]))["agents"]
    assert agents["S1"]["agent_kind"] == "main"
    assert agents["agent-A"]["agent_kind"] == "subagent"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
