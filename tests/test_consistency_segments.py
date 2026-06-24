"""
U7 — Consistency rebuilt on busy-segments (KTD7).

Runnable under pytest or as a plain script.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from computers.base import ComputeContext  # noqa: E402
from computers.consistency import Consistency  # noqa: E402


def _ctx(segs):
    return ComputeContext(
        sessions_by_dev={}, meta_by_sid={}, turns_by_session={}, skill_events=[],
        busy_segments=segs, turn_events=[], facets={}, plans={}, app_state={}, agent_tasks={},
    )


def _seg(dev, s, e):
    return {"developer_key": dev, "start_ts": s, "end_ts": e, "is_sidechain": False}


def test_even_distribution_scores_high():
    segs = [_seg("dev1", f"2026-06-2{d}T10:00:00Z", f"2026-06-2{d}T12:00:00Z") for d in (1, 2, 3)]
    out = Consistency().compute(_ctx(segs))
    assert out["dev1"]["consistency_score"] == 100.0
    assert out["dev1"]["active_days"] == 3


def test_bursty_distribution_scores_low():
    segs = [
        _seg("dev1", "2026-06-21T09:00:00Z", "2026-06-21T17:00:00Z"),   # 8h
        _seg("dev1", "2026-06-22T10:00:00Z", "2026-06-22T10:06:00Z"),   # 0.1h
        _seg("dev1", "2026-06-23T10:00:00Z", "2026-06-23T10:06:00Z"),   # 0.1h
    ]
    out = Consistency().compute(_ctx(segs))
    assert out["dev1"]["consistency_score"] < 50, out["dev1"]


def test_overlapping_segments_unioned_per_day():
    # 10:00-12:00 and 11:00-13:00 on the same day -> union is 10:00-13:00 = 3h, not 4h.
    segs = [
        _seg("dev1", "2026-06-21T10:00:00Z", "2026-06-21T12:00:00Z"),
        _seg("dev1", "2026-06-21T11:00:00Z", "2026-06-21T13:00:00Z"),
    ]
    out = Consistency().compute(_ctx(segs))
    assert out["dev1"]["daily_hours"]["2026-06-21"] == 3.0, out["dev1"]["daily_hours"]


def test_single_active_day_no_crash():
    out = Consistency().compute(_ctx([_seg("dev1", "2026-06-21T10:00:00Z", "2026-06-21T12:00:00Z")]))
    assert out["dev1"]["active_days"] == 1


def test_reads_busy_segments_not_session_meta():
    # sessions_by_dev is empty; consistency must still produce a result from segments.
    out = Consistency().compute(_ctx([_seg("dev1", "2026-06-21T10:00:00Z", "2026-06-21T11:00:00Z")]))
    assert "dev1" in out and out["dev1"]["mean_daily_hours"] == 1.0


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
