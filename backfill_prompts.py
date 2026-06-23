#!/usr/bin/env python3
"""
Backfill prompt_text for turn_events rows pushed before the prompt collection
feature was added.

For each session in scrape_data.turn_events with NULL prompt_text, finds the
original JSONL file by scanning ~/.claude* directories, extracts user message
text, and UPDATEs the rows by matching on (session_id, user_ts).

Usage:
  python backfill_prompts.py postgresql://user:pass@host:5432/dbname
  python backfill_prompts.py  # uses POSTGRES_URL env var
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from central_store import CentralStore
from collectors.sessions import _extract_user_text, _parse_iso

logger = logging.getLogger(__name__)


def _build_jsonl_index() -> dict[str, Path]:
    """Scan all ~/.claude* dirs and return {session_id: jsonl_path}."""
    index: dict[str, Path] = {}
    for claude_dir in sorted(Path.home().glob(".claude*")):
        projects_dir = claude_dir / "projects"
        if not projects_dir.is_dir():
            continue
        for jsonl in projects_dir.glob("**/*.jsonl"):
            index[jsonl.stem] = jsonl
    return index


def _build_prompt_map(jsonl_path: Path) -> dict[datetime, str]:
    """Parse a JSONL and return {user_ts (datetime): prompt_text}."""
    prompt_map: dict[datetime, str] = {}
    try:
        lines = jsonl_path.read_text(errors="replace").splitlines()
    except Exception:
        return prompt_map

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("type") != "user":
            continue
        ts_raw = msg.get("timestamp")
        ts = _parse_iso(ts_raw) if ts_raw else None
        if not ts:
            continue
        text = _extract_user_text(msg)
        if text:
            prompt_map[ts] = text

    return prompt_map


def backfill(db_url: str) -> None:
    cs = CentralStore(db_url)

    # All session_ids that have turns with NULL prompt_text
    rows = cs._fetchall("""
        SELECT DISTINCT session_id
        FROM scrape_data.turn_events
        WHERE event_type = 'turn' AND prompt_text IS NULL
        ORDER BY session_id
    """)

    logger.info(f"Sessions to backfill: {len(rows)}")
    if not rows:
        logger.info("Nothing to do.")
        cs.close()
        return

    logger.info("Building JSONL index from ~/.claude* ...")
    jsonl_index = _build_jsonl_index()
    logger.info(f"  {len(jsonl_index)} JSONL files indexed")

    total_updated = 0
    total_skipped = 0

    for (session_id,) in rows:
        jsonl_path = jsonl_index.get(session_id)
        if not jsonl_path:
            logger.info(f"  [{session_id[:8]}] JSONL not found — skipping")
            total_skipped += 1
            continue

        prompt_map = _build_prompt_map(jsonl_path)
        if not prompt_map:
            logger.info(f"  [{session_id[:8]}] no user text found — skipping")
            total_skipped += 1
            continue

        # UPDATE each turn row by matching user_ts
        cur = cs._conn.cursor()
        session_updated = 0
        for user_ts, prompt_text in prompt_map.items():
            cur.execute(
                """
                UPDATE scrape_data.turn_events
                   SET prompt_text = %s
                 WHERE session_id = %s
                   AND user_ts    = %s
                   AND event_type = 'turn'
                   AND prompt_text IS NULL
                """,
                (prompt_text, session_id, user_ts),
            )
            session_updated += cur.rowcount

        cs._conn.commit()
        total_updated += session_updated
        logger.info(f"  [{session_id[:8]}] updated {session_updated} / {len(prompt_map)} turns")

    logger.info(f"Done.  turns backfilled: {total_updated}  sessions skipped: {total_skipped}")
    cs.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    url = (sys.argv[1] if len(sys.argv) > 1 else None) or os.environ.get("POSTGRES_URL")
    if not url:
        logger.error("Usage: python backfill_prompts.py postgresql://user:pass@host/db")
        logger.error("       or set POSTGRES_URL env var")
        sys.exit(1)
    backfill(url)
