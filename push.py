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


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _select_sessions_for_push(
    current_prompt_cursors: dict[str, str | None],
    stored_prompt_cursors: dict[str, str | None],
    force: bool,
) -> set[str]:
    if force:
        return set(current_prompt_cursors)

    selected: set[str] = set()
    for session_id, current_prompt_ts in current_prompt_cursors.items():
        stored_prompt_ts = stored_prompt_cursors.get(session_id)
        if current_prompt_ts is None:
            if session_id not in stored_prompt_cursors:
                selected.add(session_id)
            continue
        current_dt = _parse_iso(current_prompt_ts)
        stored_dt = _parse_iso(stored_prompt_ts)
        if stored_dt is None or (current_dt and current_dt > stored_dt):
            selected.add(session_id)
    return selected


def _filter_turn_events(
    events: list[dict],
    selected_session_ids: set[str],
    stored_prompt_cursors: dict[str, str | None],
    force: bool,
) -> list[dict]:
    filtered: list[dict] = []
    for event in events:
        session_id = event.get("session_id")
        if session_id not in selected_session_ids:
            continue
        if force:
            filtered.append(event)
            continue

        cursor_dt = _parse_iso(stored_prompt_cursors.get(session_id))
        if cursor_dt is None:
            filtered.append(event)
            continue

        if event.get("event_type") == "skill":
            skill_dt = _parse_iso(event.get("ts"))
            if skill_dt and skill_dt > cursor_dt:
                filtered.append(event)
            continue

        user_dt = _parse_iso(event.get("user_ts"))
        if user_dt and user_dt > cursor_dt:
            filtered.append(event)
    return filtered


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

    developer_map = discover.build_developer_map()
    dev_dirs = [d for dev in developer_map for d in dev["claude_dirs"]]
    logger.info(f"\n[push] Found {len(developer_map)} developer(s) across {len(dev_dirs)} account(s):")
    for dev in developer_map:
        logger.info(f"         {dev.get('name') or 'unknown'} <{dev.get('email') or 'no email'}>")
        for d in dev["claude_dirs"]:
            logger.info(f"           {d}")

    if not dry_run:
        store.upsert_developers(developer_map)

    stored_prompt_cursors = {} if force else store.session_prompt_cursors()
    current_prompt_cursors = session_index.collect_latest_prompts(developer_map, since=since)
    selected_session_ids = _select_sessions_for_push(
        current_prompt_cursors,
        stored_prompt_cursors,
        force=force,
    )
    skipped_sessions = max(len(current_prompt_cursors) - len(selected_session_ids), 0)

    logger.info(
        f"\n[push] Session freshness: {len(current_prompt_cursors)} scanned, "
        f"{len(selected_session_ids)} advanced/new, {skipped_sessions} unchanged"
    )

    if not selected_session_ids and not force:
        logger.info("[push] No sessions advanced past the stored prompt cursor.")
        if dry_run:
            logger.info("\n[push] DRY RUN — nothing written.")
        store.close()
        return

    logger.info("\n[push] Collecting session metadata...")
    raw_session_metas = [
        m for m in session_meta.collect(developer_map, since=since)
        if m["session_id"] in selected_session_ids
    ]
    jsonl_sessions = [
        m for m in session_index.collect(developer_map, since=since)
        if m["session_id"] in selected_session_ids
    ]
    union_metas = session_index.merge_jsonl_primary(jsonl_sessions, raw_session_metas)
    for meta in union_metas:
        meta["last_prompt_ts"] = current_prompt_cursors.get(meta["session_id"])
    logger.info(
        f"         {len(jsonl_sessions)} JSONL (primary), {len(raw_session_metas)} telemetry, "
        f"{len(union_metas)} union to upsert"
    )

    logger.info("[push] Parsing JSONL transcripts...")
    raw_turn_events = _filter_turn_events(
        sessions.collect(developer_map, since=since),
        selected_session_ids,
        stored_prompt_cursors,
        force=force,
    )
    new_te_sessions = len({e["session_id"] for e in raw_turn_events})
    logger.info(f"         {len(raw_turn_events)} turn events across {new_te_sessions} session(s)")

    logger.info("[push] Building busy segments (accurate agent hours)...")
    raw_busy_segments = [
        s for s in sessions.collect_segments(developer_map, since=since)
        if s["session_id"] in selected_session_ids
    ]
    seg_sessions = len({s["session_id"] for s in raw_busy_segments})
    logger.info(f"         {len(raw_busy_segments)} segments across {seg_sessions} sessions")

    logger.info("[push] Collecting facets, app state, plans, agent tasks...")
    raw_facets = {
        sid: data for sid, data in facets.collect(developer_map).items()
        if sid in selected_session_ids
    }
    raw_app_state = app_state.collect(developer_map)
    raw_plans = plans.collect(developer_map)
    raw_agent_tasks = {
        sid: data for sid, data in agent_tasks.collect(developer_map, since=since).items()
        if sid in selected_session_ids
    }
    plugins.collect(developer_map)
    settings.collect(developer_map)

    task_count = sum(len(v.get("tasks", [])) for v in raw_agent_tasks.values())
    logger.info(
        f"         {len(raw_facets)} facets, {len(raw_app_state)} app states, "
        f"{len(raw_agent_tasks)} agent sessions ({task_count} tasks)"
    )

    raw = {
        "session_metas": union_metas,
        "turn_events": raw_turn_events,
        "busy_segments": raw_busy_segments,
        "facets": raw_facets,
        "app_state": raw_app_state,
        "plans": raw_plans,
        "agent_tasks": raw_agent_tasks,
    }

    logger.info("\n[push] To be pushed:")
    logger.info(f"         session_metas  : {len(union_metas)}")
    logger.info(f"         turn_events    : {len(raw_turn_events)}")
    logger.info(f"         busy_segments  : {len(raw_busy_segments)}")
    logger.info(f"         facets         : {len(raw_facets)}")
    logger.info(f"         app_state      : {len(raw_app_state)}")
    logger.info(f"         plans          : {len(raw_plans)}")
    logger.info(f"         agent_tasks    : {task_count}")

    if dry_run:
        logger.info("\n[push] DRY RUN — nothing written.")
        store.close()
        return

    inserted = store.push(raw, force=force)
    store.close()

    stats_delta = {
        "session_metas": inserted.get("session_metas", 0),
        "turn_events": inserted.get("turn_events", 0),
        "busy_segments": inserted.get("busy_segments", 0),
        "facets": inserted.get("facets", 0),
        "app_state": inserted.get("app_state", 0),
        "plans": inserted.get("plans", 0),
        "agent_tasks": inserted.get("agent_tasks", 0),
    }

    logger.info("\n[push] Done.")
    logger.info(f"  {'Table':<22} {'Before':>8} {'Inserted':>10} {'After':>8}")
    logger.info("  " + "-" * 52)
    for k, before in stats_before.items():
        if k in stats_delta:
            after = before + stats_delta[k]
            logger.info(f"  {k:<22} {before:>8} {stats_delta[k]:>10} {after:>8}")


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
