"""
U4 (Efficiency) + U5 (Usefulness) computers.

Runnable under pytest or as a plain script (python3 tests/test_efficiency_usefulness.py).
Builds a minimal ComputeContext with synthetic segment_signals (and an agent_hours
result for the efficiency denominator) and asserts the computed metrics.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from computers.base import ComputeContext  # noqa: E402
from computers.efficiency import Efficiency, _week, _parse  # noqa: E402
from computers.usefulness import Usefulness  # noqa: E402

WK = _week(_parse("2026-06-24T10:00:00Z"))


def _ctx(segment_signals, agent_hours=None):
    ctx = ComputeContext(
        sessions_by_dev={}, meta_by_sid={}, turns_by_session={}, skill_events=[],
        busy_segments=[], turn_events=[], facets={}, plans={}, app_state={},
        agent_tasks={}, segment_signals=segment_signals,
    )
    ctx.results = {"agent_hours": agent_hours or {}}
    return ctx


def _ah(hours, dev="dev1"):
    return {dev: {"developer_key": dev, "by_week": {WK: {"agent_hours_wallclock": hours,
                                                         "agent_hours": hours}}}}


def _call(ts, name="Bash", target="cmd", is_error=False, interrupted=False):
    return {"name": name, "target": target, "is_error": is_error,
            "interrupted": interrupted, "ts": ts}


def _seg(start, end, tool_calls=None, verification=None, churn=None, ended=False, dev="dev1"):
    return {"session_id": "S1", "developer_key": dev, "start_ts": start, "end_ts": end,
            "is_sidechain": False, "tool_calls": tool_calls or [],
            "verification": verification or [],
            "churn": churn or {"added": 0, "survived": 0, "reverted": 0},
            "ended_in_interrupt": ended}


# ── Efficiency (U4) ───────────────────────────────────────────────────────────

def test_efficiency_perfect_when_no_failures():
    ctx = _ctx([_seg("2026-06-24T10:00:00Z", "2026-06-24T10:05:00Z",
                     [_call("2026-06-24T10:00:10Z"), _call("2026-06-24T10:02:00Z")])],
               _ah(1.0))
    out = Efficiency().compute(ctx)
    assert out["dev1"]["by_week"][WK]["efficiency"] == 1.0


def test_efficiency_penalizes_failure_then_recovery():
    # fail on target X at 10:00:10, recover (same target) at 10:01:10 -> 60s wasted of 1h.
    seg = _seg("2026-06-24T10:00:00Z", "2026-06-24T10:05:00Z", [
        _call("2026-06-24T10:00:10Z", target="X", is_error=True),
        _call("2026-06-24T10:01:10Z", target="X", is_error=False),
    ])
    out = Efficiency().compute(_ctx([seg], _ah(1.0)))
    wk = out["dev1"]["by_week"][WK]
    assert 0.0 < wk["efficiency"] < 1.0, wk
    assert wk["thrash_index"] == 1.0, wk
    assert len(wk["wasted_stretches"]) == 1 and wk["wasted_stretches"][0]["recovered"] is True


def test_efficiency_unrecovered_failure_attributes_tail():
    seg = _seg("2026-06-24T10:00:00Z", "2026-06-24T10:02:00Z", [
        _call("2026-06-24T10:00:10Z", target="X", is_error=True),
    ])
    out = Efficiency().compute(_ctx([seg], _ah(1.0)))
    wk = out["dev1"]["by_week"][WK]
    assert wk["efficiency"] < 1.0
    assert wk["wasted_stretches"][0]["recovered"] is False


# ── Usefulness (U5) ───────────────────────────────────────────────────────────

def test_usefulness_survived_edit_is_useful():
    seg = _seg("2026-06-24T10:00:00Z", "2026-06-24T10:05:00Z",
               [_call("2026-06-24T10:00:10Z", name="Edit", target="a.py")],
               churn={"added": 5, "survived": 5, "reverted": 0})
    out = Usefulness().compute(_ctx([seg]))
    wk = out["dev1"]["by_week"][WK]
    assert wk["usefulness_base"] == 1.0
    assert wk["coverage_band"]["label"] == "high"  # backed by a real edit


def test_usefulness_all_reverted_is_throwaway():
    seg = _seg("2026-06-24T10:00:00Z", "2026-06-24T10:05:00Z",
               [_call("2026-06-24T10:00:10Z", name="Edit", target="a.py")],
               churn={"added": 5, "survived": 0, "reverted": 5})
    out = Usefulness().compute(_ctx([seg]))
    assert out["dev1"]["by_week"][WK]["usefulness_base"] == 0.0


def test_usefulness_error_only_and_interrupt_are_throwaway():
    err = _seg("2026-06-24T10:00:00Z", "2026-06-24T10:01:00Z",
               [_call("2026-06-24T10:00:10Z", is_error=True)])
    intr = _seg("2026-06-24T11:00:00Z", "2026-06-24T11:01:00Z",
                [_call("2026-06-24T11:00:10Z")], ended=True)
    out = Usefulness().compute(_ctx([err, intr]))
    assert out["dev1"]["by_week"][WK]["usefulness_base"] == 0.0


def test_usefulness_failed_verification_is_throwaway():
    seg = _seg("2026-06-24T10:00:00Z", "2026-06-24T10:05:00Z",
               [_call("2026-06-24T10:00:10Z", name="Bash", target="pytest")],
               verification=[{"kind": "test", "passed": False, "ts": "2026-06-24T10:00:10Z"}])
    out = Usefulness().compute(_ctx([seg]))
    assert out["dev1"]["by_week"][WK]["usefulness_base"] == 0.0


def test_usefulness_credits_non_code_work_with_low_coverage():
    # A no-edit, no-error Q&A/read segment: useful, but estimated (low) coverage.
    seg = _seg("2026-06-24T10:00:00Z", "2026-06-24T10:05:00Z",
               [_call("2026-06-24T10:00:10Z", name="Read", target="x.py")])
    out = Usefulness().compute(_ctx([seg]))
    wk = out["dev1"]["by_week"][WK]
    assert wk["usefulness_base"] == 1.0
    assert wk["coverage_band"]["label"] == "low"  # no edit / no verification → estimated


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
