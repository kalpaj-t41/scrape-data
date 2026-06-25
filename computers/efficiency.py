"""
Efficiency axis (U4) — time-weighted Wasted-Time Ratio + Thrash/Retry Index.

  efficiency = 1 − (wasted_ms_in_stretches / total_busy_ms)

A *wasted stretch* runs from a failed/interrupted tool call to the next successful
retry of the same (tool, target), bounded by a retry window; failures never
recovered attribute their tail (capped at the window) up to the segment end. The
denominator is the per-dev-week wall-clock union from agent_hours (not the sum of
segment spans), so efficiency is "share of real busy time not burned on failure".

Reads the U1/U2 signal stream (ctx.segment_signals). Diagnostic companion — the
Thrash/Retry Index (mean run-length of consecutive same-target failures before a
success) — is reported as a drill-down, not a separate composite dimension. Wasted
stretches are surfaced for transparency, mirroring velocity.py's capped_sessions.
"""

from collections import defaultdict
from datetime import datetime

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry

# Tunable (KTD6): how long after a failure a "successful retry of the same target"
# still counts as recovery, and the cap on an unrecovered failure's attributed tail.
_RETRY_WINDOW_S = 300.0
_MAX_STRETCHES = 10  # per dev-week, surfaced for transparency


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _week(dt: datetime | None) -> str | None:
    if not dt:
        return None
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _bad(c: dict) -> bool:
    return c.get("is_error") is True or c.get("interrupted") is True


def _good(c: dict) -> bool:
    return c.get("is_error") is False and not c.get("interrupted")


def _analyze_segment(sig: dict) -> tuple[float, list[int], list[dict]]:
    """(wasted_ms, run_lengths, wasted_stretches) for one segment."""
    calls = sorted(sig.get("tool_calls", []), key=lambda c: c["ts"])
    seg_end = _parse(sig.get("end_ts"))
    window_ms = _RETRY_WINDOW_S * 1000
    pending: dict[tuple, list] = {}     # (tool, target) -> [start_dt, run_len]
    wasted_ms = 0.0
    run_lengths: list[int] = []
    stretches: list[dict] = []

    for c in calls:
        ts = _parse(c["ts"])
        if not ts:
            continue
        key = (c.get("name"), c.get("target"))
        if _bad(c):
            if key in pending:
                pending[key][1] += 1
            else:
                pending[key] = [ts, 1]
        elif _good(c) and key in pending:
            start, run_len = pending.pop(key)
            gap = min(window_ms, max(0.0, (ts - start).total_seconds() * 1000))
            wasted_ms += gap
            run_lengths.append(run_len)
            stretches.append({"target": c.get("target") or c.get("name"),
                              "wasted_s": round(gap / 1000, 1), "recovered": True})

    # Failures never recovered within the segment: attribute the tail to seg end.
    for (name, target), (start, run_len) in pending.items():
        if not seg_end:
            continue
        gap = min(window_ms, max(0.0, (seg_end - start).total_seconds() * 1000))
        if gap > 0:
            wasted_ms += gap
            run_lengths.append(run_len)
            stretches.append({"target": target or name,
                              "wasted_s": round(gap / 1000, 1), "recovered": False})

    return wasted_ms, run_lengths, stretches


@registry.register
class Efficiency(MetricComputer):
    name = "efficiency"
    deps = ("agent_hours",)

    def compute(self, ctx: ComputeContext) -> dict:
        agent_hours_data = ctx.get("agent_hours")

        wasted_ms: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        runs:      dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        stretches: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))

        for sig in ctx.segment_signals:
            dev = sig.get("developer_key")
            wk = _week(_parse(sig.get("start_ts")))
            if not dev or not wk:
                continue
            w, rl, st = _analyze_segment(sig)
            wasted_ms[dev][wk] += w
            runs[dev][wk].extend(rl)
            stretches[dev][wk].extend(st)

        all_devs = set(wasted_ms) | set(agent_hours_data)
        results: dict[str, dict] = {}
        for dev in all_devs:
            ah_weeks = agent_hours_data.get(dev, {}).get("by_week", {})
            weeks = set(wasted_ms.get(dev, {})) | set(ah_weeks)
            by_week: dict[str, dict] = {}
            for wk in weeks:
                ah = ah_weeks.get(wk, {})
                wall_h = ah.get("agent_hours_wallclock", ah.get("agent_hours", 0.0))
                total_busy_ms = wall_h * 3_600_000
                w = wasted_ms.get(dev, {}).get(wk, 0.0)
                efficiency = (
                    round(max(0.0, 1.0 - w / total_busy_ms), 3) if total_busy_ms > 0 else 1.0
                )
                rl = runs.get(dev, {}).get(wk, [])
                thrash = round(sum(rl) / len(rl), 2) if rl else 0.0
                st = sorted(stretches.get(dev, {}).get(wk, []),
                            key=lambda s: s["wasted_s"], reverse=True)[:_MAX_STRETCHES]
                by_week[wk] = {
                    "efficiency":       efficiency,
                    "thrash_index":     thrash,
                    "wasted_stretches": st,
                }
            results[dev] = {"developer_key": dev, "by_week": by_week}

        return results

    def team_summary(self, results: dict, ctx: ComputeContext) -> dict:
        week = ctx.week
        effs = [
            results[k]["by_week"].get(week, {}).get("efficiency")
            for k in results
            if week in results[k].get("by_week", {})
        ]
        effs = [e for e in effs if e is not None]
        if not effs:
            return {}
        wasted_stretch_count = sum(
            len(results[k]["by_week"].get(week, {}).get("wasted_stretches", []))
            for k in results
        )
        return {
            "week": week,
            "avg_efficiency": round(sum(effs) / len(effs), 3),
            "wasted_stretches": wasted_stretch_count,
        }
