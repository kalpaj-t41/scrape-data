"""
U8 — team concentration reuses equity.py, with a top-1-share field (R15).

Runnable under pytest or as a plain script.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from computers.base import ComputeContext  # noqa: E402
from computers.equity import Equity  # noqa: E402
from computers.composite import Composite  # noqa: E402


def _ctx(composite_list, agent_hours, week="W"):
    ctx = ComputeContext(
        sessions_by_dev={}, meta_by_sid={}, turns_by_session={}, skill_events=[],
        busy_segments=[], turn_events=[], facets={}, plans={}, app_state={}, agent_tasks={},
    )
    ctx.results = {"composite": composite_list, "agent_hours": agent_hours}
    ctx.week = week
    return ctx


def _ah(pairs, week="W"):
    return {dev: {"developer_key": dev, "by_week": {week: {"agent_hours": h}}} for dev, h in pairs}


def test_top_1_share_when_one_person_dominant():
    ctx = _ctx([{"developer_key": "a"}, {"developer_key": "b"}], _ah([("a", 90.0), ("b", 10.0)]))
    out = Equity().compute(ctx)
    assert out["top_1_share"] == 0.9, out
    assert out["gini_coefficient"] > 0.0  # concentrated


def test_top_1_share_when_even():
    ctx = _ctx([{"developer_key": "a"}, {"developer_key": "b"}], _ah([("a", 50.0), ("b", 50.0)]))
    out = Equity().compute(ctx)
    assert out["top_1_share"] == 0.5, out


def test_team_composite_passes_through_top_1_share():
    res = Composite().team_composite(
        [{"ai_native_score": 50.0}],
        equity_data={"top_1_share": 0.7, "gini_coefficient": 0.4, "equity_label": "Uneven"},
    )
    assert res["top_1_share"] == 0.7
    assert res["gini_coefficient"] == 0.4


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
