"""
U6 — QAAH computer + composite cap-then-discount rewiring (KTD1, KTD5).

Runnable under pytest or as a plain script.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from computers.base import ComputeContext  # noqa: E402
from computers.qaah import QAAH  # noqa: E402
from computers.composite import _score_developer, _normalize_agent_hours  # noqa: E402


def _ctx(agent_hours, efficiency, usefulness):
    ctx = ComputeContext(
        sessions_by_dev={}, meta_by_sid={}, turns_by_session={}, skill_events=[],
        busy_segments=[], turn_events=[], facets={}, plans={}, app_state={}, agent_tasks={},
    )
    ctx.results = {"agent_hours": agent_hours, "efficiency": efficiency, "usefulness": usefulness}
    return ctx


def _ah(hours, dev="d", wk="W"):
    return {dev: {"developer_key": dev, "by_week": {wk: {"agent_hours_wallclock": hours,
                                                         "agent_hours": hours}}}}


# ── QAAH computer ─────────────────────────────────────────────────────────────

def test_qaah_is_hours_times_factors():
    ctx = _ctx(_ah(80.0),
               {"d": {"by_week": {"W": {"efficiency": 0.95}}}},
               {"d": {"by_week": {"W": {"usefulness_base": 0.53,
                                        "coverage_band": {"pct": 0.6, "label": "medium"}}}}})
    qw = QAAH().compute(ctx)["d"]["by_week"]["W"]
    assert round(qw["qaah"], 1) == 40.3, qw          # 80 × 0.95 × 0.53 ≈ 40.28
    assert qw["scored"] is True


def test_qaah_low_coverage_not_scored():
    ctx = _ctx(_ah(80.0),
               {"d": {"by_week": {"W": {"efficiency": 0.95}}}},
               {"d": {"by_week": {"W": {"usefulness_base": 0.53,
                                        "coverage_band": {"pct": 0.10, "label": "low"}}}}})
    assert QAAH().compute(ctx)["d"]["by_week"]["W"]["scored"] is False


def test_qaah_no_usefulness_passes_hours_through():
    ctx = _ctx(_ah(80.0), {"d": {"by_week": {"W": {"efficiency": 0.95}}}}, {})
    qw = QAAH().compute(ctx)["d"]["by_week"]["W"]
    assert qw["qaah"] == 80.0 and qw["scored"] is False


# ── Composite cap-then-discount (KTD1) ────────────────────────────────────────

def _score(hours, eff, use, scored, wk="W"):
    return _score_developer(
        "d", {}, {"by_week": {wk: {"agent_hours": hours}}}, {}, {}, {}, {}, {}, {}, {},
        qaah_data={"by_week": {wk: {"qaah": round(hours * eff * use, 2), "efficiency": eff,
                                    "usefulness": use, "scored": scored}}},
        week=wk,
    )["components"]["agent_hours"]


def test_composite_cap_then_discount():
    # 80h × 0.5 quality → norm(80)=100 × 0.5 = 50
    assert _score(80.0, 1.0, 0.5, True) == 50.0


def test_composite_grinding_does_not_compensate():
    # 160h and 80h at the same 0.5 quality score identically — hours capped first.
    assert _score(160.0, 1.0, 0.5, True) == _score(80.0, 1.0, 0.5, True) == 50.0


def test_composite_identity_when_quality_perfect():
    # eff = use = 1.0 → dimension equals the raw normalized hours.
    assert _score(40.0, 1.0, 1.0, True) == round(_normalize_agent_hours(40.0), 1)


def test_composite_falls_back_when_low_confidence():
    # scored=False → quantity-only, no discount applied.
    s = _score_developer(
        "d", {}, {"by_week": {"W": {"agent_hours": 80.0}}}, {}, {}, {}, {}, {}, {}, {},
        qaah_data={"by_week": {"W": {"qaah": 40.0, "efficiency": 1.0, "usefulness": 0.5,
                                     "scored": False}}},
        week="W",
    )
    assert s["components"]["agent_hours"] == 100.0
    assert s["qaah_confidence"] == "low_confidence_fallback"


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
