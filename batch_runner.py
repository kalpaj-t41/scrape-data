#!/usr/bin/env python3
"""
Batch runner — orchestrates full metric computation pipeline.

Single-machine mode (default):
  python batch_runner.py --since 7d --report

Distributed mode — read from central store instead of local files:
  python batch_runner.py --from-store /shared/central.db --since 7d --report

All-weeks mode (score every week found in the data):
  python batch_runner.py --from-store $POSTGRES_URL --since 90d --all-weeks --report

Other options:
  --output /tmp/out.json   custom output path
  --team-size 15           total developer count for adoption %
  --week 2026-W25          score a specific ISO week
  --daily                  fast daily update (no JSONL parsing, single-machine only)
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from collectors import discover, session_meta, session_index, sessions, facets, app_state, plans, plugins, settings, agent_tasks
from computers import registry, ComputeContext
from metrics_store import MetricsStore
from central_store import CentralStore

logger = logging.getLogger(__name__)


def _parse_since(since_str: str) -> datetime:
    since_str = since_str.strip()
    if since_str.endswith("d"):
        days = int(since_str[:-1])
        return datetime.now(tz=timezone.utc) - timedelta(days=days)
    return datetime.fromisoformat(since_str).replace(tzinfo=timezone.utc)


def _current_week() -> str:
    now = datetime.now(tz=timezone.utc)
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _collect(developer_map: list[dict], since: datetime, store: MetricsStore, daily_only: bool) -> dict:
    """Collect all raw data in one pass. Every computer draws from this dict."""
    since_mtime = store.last_run_dt("weekly")
    since_mtime = since_mtime.timestamp() if since_mtime else None

    logger.info("[batch] Collecting session metadata...")
    raw_session_metas = session_meta.collect(developer_map, since=since)
    logger.info(f"[batch]   {len(raw_session_metas)} sessions in period")

    logger.info("[batch] Collecting facets, app state, plans, plugins, settings...")
    raw_facets    = facets.collect(developer_map)
    raw_app_state = app_state.collect(developer_map)
    raw_plans     = plans.collect(developer_map, since_mtime=since_mtime)
    plugins.collect(developer_map)
    settings.collect(developer_map)

    raw_turn_events: list[dict] = []
    raw_busy_segments: list[dict] = []
    raw_segment_signals: list[dict] = []
    raw_agent_tasks: dict = {}
    if not daily_only:
        logger.info("[batch] Parsing session transcripts (JSONL)...")
        processed = store.processed_sessions()
        raw_turn_events = sessions.collect(developer_map, processed_sessions=processed, since=since)
        raw_busy_segments = sessions.collect_segments(developer_map, since=since)
        raw_segment_signals = sessions.collect_segment_signals(developer_map, since=since)
        raw_agent_tasks = agent_tasks.collect(developer_map, processed_sessions=processed, since=since)
        logger.info(f"[batch]   {len(raw_turn_events)} turn events, "
                    f"{len(raw_busy_segments)} busy segments, "
                    f"{sum(len(v.get('tasks',[])) for v in raw_agent_tasks.values())} agent tasks extracted")
        store.mark_sessions_processed(list({e["session_id"] for e in raw_turn_events}))

        # JSONL is the source of truth (usage-data has clear coverage gaps). Telemetry
        # only fills orphan sessions + a few fields JSONL can't derive (user_interruptions).
        jsonl_sessions = session_index.collect(developer_map, since=since)
        tele = len(raw_session_metas)
        raw_session_metas = session_index.merge_jsonl_primary(jsonl_sessions, raw_session_metas)
        orphans = sum(1 for m in raw_session_metas if m.get("source") == "telemetry")
        logger.info(f"[batch]   session universe: {len(jsonl_sessions)} JSONL (primary) + "
                    f"{orphans} telemetry-only orphans = {len(raw_session_metas)} "
                    f"(telemetry had {tele})")

    return {
        "session_metas":   raw_session_metas,
        "turn_events":     raw_turn_events,
        "busy_segments":   raw_busy_segments,
        "segment_signals": raw_segment_signals,
        "facets":          raw_facets,
        "app_state":     raw_app_state,
        "plans":         raw_plans,
        "agent_tasks":   raw_agent_tasks,
    }


def _compute(raw: dict, team_size: int | None, week: str, store: MetricsStore,
             dev_name_map: dict | None = None) -> ComputeContext:
    """
    Build the ComputeContext and run all metric-phase computers once.

    Indexes are built here — one pass per source list — so no computer
    re-iterates the full session_metas or turn_events list on its own.
    Score-phase computers (composite, equity) run later, per week, in _score_week.
    """
    from collections import defaultdict

    sm = raw["session_metas"]
    te = raw["turn_events"]

    # ── One pass over session_metas ───────────────────────────────────────
    sessions_by_dev: dict = defaultdict(list)
    meta_by_sid: dict = {}
    for m in sm:
        sessions_by_dev[m["developer_key"]].append(m)
        meta_by_sid[m["session_id"]] = m

    # ── One pass over turn_events ─────────────────────────────────────────
    turns_by_session: dict = defaultdict(list)
    skill_events: list = []
    for t in te:
        turns_by_session[t.get("session_id", "")].append(t)
        if t.get("event_type") == "skill":
            skill_events.append(t)

    ctx = ComputeContext(
        sessions_by_dev  = dict(sessions_by_dev),
        meta_by_sid      = meta_by_sid,
        turns_by_session = dict(turns_by_session),
        skill_events     = skill_events,
        busy_segments    = raw.get("busy_segments") or [],
        segment_signals  = raw.get("segment_signals") or [],
        turn_events      = te,
        facets           = raw["facets"],
        plans            = raw["plans"],
        app_state        = raw["app_state"],
        agent_tasks      = raw.get("agent_tasks", {}),
        team_size        = team_size,
        week             = week,
        weekly_history   = store.read_weekly_history(),
        dev_name_map     = dev_name_map or {},
    )

    # Registry topo-orders the metric computers (e.g. velocity after agent_hours)
    # and stores each result in ctx.results.
    registry.run_metrics(ctx)
    return ctx


def _all_weeks_in_data(raw: dict) -> list[str]:
    """Return sorted distinct ISO weeks across every dated source in `raw`.

    Spans session_metas (start_time), busy_segments (start_ts), and turn_events
    (user_ts / assistant_ts). Deriving from session_metas alone dropped weeks
    that had JSONL segments but no session-meta file on this machine.
    """
    weeks: set[str] = set()

    def _add(ts) -> None:
        if not ts:
            return
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            iso = dt.isocalendar()
            weeks.add(f"{iso.year}-W{iso.week:02d}")
        except Exception:
            pass

    for s in raw.get("session_metas") or []:
        _add(s.get("start_time"))
    for seg in raw.get("busy_segments") or []:
        _add(seg.get("start_ts"))
    for e in raw.get("turn_events") or []:
        _add(e.get("user_ts") or e.get("assistant_ts") or e.get("ts"))

    return sorted(weeks)


def _pull_raw(since: datetime, store: MetricsStore, daily_only: bool, central_db) -> tuple[dict, dict]:
    """Return (raw, dev_name_map). Shared by run() and run_all_weeks()."""
    if central_db:
        logger.info(f"[batch] Reading from central store: {central_db}")
        logger.info(f"[batch] Period: since {since.date().isoformat()}")
        cs = CentralStore(central_db)
        raw = cs.pull_raw(since=since)
        dev_name_map = cs.developer_names()
        cs.close()
        logger.info(f"[batch]   {len(raw['session_metas'])} sessions, "
                    f"{len(raw['turn_events'])} turn events from store")
        return raw, dev_name_map
    else:
        logger.info(f"[batch] Starting {'daily' if daily_only else 'weekly'} run")
        logger.info(f"[batch] Period: since {since.date().isoformat()}")
        developer_map = discover.build_developer_map()
        logger.info(f"[batch]   Found {len(developer_map)} developer(s)")
        raw = _collect(developer_map, since, store, daily_only)
        dev_name_map = {d["developer_key"]: d.get("name") or d["developer_key"][:12]
                        for d in developer_map}
        return raw, dev_name_map


def _score_week(week: str, ctx: ComputeContext, store: MetricsStore) -> dict:
    """Compute per-developer and team composite scores for one ISO week."""
    ctx.week = week
    # Score-phase computers (composite → equity); topo-ordered by the registry.
    registry.run_scores(ctx)

    developer_scores = ctx.get("composite")
    equity_data      = ctx.get("equity")

    composite_c   = registry.get("composite")
    agent_hours_c = registry.get("agent_hours")
    velocity_c    = registry.get("velocity")
    skills_c      = registry.get("skills")
    efficiency_c  = registry.get("efficiency")
    usefulness_c  = registry.get("usefulness")

    team_score = composite_c.team_composite(developer_scores, equity_data=equity_data)

    # Surface the quality layer (U4/U5/U12) for visibility. These do NOT feed the
    # composite score yet — that fold-in is QAAH (U6). Here they ride alongside.
    eff_results = ctx.get("efficiency")
    use_results = ctx.get("usefulness")
    for d in developer_scores:
        k = d["developer_key"]
        ew = eff_results.get(k, {}).get("by_week", {}).get(week, {})
        uw = use_results.get(k, {}).get("by_week", {}).get(week, {})
        d["efficiency"]          = ew.get("efficiency")
        d["usefulness"]          = uw.get("usefulness_base")
        d["usefulness_coverage"] = uw.get("coverage_band")

    return {
        "week": week,
        "team": {
            **team_score,
            "adoption":    ctx.get("adoption")["team"],
            "agent_hours": agent_hours_c.team_summary(ctx.get("agent_hours"), ctx),
            "velocity":    velocity_c.team_summary(ctx.get("velocity"), ctx),
            "skills":      skills_c.team_summary(ctx.get("skills"), ctx),
            "efficiency":  efficiency_c.team_summary(eff_results, ctx),
            "usefulness":  usefulness_c.team_summary(use_results, ctx),
            "agent_quality": ctx.get("agent_quality"),
        },
        "developers": sorted(developer_scores, key=lambda d: d["ai_native_score"], reverse=True),
    }


def run(
    since: datetime,
    team_size: int | None,
    target_week: str | None,
    output_path: Path | None,
    daily_only: bool,
    store: MetricsStore,
    central_db: Path | None = None,
) -> dict:
    raw, dev_name_map = _pull_raw(since, store, daily_only, central_db)
    if target_week:
        week = target_week
    else:
        weeks_found = _all_weeks_in_data(raw)
        week = weeks_found[-1] if weeks_found else _current_week()
        logger.info(f"[batch] No --week specified; using most recent week with data: {week}")

    ctx = _compute(raw, team_size, week, store, dev_name_map)
    weekly_payload = _score_week(week, ctx, store)

    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "period_since": since.isoformat(),
        **weekly_payload,
        "raw": {
            "session_count": len(raw["session_metas"]),
            "turn_events":   len(raw["turn_events"]),
            "facet_count":   len(raw["facets"]),
        },
    }

    out_path = store.write_output(payload, output_path)
    store.append_weekly_snapshot({"week": week, "team_score": payload["team"].get("team_ai_native_score")})
    store.mark_run_complete("daily" if daily_only else "weekly")

    logger.info(f"[batch] Done. Output → {out_path}")
    logger.info(f"[batch] Team AI Native Score: {payload['team'].get('team_ai_native_score')} ({payload['team'].get('label')})")
    return payload


def run_all_weeks(
    since: datetime,
    team_size: int | None,
    output_path: Path | None,
    store: MetricsStore,
    central_db: Path | None = None,
) -> list[dict]:
    """Compute metrics for every distinct week present in the pulled data."""
    raw, dev_name_map = _pull_raw(since, store, daily_only=False, central_db=central_db)

    weeks = _all_weeks_in_data(raw)
    if not weeks:
        logger.info("[batch] No sessions found in the specified period.")
        return []

    logger.info(f"[batch] Found {len(weeks)} week(s): {', '.join(weeks)}")

    # Run metric computers once — they produce by_week breakdowns internally
    ctx = _compute(raw, team_size, weeks[-1], store, dev_name_map)

    week_payloads: list[dict] = []
    for week in weeks:
        wp = _score_week(week, ctx, store)
        week_payloads.append(wp)
        store.append_weekly_snapshot({"week": week, "team_score": wp["team"].get("team_ai_native_score")})
        logger.info(f"[batch]   {week}: score {wp['team'].get('team_ai_native_score')} ({wp['team'].get('label')})")

    full_output = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "period_since": since.isoformat(),
        "weeks": week_payloads,
        "raw": {
            "session_count": len(raw["session_metas"]),
            "turn_events":   len(raw["turn_events"]),
            "facet_count":   len(raw["facets"]),
        },
    }

    out_path = store.write_output(full_output, output_path)
    store.mark_run_complete("weekly")
    logger.info(f"[batch] Done. Output → {out_path}")
    return week_payloads


def print_report(payload: dict) -> None:
    team = payload.get("team", {})
    devs = payload.get("developers", [])
    week = payload.get("week", "")
    aq = team.get("agent_quality", {}) or {}

    logger.info("")
    logger.info("=" * 62)
    logger.info(f"  AI NATIVENESS REPORT — {week}")
    logger.info("=" * 62)
    logger.info(f"  AI Native Score     {team.get('team_ai_native_score'):>6}  {team.get('label','')}")
    logger.info(f"  Team Adoption       {team.get('adoption',{}).get('adoption_index',0):>5}%  ({team.get('adoption',{}).get('active_developers','?')}/{team.get('adoption',{}).get('total_developers','?')} devs active)")
    logger.info(f"  Avg Agent Hours/Dev {team.get('agent_hours',{}).get('avg_agent_hours',0):>6.1f}  (target: 80 hrs/week)")
    logger.info(f"  Team Velocity       {team.get('velocity',{}).get('team_velocity',0):>6.0f}  lines / agent hour")
    logger.info(f"  Skill Invocations   {team.get('skills',{}).get('total_invocations',0):>6}")
    _eff = team.get("efficiency", {}) or {}
    _use = team.get("usefulness", {}) or {}
    logger.info(f"  Avg Efficiency      {(_eff.get('avg_efficiency') or 0):>6.2f}  (1.0 = no time lost to failed/retried calls)")
    logger.info(f"  Avg Usefulness      {(_use.get('avg_usefulness') or 0):>6.2f}  (coverage {(_use.get('avg_coverage') or 0):.2f})  [quality layer — not yet in the score]")
    logger.info("")
    logger.info(f"  {'Developer':<18} {'Team (org/project)':<24} {'Score':>6} {'AgentHrs':>9} {'Eff':>5} {'Use':>5} {'Status':<13}")
    logger.info("  " + "-" * 92)
    for d in devs:
        flag = " ← needs attention" if d.get("agent_hours_status") == "stuck" else ""
        name = (str(d.get("name") or "unknown"))[:17]
        dteam = (str(d.get("team") or "unknown"))[:23]
        eff_v = d.get("efficiency"); use_v = d.get("usefulness")
        logger.info(
            f"  {name:<18} {dteam:<24} "
            f"{d['ai_native_score']:>6.1f} {d.get('agent_hours_week',0):>8.1f}h "
            f"{(eff_v if eff_v is not None else 0):>5.2f} {(use_v if use_v is not None else 0):>5.2f} "
            f"{d.get('agent_hours_status',''):<13}{flag}"
        )

    by_type = aq.get("by_agent_type", {})
    if by_type:
        logger.info("")
        logger.info(f"  Agent quality by type  ({len(aq.get('agents',{}))} agents, {len(aq.get('by_workflow_run',{}))} workflow runs)")
        logger.info(f"    {'Type':<20} {'n':>3} {'Eff':>6} {'Use':>6} {'BusyHrs':>8}")
        for t, v in sorted(by_type.items(), key=lambda x: -(x[1].get("n_agents") or 0)):
            logger.info(
                f"    {str(t)[:19]:<20} {v.get('n_agents',0):>3} "
                f"{(v.get('avg_efficiency') or 0):>6.2f} {(v.get('avg_usefulness') or 0):>6.2f} "
                f"{(v.get('total_busy_hours') or 0):>8.2f}"
            )
    logger.info("=" * 62)
    logger.info("")


def print_all_weeks_report(week_payloads: list[dict]) -> None:
    if not week_payloads:
        logger.info("\n[batch] No data to report.\n")
        return

    logger.info("")
    logger.info("=" * 62)
    logger.info("  AI NATIVENESS REPORT — ALL WEEKS")
    logger.info("=" * 62)
    logger.info(f"  {'Week':<12} {'Score':>6}  {'Label':<15} {'AvgAgentHrs':>12}")
    logger.info("  " + "-" * 50)
    for wp in week_payloads:
        team = wp.get("team", {})
        logger.info(
            f"  {wp.get('week',''):<12} "
            f"{team.get('team_ai_native_score', 0.0):>6.1f}  "
            f"{team.get('label', ''):<15} "
            f"{team.get('agent_hours', {}).get('avg_agent_hours', 0.0):>11.1f}h"
        )
    logger.info("=" * 62)
    logger.info("")

    for wp in week_payloads:
        print_report(wp)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="AI Nativeness Metrics Batch Runner")
    parser.add_argument("--since", default=None,
                        help="Period to analyse, e.g. 7d, 30d (default: 7d; 365d when --all-weeks)")
    parser.add_argument("--week", default=None, help="ISO week to score, e.g. 2026-W25")
    parser.add_argument("--all-weeks", action="store_true",
                        help="Score every week found in the data (overrides --week)")
    parser.add_argument("--team-size", type=int, default=None, help="Total developer count for adoption pct")
    parser.add_argument("--output", type=Path, default=None, help="Custom output JSON path")
    parser.add_argument("--store-dir", type=Path, default=None, help="Metrics store directory")
    parser.add_argument("--daily", action="store_true", help="Fast daily run (skip JSONL parsing, single-machine only)")
    parser.add_argument("--report", action="store_true", help="Print human-readable report after run")
    parser.add_argument("--from-store", default=None, metavar="DB_PATH_OR_URL",
                        help="Read from central store: SQLite path or PostgreSQL URL. "
                             "Also reads POSTGRES_URL env var.")
    args = parser.parse_args()

    import os
    default_since = "365d" if args.all_weeks else "7d"
    since      = _parse_since(args.since or default_since)
    store      = MetricsStore(store_dir=args.store_dir)
    central_db = args.from_store or os.environ.get("POSTGRES_URL")

    if args.all_weeks:
        week_payloads = run_all_weeks(
            since=since,
            team_size=args.team_size,
            output_path=args.output,
            store=store,
            central_db=central_db,
        )
        if args.report:
            print_all_weeks_report(week_payloads)
    else:
        payload = run(
            since=since,
            team_size=args.team_size,
            target_week=args.week,
            output_path=args.output,
            daily_only=args.daily,
            store=store,
            central_db=central_db,
        )
        if args.report:
            print_report(payload)


if __name__ == "__main__":
    main()
