"""
Usefulness axis (U5) — Throwaway-Segment Rate with a mandatory coverage band.

  usefulness_base = useful_segments / all_busy_segments        (R7, count-based)

Each busy segment is classified *artifact-or-accepted* from deterministic signals
only (KTD3 — AI judgment never moves this number):

  throwaway if  the segment ended in a user interrupt,
                OR every tool call errored (error-only),
                OR a triggered verification failed and none passed,
                OR its edits were reverted in-session (churn: added>0, survived==0).
  useful     otherwise — edits that survived, passing verification, or non-error
                work (Q&A / reads / debugging) that was not thrown away. A "no edit"
                segment is NOT automatically throwaway.

The "next human turn builds on it" acceptance heuristic (origin assumption, still an
open question) is approximated here by the default-useful branch; tightening it is
deferred. Every number carries a coverage band: the share of busy time backed by a
strong signal (a surviving/reverted edit or a verification run) vs estimated from the
absence of a negative signal. Enrichment (git survival, facets) is band-only and not
yet wired here.
"""

from collections import defaultdict
from datetime import datetime

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry

# Tunable (KTD6): hours-backed coverage thresholds for the band label.
_COVERAGE_HIGH = 0.66
_COVERAGE_MED = 0.33


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


def _segment_ms(sig: dict) -> float:
    s, e = _parse(sig.get("start_ts")), _parse(sig.get("end_ts"))
    return max(0.0, (e - s).total_seconds() * 1000) if s and e else 0.0


def _has_strong_signal(sig: dict) -> bool:
    """Strong = the segment produced something deterministically checkable: an edit
    that survived or was reverted, or a verification run. Otherwise the useful/
    throwaway call rests on the acceptance default → counts as estimated coverage."""
    churn = sig.get("churn") or {}
    return bool(churn.get("added")) or bool(sig.get("verification"))


def _is_useful(sig: dict) -> bool:
    calls = sig.get("tool_calls", [])
    ver = sig.get("verification", [])
    churn = sig.get("churn") or {}
    n = len(calls)
    errors = sum(1 for c in calls if c.get("is_error") is True)

    if sig.get("ended_in_interrupt"):
        return False
    if n > 0 and errors == n:
        return False
    ver_pass = any(v.get("passed") for v in ver)
    ver_fail = any(not v.get("passed") for v in ver)
    if ver_fail and not ver_pass:
        return False
    if churn.get("added", 0) > 0 and churn.get("survived", 0) == 0:
        return False
    return True


def _band(pct: float) -> str:
    if pct >= _COVERAGE_HIGH:
        return "high"
    if pct >= _COVERAGE_MED:
        return "medium"
    return "low"


@registry.register
class Usefulness(MetricComputer):
    name = "usefulness"
    deps = ("agent_hours",)

    def compute(self, ctx: ComputeContext) -> dict:
        agent_hours_data = ctx.get("agent_hours")

        # per dev -> week -> counters
        useful:   dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        total:    dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        strong_ms: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        all_ms:    dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

        for sig in ctx.segment_signals:
            dev = sig.get("developer_key")
            wk = _week(_parse(sig.get("start_ts")))
            if not dev or not wk:
                continue
            total[dev][wk] += 1
            if _is_useful(sig):
                useful[dev][wk] += 1
            ms = _segment_ms(sig)
            all_ms[dev][wk] += ms
            if _has_strong_signal(sig):
                strong_ms[dev][wk] += ms

        all_devs = set(total) | set(agent_hours_data)
        results: dict[str, dict] = {}
        for dev in all_devs:
            weeks = set(total.get(dev, {})) | set(agent_hours_data.get(dev, {}).get("by_week", {}))
            by_week: dict[str, dict] = {}
            for wk in weeks:
                t = total.get(dev, {}).get(wk, 0)
                u = useful.get(dev, {}).get(wk, 0)
                base = round(u / t, 3) if t else None
                tot_ms = all_ms.get(dev, {}).get(wk, 0.0)
                cov = (strong_ms.get(dev, {}).get(wk, 0.0) / tot_ms) if tot_ms > 0 else 0.0
                by_week[wk] = {
                    "usefulness_base": base,
                    "useful_segments": u,
                    "total_segments":  t,
                    "coverage_band":   {"pct": round(cov, 3), "label": _band(cov)},
                }
            results[dev] = {"developer_key": dev, "by_week": by_week}

        return results

    def team_summary(self, results: dict, ctx: ComputeContext) -> dict:
        week = ctx.week
        bases, covs = [], []
        for k in results:
            wk = results[k]["by_week"].get(week, {})
            if wk.get("usefulness_base") is not None:
                bases.append(wk["usefulness_base"])
                covs.append(wk.get("coverage_band", {}).get("pct", 0.0))
        if not bases:
            return {}
        return {
            "week": week,
            "avg_usefulness": round(sum(bases) / len(bases), 3),
            "avg_coverage":   round(sum(covs) / len(covs), 3),
        }
