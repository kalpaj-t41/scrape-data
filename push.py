#!/usr/bin/env python3
"""
Per-machine push script.

Runs on each developer's machine. Collects raw data from local ~/.claude* dirs
and pushes it to the central store using bulk inserts. Does NOT compute metrics.

Usage:
  python push.py --central postgresql://user:pass@host:5432/db
  python push.py --central postgresql://user:pass@host:5432/db --since 90d
  python push.py --central postgresql://user:pass@host:5432/db --force
  python push.py --central postgresql://user:pass@host:5432/db --dry-run
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from collectors import discover, session_meta, session_index, sessions, facets, app_state, plans, plugins, settings, agent_tasks
from central_store import CentralStore

logger = logging.getLogger(__name__)


def _parse_since(s: str) -> datetime:
    s = s.strip()
    if s.endswith("d"):
        return datetime.now(tz=timezone.utc) - timedelta(days=int(s[:-1]))
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def push(central_db, since: datetime, dry_run: bool, force: bool) -> None:
    store = CentralStore(central_db)

    stats_before = store.stats()
    logger.info(f"[push] Connecting to central store")
    logger.info(f"[push] Period  : since {since.date().isoformat()}")
    logger.info(f"[push] Mode    : {'FORCE (ignoring existing)' if force else 'incremental'}")
    logger.info(f"[push] DB state before push:")
    for k, v in stats_before.items():
        if k != "backend":
            logger.info(f"         {k:<20} {v:>6} rows")

    # Discover local .claude* dirs and register developers
    developer_map = discover.build_developer_map()
    dev_dirs = [d for dev in developer_map for d in dev["claude_dirs"]]
    logger.info(f"\n[push] Found {len(developer_map)} developer(s) across {len(dev_dirs)} account(s):")
    for dev in developer_map:
        logger.info(f"         {dev.get('name') or 'unknown'} <{dev.get('email') or 'no email'}>")
        for d in dev["claude_dirs"]:
            logger.info(f"           {d}")

    if not dry_run:
        store.upsert_developers(developer_map)

    # Determine which sessions to skip
    already_sm  = set() if force else store.pushed_session_ids()
    already_te  = set() if force else store.pushed_turn_session_ids()
    already_at  = set() if force else store.pushed_agent_session_ids()

    logger.info(f"\n[push] Skipping: {len(already_sm)} session_metas, "
                f"{len(already_te)} turn-event sessions, "
                f"{len(already_at)} agent-task sessions already in store")

    # ── Collect ──────────────────────────────────────────────────────────────
    logger.info("\n[push] Collecting session metadata...")
    raw_session_metas = session_meta.collect(developer_map, since=since)
    # JSONL is the source of truth (usage-data has coverage gaps); telemetry only fills
    # orphan sessions + fields JSONL can't derive. Re-push with --force to refresh the
    # sessions already stored under the old telemetry-primary scheme.
    jsonl_sessions = session_index.collect(developer_map, since=since)
    union_metas = session_index.merge_jsonl_primary(jsonl_sessions, raw_session_metas)
    # Always upsert all sessions within the --since window: web flags, agent_names, and
    # other derived fields can change as sub-agents run after the first push.
    # Sessions outside the window are not collected (file mtime filtered), so this is safe.
    new_metas = union_metas if since else [m for m in union_metas if m["session_id"] not in already_sm]
    truly_new = sum(1 for m in new_metas if m["session_id"] not in already_sm)
    logger.info(f"         {len(jsonl_sessions)} JSONL (primary), {len(raw_session_metas)} telemetry, "
                f"{len(union_metas)} union, {truly_new} new, {len(new_metas) - truly_new} refreshed")

    logger.info("[push] Parsing JSONL transcripts...")
    raw_turn_events = sessions.collect(
        developer_map,
        processed_sessions=already_te,
        since=since,
    )
    new_te_sessions = len({e["session_id"] for e in raw_turn_events})
    logger.info(f"         {len(raw_turn_events)} turn events across {new_te_sessions} new sessions")

    logger.info("[push] Building busy segments (accurate agent hours)...")
    raw_busy_segments = sessions.collect_segments(developer_map, since=since)
    seg_sessions = len({s["session_id"] for s in raw_busy_segments})
    logger.info(f"         {len(raw_busy_segments)} segments across {seg_sessions} sessions")

    logger.info("[push] Extracting per-segment quality signals (efficiency/usefulness)...")
    raw_segment_signals = sessions.collect_segment_signals(developer_map, since=since)
    sig_sessions = len({s["session_id"] for s in raw_segment_signals})
    logger.info(f"         {len(raw_segment_signals)} signal segments across {sig_sessions} sessions")

    logger.info("[push] Collecting facets, app state, plans, agent tasks...")
    raw_facets      = facets.collect(developer_map)
    raw_app_state   = app_state.collect(developer_map)
    raw_plans       = plans.collect(developer_map)
    raw_agent_tasks = agent_tasks.collect(developer_map, processed_sessions=already_at, since=since)
    plugins.collect(developer_map)
    settings.collect(developer_map)

    task_count = sum(len(v.get("tasks", [])) for v in raw_agent_tasks.values())
    logger.info(f"         {len(raw_facets)} facets, {len(raw_app_state)} app states, "
                f"{len(raw_agent_tasks)} agent sessions ({task_count} tasks)")

    raw = {
        "session_metas":   new_metas,
        "turn_events":     raw_turn_events,
        "busy_segments":   raw_busy_segments,
        "segment_signals": raw_segment_signals,
        "facets":          raw_facets,
        "app_state":       raw_app_state,
        "plans":           raw_plans,
        "agent_tasks":     raw_agent_tasks,
    }

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info("\n[push] To be pushed:")
    logger.info(f"         session_metas  : {len(new_metas)}")
    logger.info(f"         turn_events    : {len(raw_turn_events)}")
    logger.info(f"         busy_segments  : {len(raw_busy_segments)}")
    logger.info(f"         segment_signals: {len(raw_segment_signals)}")
    logger.info(f"         facets         : {len(raw_facets)}")
    logger.info(f"         app_state      : {len(raw_app_state)}")
    logger.info(f"         plans          : {len(raw_plans)}")
    logger.info(f"         agent_tasks    : {task_count}")

    if dry_run:
        logger.info("\n[push] DRY RUN — nothing written.")
        store.close()
        return

    # ── Push ─────────────────────────────────────────────────────────────────
    inserted = store.push(raw, force=force)
    stats_after = store.stats()
    store.close()

    logger.info("\n[push] Done.")
    logger.info(f"  {'Table':<22} {'Before':>8} {'Upserted':>10} {'After':>8}")
    logger.info("  " + "-" * 52)
    tables = ["session_metas", "turn_events", "busy_segments", "segment_signals", "facets", "app_state", "plans", "agent_tasks"]
    for k in tables:
        before = stats_before.get(k, 0)
        upserted = inserted.get(k, 0)
        after = stats_after.get(k, before)
        logger.info(f"  {k:<22} {before:>8} {upserted:>10} {after:>8}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Push local Claude data to central store")
    parser.add_argument("--central", default=None,
                        help="SQLite path or PostgreSQL URL. "
                             "Defaults to POSTGRES_URL env var if set.")
    parser.add_argument("--since", default="7d",
                        help="Collect sessions since this period, e.g. 7d, 30d, 90d (default: 7d)")
    parser.add_argument("--force", action="store_true",
                        help="Re-push all data, ignoring what is already in the store")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be pushed without writing anything")
    args = parser.parse_args()

    target = args.central or os.environ.get("POSTGRES_URL")
    if not target:
        logger.error("Error: provide --central <path/url> or set POSTGRES_URL env var")
        raise SystemExit(1)

    push(
        central_db=target,
        since=_parse_since(args.since),
        dry_run=args.dry_run,
        force=args.force,
    )


if __name__ == "__main__":
    main()
