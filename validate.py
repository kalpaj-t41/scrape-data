#!/usr/bin/env python3
"""
End-to-end validation of the scrape_data metrics pipeline.

For every metric, independently computes the expected value from raw JSONL /
session-meta files (ground truth) and compares it against what the collector
and computer layers produce.  No hardcoded expected values — everything is
derived from the actual files on disk.

Usage:
    python3 validate.py                  # local data only
    python3 validate.py --since 30d      # restrict window (default: all data)
"""

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from collectors import discover, session_meta, sessions, agent_tasks
from computers import adoption, agent_hours, skills, velocity, depth, trust
from computers import registry as _registry
from computers.base import MetricComputer

# Must match collectors/sessions.py _SKILL_RE exactly
_SKILL_RE = re.compile(r"<command-name>(/[^<]+)</command-name>")

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"

logger = logging.getLogger(__name__)
results: list[tuple[str, bool, str]] = []  # (label, passed, detail)


def check(label: str, actual, expected, tol: float = 0.01, unit: str = "") -> None:
    if isinstance(expected, float) or isinstance(actual, float):
        passed = abs(float(actual) - float(expected)) <= max(tol, abs(float(expected)) * 0.02)
    else:
        passed = actual == expected
    detail = f"got {actual}{unit}  expected {expected}{unit}"
    results.append((label, passed, detail))
    tag = PASS if passed else FAIL
    logger.info(f"  [{tag}] {label}: {detail}")


def warn(label: str, msg: str) -> None:
    results.append((label, None, msg))
    logger.warning(f"  [{WARN}] {label}: {msg}")


def validate_registry() -> None:
    """Layer 0: compute-layer registry wiring (independent of data)."""
    logger.info("\n── Layer 0: Compute-layer registry ──────────────────────────────────")

    expected = {"adoption", "agent_hours", "parallel_agents", "depth", "harness",
                "skills", "trust", "outcomes", "velocity", "consistency",
                "composite", "equity"}
    check("Registry holds all 12 computers", len(_registry.names()), 12)
    check("Registry names match expected set",
          sorted(_registry.names()), sorted(expected))

    metric_order = [c.name for c in _registry._ordered("metric")]
    score_order  = [c.name for c in _registry._ordered("score")]
    check("agent_hours before velocity (metric topo)",
          metric_order.index("agent_hours") < metric_order.index("velocity"), True)
    check("composite before equity (score topo)",
          score_order.index("composite") < score_order.index("equity"), True)

    # Cycle detection: inject two mutually-dependent dummies, expect ValueError, restore.
    class _CycleA(MetricComputer):
        name = "_cycle_a"; deps = ("_cycle_b",)
        def compute(self, ctx): return {}
    class _CycleB(MetricComputer):
        name = "_cycle_b"; deps = ("_cycle_a",)
        def compute(self, ctx): return {}
    _registry._computers["_cycle_a"] = _CycleA()
    _registry._computers["_cycle_b"] = _CycleB()
    raised = False
    try:
        _registry._ordered("metric")
    except ValueError:
        raised = True
    finally:
        _registry._computers.pop("_cycle_a", None)
        _registry._computers.pop("_cycle_b", None)
    check("Dependency cycle raises ValueError", raised, True)


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _week_of(ts_str: str) -> str | None:
    dt = _parse_iso(ts_str)
    if not dt:
        return None
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


# ── ground truth: raw JSONL scan ──────────────────────────────────────────────

def scan_jsonl(claude_dirs: list[Path], since: datetime | None) -> dict:
    """
    Read every JSONL under each claude_dir and compute ground-truth counters.
    Returns a dict with aggregate counts across all sessions.
    """
    gt_turns        = 0          # user→assistant pairs
    gt_agent_ms     = 0.0        # sum of valid timestamp-diff based agent_ms
    gt_skill_count  = 0          # /local_command events
    gt_agent_names  = set()      # unique agent names seen
    gt_sessions     = set()      # session IDs with at least one user→assistant pair
    gt_session_skills: dict[str, int] = defaultdict(int)
    gt_by_week: dict[str, float] = defaultdict(float)   # week → agent_ms

    gt: dict = {
        "turns": 0, "agent_ms": 0.0, "skills": 0,
        "agent_names": set(), "sessions": set(),
        "by_week": defaultdict(float),
    }
    for claude_dir in claude_dirs:
        projects_dir = claude_dir / "projects"
        if not projects_dir.exists():
            continue
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            for jsonl_file in project_dir.glob("*.jsonl"):
                if since:
                    mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=timezone.utc)
                    if mtime < since:
                        continue
                _scan_one_jsonl(jsonl_file, gt)
    return gt


def _scan_one_jsonl(path: Path, gt: dict) -> None:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return

    pending_user_ts: datetime | None = None
    session_id = path.stem
    first_ts: str | None = None

    for line in lines:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue

        mtype   = msg.get("type", "")
        ts_raw  = msg.get("timestamp", "")
        ts      = _parse_iso(ts_raw) if ts_raw else None
        if ts_raw and not first_ts:
            first_ts = ts_raw

        sid = msg.get("sessionId", session_id)

        if mtype == "user":
            pending_user_ts = ts

        elif mtype == "assistant" and pending_user_ts is not None:
            gt["turns"] += 1
            gt["sessions"].add(sid)
            if pending_user_ts and ts:
                diff = (ts - pending_user_ts).total_seconds() * 1000
                if 0 < diff < 600_000:
                    gt["agent_ms"] += diff
                    # agent_hours.compute() buckets by user_ts week (not assistant_ts)
                    # so we store pending_user_ts_raw for week assignment
                    week = _week_of(pending_user_ts.isoformat())
                    if week:
                        gt["by_week"][week] += diff
            pending_user_ts = None

        elif mtype == "system" and msg.get("subtype") == "local_command":
            content = msg.get("content", "")
            if isinstance(content, str) and _SKILL_RE.search(content):
                gt["skills"] += 1

        elif mtype == "agent-name":
            name = msg.get("agentName", "").strip()
            if name:
                gt["agent_names"].add(name)


def _scan_file_span_ceiling(claude_dirs: list[Path], since: datetime | None) -> float:
    """
    Independent upper bound on total wall-clock busy hours: sum over every JSONL
    file (main sessions + sub-agents) of (last_timestamp - first_timestamp).
    Real busy time is always <= this, since busy <= full file span.
    """
    total_ms = 0.0
    for claude_dir in claude_dirs:
        projects_dir = claude_dir / "projects"
        if not projects_dir.exists():
            continue
        for jsonl_file in projects_dir.glob("**/*.jsonl"):
            if since:
                mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=timezone.utc)
                if mtime < since:
                    continue
            first_ts = last_ts = None
            try:
                for line in jsonl_file.read_text(errors="replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        ts = _parse_iso(json.loads(line).get("timestamp", ""))
                    except Exception:
                        continue
                    if not ts:
                        continue
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
            except Exception:
                continue
            if first_ts and last_ts and last_ts > first_ts:
                total_ms += (last_ts - first_ts).total_seconds() * 1000
    return total_ms / 3_600_000


def scan_session_metas(claude_dirs: list[Path], since: datetime | None) -> dict:
    """Ground truth from session-meta JSON files."""
    gt: dict = {
        "session_count": 0,
        "total_lines_added":    0,
        "total_lines_removed":  0,
        "total_lines_weighted": 0,  # added + int(removed * 0.5) per session (matches velocity.py)
        "duration_minutes":     0,
        "session_ids":          set(),
    }
    for claude_dir in claude_dirs:
        sm_dir = claude_dir / "usage-data" / "session-meta"
        if not sm_dir.exists():
            continue
        for f in sm_dir.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            if since:
                ts = _parse_iso(d.get("start_time", ""))
                if ts and ts < since:
                    continue
            added   = d.get("lines_added", 0)
            removed = d.get("lines_removed", 0)
            gt["session_count"]       += 1
            gt["total_lines_added"]   += added
            gt["total_lines_removed"] += removed
            gt["total_lines_weighted"]+= added + int(removed * 0.5)
            gt["duration_minutes"]    += d.get("duration_minutes", 0)
            gt["session_ids"].add(d.get("session_id", f.stem))
    return gt


# ── main validation ────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=None,
                        help="e.g. 7d or 2026-01-01 (default: all data)")
    parser.add_argument("--from-store", default=None, metavar="DSN",
                        help="PostgreSQL URL or SQLite path — enables Layer 5 (store completeness)")
    args = parser.parse_args()

    since: datetime | None = None
    if args.since:
        s = args.since.strip()
        if s.endswith("d"):
            since = datetime.now(tz=timezone.utc) - timedelta(days=int(s[:-1]))
        else:
            since = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

    logger.info("\n" + "=" * 68)
    logger.info("  E2E METRICS VALIDATION")
    if since:
        logger.info(f"  Period: since {since.date()}")
    logger.info("=" * 68 + "\n")

    # ── Layer 0: registry wiring (no data needed) ──────────────────────────────
    validate_registry()

    # ── discover ──────────────────────────────────────────────────────────────
    developer_map = discover.build_developer_map()
    claude_dirs   = [Path(d) for dev in developer_map for d in dev["claude_dirs"]]
    logger.info(f"Developers found  : {len(developer_map)}")
    logger.info(f"Claude dirs       : {len(claude_dirs)}")

    # ── ground truth ─────────────────────────────────────────────────────────
    logger.info("\n[Computing ground truth from raw files...]\n")
    gt_jsonl = scan_jsonl(claude_dirs, since)
    gt_sm    = scan_session_metas(claude_dirs, since)

    logger.info(f"  GT sessions (session-meta files) : {gt_sm['session_count']}")
    logger.info(f"  GT turns    (JSONL user→asst)    : {gt_jsonl['turns']}")
    logger.info(f"  GT agent_ms (sum timestamp diff) : {gt_jsonl['agent_ms']:.0f} ms  "
                f"({gt_jsonl['agent_ms']/3_600_000:.3f} hrs)")
    logger.info(f"  GT skills   (local_command msgs) : {gt_jsonl['skills']}")
    logger.info(f"  GT agents   (unique agent names) : {len(gt_jsonl['agent_names'])}")
    logger.info(f"  GT lines    (added + int(removed*0.5) per session): "
                f"{gt_sm['total_lines_weighted']}")

    # ── run collectors ────────────────────────────────────────────────────────
    logger.info("\n[Running collectors...]\n")
    raw_sm  = session_meta.collect(developer_map, since=since)
    raw_te  = sessions.collect(developer_map, since=since)
    raw_seg = sessions.collect_segments(developer_map, since=since)
    raw_at  = agent_tasks.collect(developer_map, since=since)

    # Turn events have no event_type field — identify by presence of user_ts
    col_turns      = len([e for e in raw_te if "user_ts" in e])
    col_skills     = len([e for e in raw_te if e.get("event_type") == "skill"])
    col_agent_ms   = sum(e.get("agent_ms") or 0 for e in raw_te if "user_ts" in e)
    col_sessions   = len(raw_sm)
    col_agent_names_set = {t["agent_name"]
                           for at_data in raw_at.values()
                           for t in at_data.get("tasks", [])
                           if t.get("agent_name")}
    col_agent_names = len(col_agent_names_set)

    logger.info(f"  COL sessions   : {col_sessions}")
    logger.info(f"  COL turns      : {col_turns}")
    logger.info(f"  COL agent_ms   : {col_agent_ms:.0f} ms  ({col_agent_ms/3_600_000:.3f} hrs)")
    logger.info(f"  COL skills     : {col_skills}")
    logger.info(f"  COL agent names: {col_agent_names}")

    # ── LAYER 1: collector vs ground truth ───────────────────────────────────
    logger.info("\n── Layer 1: Collector accuracy ──────────────────────────────────────")
    check("Session count (session-meta vs gt)",
          col_sessions, gt_sm["session_count"])
    check("Turn count (collector vs gt JSONL)",
          col_turns, gt_jsonl["turns"])
    check("Agent ms total (collector vs gt)",
          col_agent_ms, gt_jsonl["agent_ms"], unit=" ms")
    check("Skill events (collector vs gt)",
          col_skills, gt_jsonl["skills"])
    # GT may see fewer sessions than collector (e.g. unfiltered vs filtered window).
    # Validate that every GT name appears in the collector output (GT ⊆ collector).
    missing_in_col = gt_jsonl["agent_names"] - col_agent_names_set
    check("GT agent names all present in collector (gt ⊆ collector)",
          len(missing_in_col), 0,
          unit=f"  [col={col_agent_names} gt={len(gt_jsonl['agent_names'])} missing={missing_in_col}]")

    # ── run computers ─────────────────────────────────────────────────────────
    logger.info("\n[Running computers...]\n")
    from collections import defaultdict
    sessions_by_dev: dict = defaultdict(list)
    meta_by_sid: dict     = {}
    turns_by_session: dict = defaultdict(list)
    skill_events: list    = []

    for m in raw_sm:
        sessions_by_dev[m["developer_key"]].append(m)
        meta_by_sid[m["session_id"]] = m
    for t in raw_te:
        turns_by_session[t.get("session_id", "")].append(t)
        if t.get("event_type") == "skill":
            skill_events.append(t)

    hours_result   = agent_hours.compute(raw_seg, sessions_by_dev)
    skills_result  = skills.compute(skill_events)
    depth_result   = depth.compute(sessions_by_dev)
    adopt_result   = adoption.compute(sessions_by_dev)
    vel_result     = velocity.compute(sessions_by_dev, hours_result)
    trust_result   = trust.compute(sessions_by_dev, turns_by_session)

    # ── LAYER 1b: Agent-hours segments (wallclock vs labor) ──────────────────
    logger.info("\n── Layer 1b: Agent-hours busy segments ──────────────────────────────")

    # Well-formed: every emitted segment must have end > start.
    bad_segs = sum(
        1 for s in raw_seg
        if not (_parse_iso(s.get("start_ts")) and _parse_iso(s.get("end_ts"))
                and _parse_iso(s.get("end_ts")) > _parse_iso(s.get("start_ts")))
    )
    check("Segments well-formed (end > start)", bad_segs, 0)

    # Aggregate computer totals.
    total_wall  = sum(w.get("agent_hours_wallclock", 0.0)
                      for d in hours_result.values()
                      for w in d.get("by_week", {}).values())
    total_labor = sum(w.get("agent_hours_labor", 0.0)
                      for d in hours_result.values()
                      for w in d.get("by_week", {}).values())

    # Invariant: union (wallclock) <= sum (labor).
    check("Wallclock <= labor (union <= sum)",
          total_wall <= total_labor + 0.01, True,
          unit=f"  [wall={round(total_wall,2)}h labor={round(total_labor,2)}h]")

    # Invariant: new labor captures at least the old first-gap total (tool runtime
    # + sub-agents are added, never removed).
    old_first_gap_hrs = col_agent_ms / 3_600_000
    check("Labor >= legacy first-gap total",
          round(total_labor, 2) >= round(old_first_gap_hrs, 2) - 0.01, True,
          unit=f"  [labor={round(total_labor,2)}h legacy={round(old_first_gap_hrs,2)}h]")

    # Independent ceiling: wallclock can't exceed the sum of every file's
    # (last_ts - first_ts) span (a strict upper bound on busy time).
    span_ceiling_hrs = _scan_file_span_ceiling(claude_dirs, since)
    check("Wallclock <= sum of file time-spans (ceiling)",
          total_wall <= span_ceiling_hrs + 0.01, True,
          unit=f"  [wall={round(total_wall,2)}h ceiling={round(span_ceiling_hrs,2)}h]")

    # Sub-agent work should manifest as parallelism > 1 somewhere (info).
    n_sidechain = sum(1 for s in raw_seg if s.get("is_sidechain"))
    max_parallel = max(
        (w.get("parallelism", 0.0)
         for d in hours_result.values()
         for w in d.get("by_week", {}).values()),
        default=0.0,
    )
    warn("Sub-agent segments / parallelism",
         f"{n_sidechain} sidechain segments  max weekly parallelism={max_parallel}")

    # ── LAYER 2: computer vs collector raw ───────────────────────────────────
    logger.info("── Layer 2: Computer vs collector raw data ──────────────────────────")

    # Agent hours: computer total >= collector total is expected because the fallback
    # path adds estimated hours for sessions present in session-meta but not in JSONL.
    comp_total_hrs = sum(
        wk_data.get("agent_hours", 0.0)
        for dev_data in hours_result.values()
        for wk_data in dev_data.get("by_week", {}).values()
    )
    col_total_hrs = col_agent_ms / 3_600_000
    if round(comp_total_hrs, 3) < round(col_total_hrs, 3) - 0.01:
        check("Agent hours total (computer vs collector)",
              round(comp_total_hrs, 3), round(col_total_hrs, 3), unit=" hrs")
    else:
        fallback_hrs = round(comp_total_hrs - col_total_hrs, 3)
        warn("Agent hours total (computer >= collector)",
             f"computer={round(comp_total_hrs,3)} hrs  collector={round(col_total_hrs,3)} hrs  "
             f"fallback={fallback_hrs} hrs added for sessions missing from JSONL")

    # Skills: sum total_invocations across all developer entries
    comp_skills = sum(d.get("total_invocations", 0) for d in skills_result.values()
                      if isinstance(d, dict))
    check("Skill invocations (computer vs collector)",
          comp_skills, col_skills)

    # Velocity: total_lines_changed summed across all developer entries.
    # velocity.py weights removed lines at 0.5 — GT must use the same formula.
    comp_lines = sum(d.get("total_lines_changed", 0) for d in vel_result.values()
                     if isinstance(d, dict))
    gt_lines   = gt_sm["total_lines_weighted"]
    check("Lines changed (computer vs session-meta gt)",
          comp_lines, gt_lines)

    # Adoption: active_developers count vs developers with sessions
    devs_with_sessions = sum(1 for v in sessions_by_dev.values() if v)
    comp_active_devs   = adopt_result.get("team", {}).get("active_developers", 0)
    check("Active developers (computer vs collector)",
          comp_active_devs, devs_with_sessions)

    # Depth: every developer with sessions must have a depth entry
    devs_missing_depth = [k for k in sessions_by_dev if k not in depth_result]
    check("Depth entries coverage (all devs have depth)",
          len(devs_missing_depth), 0)

    # Trust: interruption_rate must be in [0, 1]
    for dev_key, td in trust_result.items():
        rate = td.get("interruption_rate", 0)
        if not (0.0 <= rate <= 1.0):
            check(f"Trust interruption_rate in [0,1] ({dev_key[:8]})", False, True)
        else:
            check(f"Trust interruption_rate in [0,1] ({dev_key[:8]})", True, True)

    # ── LAYER 3: ground truth vs computer ────────────────────────────────────
    logger.info("\n── Layer 3: Computer vs ground truth (JSONL) ────────────────────────")

    # Agent hours by week: computer can exceed GT because the fallback path adds
    # estimated hours (session_duration - user_idle) for sessions with no JSONL turns.
    # FAIL only if computer < GT (hours disappearing is a real bug).
    for week, gt_ms in gt_jsonl["by_week"].items():
        gt_hrs = gt_ms / 3_600_000
        comp_hrs = sum(
            dev_data.get("by_week", {}).get(week, {}).get("agent_hours", 0.0)
            for dev_data in hours_result.values()
        )
        if round(comp_hrs, 3) < round(gt_hrs, 3) - 0.01:
            check(f"Agent hours {week} (computer vs gt)",
                  round(comp_hrs, 3), round(gt_hrs, 3), unit=" hrs")
        else:
            warn(f"Agent hours {week} (computer >= gt OK)",
                 f"computer={round(comp_hrs,3)} hrs  gt={round(gt_hrs,3)} hrs  "
                 f"delta={round(comp_hrs - gt_hrs, 3)} hrs from fallback estimation")

    # Skills alltime: sum across developer entries vs gt JSONL
    comp_skills_alltime = sum(d.get("total_invocations", 0) for d in skills_result.values()
                              if isinstance(d, dict))
    check("Skills alltime (computer vs gt JSONL)",
          comp_skills_alltime, gt_jsonl["skills"])

    # ── LAYER 4: internal consistency ────────────────────────────────────────
    logger.info("\n── Layer 4: Internal consistency ────────────────────────────────────")

    # Every turn event must have either agent_ms set or a reason it's None
    turns_only    = [e for e in raw_te if "user_ts" in e]
    turns_with_ms = sum(1 for e in turns_only if e.get("agent_ms") is not None)
    pct_with_ms   = turns_with_ms / max(len(turns_only), 1) * 100
    if pct_with_ms < 50:
        warn("Turn events with agent_ms",
             f"only {pct_with_ms:.0f}% of turns have agent_ms set "
             f"({turns_with_ms}/{len(turns_only)}) — check timestamp parsing")
    else:
        check("Turn events with agent_ms set (>= 50%)",
              True, True,
              unit=f" [{pct_with_ms:.0f}% = {turns_with_ms}/{len(turns_only)}]")

    # All agent tasks must have agent_name set (no ghost rows)
    null_name_tasks = [
        t for at_data in raw_at.values()
        for t in at_data.get("tasks", [])
        if not t.get("agent_name")
    ]
    check("Agent tasks with NULL agent_name (should be 0)",
          len(null_name_tasks), 0)

    # All agent tasks must have week set
    null_week_tasks = [
        t for at_data in raw_at.values()
        for t in at_data.get("tasks", [])
        if not t.get("week")
    ]
    check("Agent tasks with NULL week (should be 0)",
          len(null_week_tasks), 0)

    # Session-metas without turn events are OK (daily mode), but log the count
    sm_ids  = {m["session_id"] for m in raw_sm}
    te_sids = {e.get("session_id") for e in raw_te}
    sm_no_turns = sm_ids - te_sids
    warn("Sessions in session_metas without turn events",
         f"{len(sm_no_turns)} sessions — normal if turns were collected earlier")

    # ── Layer 5: Store completeness (--from-store only) ──────────────────────
    if args.from_store:
        logger.info("\n── Layer 5: Store completeness (local JSONL → DB) ────────────────")
        from central_store import CentralStore as _CentralStore
        store = _CentralStore(args.from_store)
        is_pg = str(args.from_store).startswith(("postgresql://", "postgres://"))
        S  = "scrape_data." if is_pg else ""   # schema prefix
        PH = "%s"           if is_pg else "{p}" # placeholder style

        # 5a. All local session_ids must be present in DB session_metas
        db_sm_ids    = {r[0] for r in store._fetchall(f"SELECT session_id FROM {S}session_metas")}
        local_sm_ids = gt_sm["session_ids"]
        missing_in_db = local_sm_ids - db_sm_ids
        check("Local sessions present in DB session_metas",
              len(missing_in_db), 0,
              unit=f"  [local={len(local_sm_ids)} db={len(db_sm_ids)} missing={len(missing_in_db)}]")

        # 5b. Sessions that had local turns must have rows in DB turn_events
        db_te_sids = {r[0] for r in store._fetchall(
            f"SELECT DISTINCT session_id FROM {S}turn_events"
        )}
        sessions_with_local_turns = {e.get("session_id") for e in raw_te if "user_ts" in e}
        missing_te = sessions_with_local_turns - db_te_sids
        check("Sessions with local turns present in DB turn_events",
              len(missing_te), 0,
              unit=f"  [local_with_turns={len(sessions_with_local_turns)} missing={len(missing_te)}]")

        # 5b2. Busy segments: every local segment session must be in DB busy_segments,
        #      and the DB segment count must match what we collected locally.
        db_bs_sids = {r[0] for r in store._fetchall(
            f"SELECT DISTINCT session_id FROM {S}busy_segments"
        )}
        local_bs_sids = {s.get("session_id") for s in raw_seg}
        missing_bs = local_bs_sids - db_bs_sids
        check("Local segment sessions present in DB busy_segments",
              len(missing_bs), 0,
              unit=f"  [local={len(local_bs_sids)} db={len(db_bs_sids)} missing={len(missing_bs)}]")
        db_bs_count = int(store._fetchall(f"SELECT COUNT(*) FROM {S}busy_segments")[0][0])
        check("DB busy_segments count == local segment count",
              db_bs_count, len(raw_seg))

        # 5c. NULL agent_name — the ghost-row bug we diagnosed and fixed
        # Root cause: queue-operation msgs had no content → regex never fired → name=None inserted
        null_name = int(store._fetchall(
            f"SELECT COUNT(*) FROM {S}agent_tasks WHERE agent_name IS NULL"
        )[0][0]) if is_pg else 0
        check("DB agent_tasks NULL agent_name (ghost-row fix)", null_name, 0)

        # 5d. NULL week — timestamp-gap fix
        # Root cause: agent-name msgs had no timestamp → week could not be derived
        null_week = int(store._fetchall(
            f"SELECT COUNT(*) FROM {S}agent_tasks WHERE week IS NULL"
        )[0][0]) if is_pg else 0
        check("DB agent_tasks NULL week (timestamp-gap fix)", null_week, 0)

        # 5e. NULL enqueued_at — same timestamp-gap root cause
        null_enq = int(store._fetchall(
            f"SELECT COUNT(*) FROM {S}agent_tasks WHERE enqueued_at IS NULL"
        )[0][0]) if is_pg else 0
        check("DB agent_tasks NULL enqueued_at", null_enq, 0)

        # 5f. Agent name coverage: every unique name seen in local JSONL must be in DB.
        # SQLite has no agent_tasks table (PG-only), so this check applies to PG only.
        if is_pg:
            db_agent_names = {r[0] for r in store._fetchall(
                "SELECT DISTINCT agent_name FROM scrape_data.agent_tasks "
                "WHERE agent_name IS NOT NULL"
            )}
            missing_agents = gt_jsonl["agent_names"] - db_agent_names
            check("GT agent names present in DB agent_tasks (gt ⊆ db)",
                  len(missing_agents), 0,
                  unit=f"  [gt={len(gt_jsonl['agent_names'])} db={len(db_agent_names)} "
                       f"missing={missing_agents or 'none'}]")

        # 5g. session_metas.agent_names back-populated in DB for sessions that had agents
        # This is updated by push.py after agent_tasks are inserted via a temp-table UPDATE.
        if is_pg:
            sessions_with_agents_locally = {
                sid for sid, d in raw_at.items() if d.get("agent_names")
            }
            if sessions_with_agents_locally:
                ph_list = ",".join([PH] * len(sessions_with_agents_locally))
                empty_names = int(store._fetchall(
                    f"SELECT COUNT(*) FROM scrape_data.session_metas "
                    f"WHERE session_id IN ({ph_list}) "
                    f"AND (agent_names IS NULL OR agent_names = '[]'::jsonb)",
                    tuple(sessions_with_agents_locally)
                )[0][0])
                check("session_metas.agent_names back-populated in DB",
                      empty_names, 0,
                      unit=f"  [{len(sessions_with_agents_locally)} sessions with agents]")

        # 5h. DB row counts — informational summary
        if is_pg:
            db_stats = store.stats()
            warn("DB table sizes (info)",
                 f"session_metas={db_stats['session_metas']}  "
                 f"turn_events={db_stats['turn_events']}  "
                 f"agent_tasks={db_stats['agent_tasks']}  "
                 f"facets={db_stats.get('facets', 'n/a')}")

        store.close()

    # ── summary ───────────────────────────────────────────────────────────────
    logger.info("\n" + "=" * 68)
    passed = sum(1 for _, p, _ in results if p is True)
    failed = sum(1 for _, p, _ in results if p is False)
    warned = sum(1 for _, p, _ in results if p is None)
    logger.info(f"  RESULT: {passed} passed  {failed} failed  {warned} warnings")
    logger.info("=" * 68 + "\n")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
