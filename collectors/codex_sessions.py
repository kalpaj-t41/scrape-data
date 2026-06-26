"""
Parse Codex (OpenAI / GPT-5.x) session JSONL files and normalize them into the
SAME record shapes the Claude collectors emit, so the existing central_store /
computers pipeline can ingest Codex work with no schema changes.

Codex stores rollouts at  ~/.codex*/sessions/**/*.jsonl  (one JSON object per
line). Its schema differs fundamentally from Claude Code's transcript:

    Claude                              Codex
    ------------------------------      ----------------------------------------
    type: user | assistant | ...        type: session_meta | turn_context
                                              | event_msg | response_item
    parentUuid DAG + isSidechain        flat stream, linked by call_id / turn_id
    tool_use blocks INSIDE a message    separate function_call / _output items
    thinking block (cleartext)          reasoning.encrypted_content (opaque)
    no system prompt stored             full base_instructions in session_meta
    permissionMode (string)             approval_policy + sandbox_policy per turn

This module flattens that into Claude-shaped dicts. See
docs/codex-session-mapping.md for the full field-by-field mapping to the
scrape_data DB columns.

Output of collect():
    {
      "session_metas": [ <session_meta dict>, ... ],   # session_meta.py shape + source="codex"
      "turn_events":   [ <turn dict>, ... ],            # sessions.py _process_jsonl shape
      "busy_segments": [ <segment dict>, ... ],         # sessions.py _segments_from_jsonl shape
    }

Every emitted dict carries the same keys the Claude collectors produce, so
central_store.push(raw) and every computer read it unchanged. Fields Codex has
no equivalent for are left at their pipeline default (0 / {} / [] / False) — we
never fabricate values (see the no-synthetic-data project rule).
"""

import hashlib
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Reuse the Claude discovery helpers so a developer's Codex + Claude work merge
# under one developer_key (both hash the same git email).
from . import discover

logger = logging.getLogger(__name__)

SOURCE = "codex"
_MAX_GAP_S = 600.0  # 10 min — same human-idle cutoff sessions.py uses to split busy segments.

# exec_command shell strings that signal a git action (best-effort, counts invocations).
_GIT_COMMIT_RE = re.compile(r"\bgit\s+commit\b")
_GIT_PUSH_RE = re.compile(r"\bgit\s+push\b")
_EXIT_CODE_RE = re.compile(r"exited with code\s+(\d+)")


# ── small helpers ─────────────────────────────────────────────────────────────

def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _week_of(dt: datetime | None) -> str | None:
    if not dt:
        return None
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _permission_mode(collab_kind: str | None, approval_policy: str | None) -> str | None:
    """
    Map Codex's collaboration mode + approval policy onto Claude's permissionMode
    vocabulary, so the trust computer (M8) can read it unchanged.

      collaboration_mode == 'plan'   → 'plan'
      approval_policy 'never'        → 'bypassPermissions'  (auto-runs everything)
      approval_policy 'on-failure'   → 'acceptEdits'        (runs, asks only on error)
      approval_policy 'on-request'   → 'default'            (asks before acting)
    """
    if (collab_kind or "").lower() == "plan":
        return "plan"
    return {
        "never": "bypassPermissions",
        "on-failure": "acceptEdits",
        "on-request": "default",
        "untrusted": "default",
    }.get((approval_policy or "").lower(), "default")


def _tool_flags(tool_counts: Counter) -> dict:
    """Heuristic uses_* flags from the function names Codex actually called."""
    names = " ".join(tool_counts).lower()
    return {
        # Codex has no sub-agent spawn primitive today; left False unless one appears.
        "uses_task_agent": any(n in tool_counts for n in ("spawn_agent", "task", "agent")),
        "uses_mcp": any(k.startswith("mcp") or "mcp__" in k for k in tool_counts),
        "uses_web_search": "web_search" in names or "browser_search" in names,
        "uses_web_fetch": "web_fetch" in names or "fetch" in tool_counts,
    }


def _count_patch_lines(arguments: str) -> tuple[int, int, int]:
    """
    Best-effort REAL line counts from an apply_patch call's arguments.
    Returns (added, removed, files_touched). Defensive: any parse failure → zeros.

    Codex apply_patch carries a unified-ish patch in its arguments (key 'input'
    or the raw string). We count +/- body lines and '*** {Add,Update,Delete} File'
    markers. This is derived from real transcript content, not fabricated.
    """
    try:
        try:
            patch = json.loads(arguments)
            text = patch.get("input") or patch.get("patch") or patch.get("content") or ""
        except Exception:
            text = arguments or ""
        if not isinstance(text, str) or not text:
            return 0, 0, 0
        added = removed = files = 0
        for line in text.splitlines():
            if line.startswith("*** ") and "File:" in line:
                files += 1
            elif line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        return added, removed, files
    except Exception:
        return 0, 0, 0


# ── core parser ────────────────────────────────────────────────────────────────

def parse_session(path: Path, developer_key: str, codex_dir: str) -> dict | None:
    """
    Parse one Codex rollout .jsonl into Claude-shaped records.

    Returns {"session_meta": dict, "turn_events": [...], "busy_segments": [...]}
    or None if the file has no usable content.
    """
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return None

    session_id: str | None = None
    project_path: str | None = None
    start_dt: datetime | None = None
    first_prompt = ""

    user_msgs = 0
    assistant_msgs = 0
    tool_counts: Counter = Counter()
    input_tokens = 0
    output_tokens = 0
    lines_added = lines_removed = files_modified = 0
    git_commits = git_pushes = 0
    tool_errors = 0

    # current per-turn context (Codex can switch mode mid-session via turn_context)
    cur_collab: str | None = None
    cur_approval: str | None = None

    # turn reconstruction state
    pending_user: dict | None = None
    turn_events: list[dict] = []

    # busy-segment state (human prompt → last agent activity)
    raw_segs: list[tuple[datetime, datetime]] = []
    seg_start: datetime | None = None
    seg_last: datetime | None = None

    # response-time state (agent final answer → next human prompt)
    response_times: list[float] = []
    last_final_dt: datetime | None = None

    all_ts: list[datetime] = []

    def _agent_activity(dt: datetime | None) -> None:
        """Extend the current busy segment with agent activity at `dt`.

        Mirrors sessions._segments_from_jsonl: a stall longer than _MAX_GAP_S is
        treated as human-idle and splits the segment.
        """
        nonlocal seg_start, seg_last
        if dt is None:
            return
        if seg_start is None:
            seg_start = seg_last = dt
            return
        if seg_last and (dt - seg_last).total_seconds() > _MAX_GAP_S:
            if seg_last > seg_start:
                raw_segs.append((seg_start, seg_last))
            seg_start = dt
        seg_last = dt

    def _close_turn(assistant_dt: datetime | None) -> None:
        """Emit the pending turn, computing the user→first-assistant gap."""
        nonlocal pending_user
        if pending_user is None:
            return
        u_dt = pending_user["ts"]
        agent_ms = None
        if u_dt and assistant_dt:
            diff = (assistant_dt - u_dt).total_seconds() * 1000
            if 0 < diff < 600_000:
                agent_ms = round(diff, 1)
        turn_events.append({
            "session_id": session_id,
            "developer_key": developer_key,
            "user_ts": u_dt.isoformat() if u_dt else None,
            "assistant_ts": assistant_dt.isoformat() if assistant_dt else None,
            "agent_ms": agent_ms,
            "is_sidechain": False,                 # Codex has no sidechains
            "permission_mode": pending_user["permission_mode"],
            "tool_uses": pending_user["tool_uses"],
            "prompt_text": pending_user["prompt_text"],
            "agent_colors_in_session": 0,          # no parallel-agent streams
        })
        pending_user = None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue

        rtype = rec.get("type")
        payload = rec.get("payload") or {}
        ts = _parse_iso(rec.get("timestamp"))
        if ts:
            all_ts.append(ts)

        # ── session_meta: identity, start time, project path ──────────────────
        if rtype == "session_meta":
            session_id = payload.get("id") or path.stem
            project_path = payload.get("cwd") or project_path
            start_dt = _parse_iso(payload.get("timestamp")) or start_dt
            continue

        # ── turn_context: track approval/collaboration mode for permission_mode ─
        if rtype == "turn_context":
            cur_approval = (payload.get("approval_policy")
                            or (payload.get("sandbox_policy") or {}).get("type")
                            or cur_approval)
            collab = payload.get("collaboration_mode") or {}
            cur_collab = collab.get("mode") or cur_collab
            continue

        # ── event_msg: user prompts, agent final answers, token usage ─────────
        if rtype == "event_msg":
            etype = payload.get("type")

            if etype == "user_message":
                user_msgs += 1
                text = (payload.get("message") or "").strip()
                if not first_prompt and text:
                    first_prompt = text
                # a new human prompt closes any open turn (with its recorded reply ts)
                if pending_user is not None:
                    _close_turn(pending_user.get("assistant_dt"))
                # response time: gap from last agent final answer to this prompt
                if last_final_dt and ts and ts > last_final_dt:
                    gap = (ts - last_final_dt).total_seconds()
                    if 0 < gap < 86_400:
                        response_times.append(round(gap, 1))
                    last_final_dt = None
                # busy segment: close previous, start fresh at the human prompt
                if seg_start and seg_last and seg_last > seg_start:
                    raw_segs.append((seg_start, seg_last))
                seg_start = seg_last = ts
                pending_user = {
                    "ts": ts,
                    "permission_mode": _permission_mode(cur_collab, cur_approval),
                    "tool_uses": [],
                    "prompt_text": text,
                    "answered": False,
                }

            elif etype == "agent_message":
                # commentary or final_answer — both are agent activity
                if payload.get("phase") == "final_answer":
                    if ts:
                        last_final_dt = ts
                _agent_activity(ts)
                # record first assistant reply timestamp for the open turn
                if pending_user is not None and not pending_user.get("answered"):
                    pending_user["answered"] = True
                    pending_user["assistant_dt"] = ts

            elif etype == "token_count":
                info = payload.get("info")
                if isinstance(info, dict):
                    usage = info.get("total_token_usage") or {}
                    # cumulative — keep the largest seen
                    input_tokens = max(input_tokens, usage.get("input_tokens", 0) or 0)
                    output_tokens = max(output_tokens, usage.get("output_tokens", 0) or 0)
                _agent_activity(ts)
            else:
                _agent_activity(ts)
            continue

        # ── response_item: messages, reasoning, tool calls / outputs ──────────
        if rtype == "response_item":
            ptype = payload.get("type")

            if ptype == "message":
                role = payload.get("role")
                if role == "assistant":
                    assistant_msgs += 1
                    if pending_user is not None and not pending_user.get("answered"):
                        pending_user["answered"] = True
                        pending_user["assistant_dt"] = ts
                _agent_activity(ts)

            elif ptype == "function_call":
                name = payload.get("name") or "unknown"
                tool_counts[name] += 1
                if pending_user is not None:
                    pending_user["tool_uses"].append(name)
                args = payload.get("arguments") or ""
                if name == "apply_patch":
                    a, r, f = _count_patch_lines(args)
                    lines_added += a
                    lines_removed += r
                    files_modified += f
                elif name in ("exec_command", "shell", "local_shell"):
                    if _GIT_COMMIT_RE.search(args):
                        git_commits += 1
                    if _GIT_PUSH_RE.search(args):
                        git_pushes += 1
                _agent_activity(ts)

            elif ptype == "function_call_output":
                out = payload.get("output") or ""
                m = _EXIT_CODE_RE.search(out if isinstance(out, str) else "")
                if m and m.group(1) != "0":
                    tool_errors += 1
                _agent_activity(ts)

            else:  # reasoning, etc.
                _agent_activity(ts)
            continue

        # unknown line type — ignore

    # finalize the last open turn + segment
    if pending_user is not None:
        _close_turn(pending_user.get("assistant_dt"))
    if seg_start and seg_last and seg_last > seg_start:
        raw_segs.append((seg_start, seg_last))

    if not session_id:
        return None
    if not start_dt:
        start_dt = all_ts[0] if all_ts else None

    end_dt = all_ts[-1] if all_ts else start_dt
    duration_min = 0
    if start_dt and end_dt and end_dt > start_dt:
        duration_min = int((end_dt - start_dt).total_seconds() // 60)

    flags = _tool_flags(tool_counts)

    session_meta = {
        "session_id": session_id,
        "developer_key": developer_key,
        "claude_dir": codex_dir,                 # reuse the column; it's the .codex dir here
        "account_type": SOURCE,
        "source": SOURCE,
        "project_path": project_path,
        "start_time": start_dt.isoformat() if start_dt else None,
        "week": _week_of(start_dt),
        "date": start_dt.date().isoformat() if start_dt else None,
        "duration_minutes": duration_min,
        "user_message_count": user_msgs,
        "assistant_message_count": assistant_msgs,
        "tool_counts": dict(tool_counts),
        "languages": {},                          # not recorded by Codex
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "files_modified": files_modified,
        "git_commits": git_commits,
        "git_pushes": git_pushes,
        "first_prompt": first_prompt,
        "user_interruptions": 0,                  # no Codex equivalent
        "user_response_times": response_times,
        "tool_errors": tool_errors,
        **flags,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }

    busy_segments = [
        {
            "session_id": session_id,
            "developer_key": developer_key,
            "start_ts": s.isoformat(),
            "end_ts": e.isoformat(),
            "is_sidechain": False,
        }
        for s, e in raw_segs
    ]

    return {
        "session_meta": session_meta,
        "turn_events": turn_events,
        "busy_segments": busy_segments,
    }


# ── discovery + collection ──────────────────────────────────────────────────────

def find_codex_dirs(home: Path | None = None) -> list[Path]:
    """Return all Codex data dirs (~/.codex, ~/.codex-work, ...) that have sessions."""
    base = home or Path.home()
    dirs: list[Path] = []
    try:
        for entry in base.iterdir():
            if entry.name.startswith(".codex") and entry.is_dir() and (entry / "sessions").exists():
                dirs.append(entry)
    except Exception:
        pass
    return sorted(dirs)


def _developer_key_for(codex_dir: Path) -> tuple[str, str | None, str | None]:
    """
    Resolve (developer_key, name, email) for a Codex dir using the same rule as
    discover: global git identity → hostname fallback. This makes a developer's
    Codex sessions merge with their Claude sessions under one key.
    """
    name, email = discover._git_identity()
    if email:
        return discover._sha256(email), name, email
    import socket
    return discover._sha256(socket.gethostname() + str(codex_dir)), name, None


def augment_developer_map(developer_map: list[dict], home: Path | None = None) -> list[dict]:
    """
    Fold Codex dirs into an existing Claude developer_map (in place) so ONE
    upsert_developers() call registers both. A dev's Codex dir is appended to
    their claude_dirs; a Codex-only developer (no .claude dir) is added fresh.

    Safe for the Claude collectors: they skip any dir without a projects/
    subdir, so an appended .codex path is simply ignored by them.
    """
    by_key = {d["developer_key"]: d for d in developer_map}
    for codex_dir in find_codex_dirs(home):
        key, name, email = _developer_key_for(codex_dir)
        d = by_key.get(key)
        if d is None:
            d = {"developer_key": key, "name": name, "email": email, "claude_dirs": []}
            by_key[key] = d
            developer_map.append(d)
        if str(codex_dir) not in d["claude_dirs"]:
            d["claude_dirs"].append(str(codex_dir))
        if not d.get("name") and name:
            d["name"] = name
        if not d.get("email") and email:
            d["email"] = email
    return developer_map


def _too_old(path: Path, since: datetime) -> bool:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < since
    except Exception:
        return False


def collect(
    developer_map: list[dict] | None = None,
    processed_sessions: set[str] | None = None,
    since: datetime | None = None,
    home: Path | None = None,
) -> dict:
    """
    Discover and parse every Codex rollout into Claude-shaped records.

    Args mirror sessions.collect():
      developer_map      optional — if given, reuse its email→key/name mapping so
                         identity matches the Claude collectors exactly.
      processed_sessions session_ids to skip (incremental, no --since window).
      since              only parse files modified at/after this time.

    Returns {"session_metas", "turn_events", "busy_segments"} ready to merge into
    the batch_runner raw dict and push via central_store unchanged.
    """
    processed_sessions = processed_sessions or set()

    # email → (key, name) from the Claude-side map, so we reuse resolved identities.
    email_to_key = {}
    if developer_map:
        for d in developer_map:
            if d.get("email"):
                email_to_key[d["email"].strip().lower()] = d["developer_key"]

    session_metas: list[dict] = []
    turn_events: list[dict] = []
    busy_segments: list[dict] = []

    for codex_dir in find_codex_dirs(home):
        key, _name, email = _developer_key_for(codex_dir)
        if email and email.strip().lower() in email_to_key:
            key = email_to_key[email.strip().lower()]

        for jsonl_file in (codex_dir / "sessions").glob("**/*.jsonl"):
            if since and _too_old(jsonl_file, since):
                continue
            parsed = parse_session(jsonl_file, key, str(codex_dir))
            if not parsed:
                continue
            sid = parsed["session_meta"]["session_id"]
            # incremental skip only when no --since window (active files re-parse)
            if not since and sid in processed_sessions:
                continue
            session_metas.append(parsed["session_meta"])
            turn_events.extend(parsed["turn_events"])
            busy_segments.extend(parsed["busy_segments"])

    return {
        "session_metas": session_metas,
        "turn_events": turn_events,
        "busy_segments": busy_segments,
    }


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Parse Codex sessions → Claude-shaped records")
    ap.add_argument("path", nargs="?", help="A single .jsonl to inspect (else discover all)")
    args = ap.parse_args()

    if args.path:
        out = parse_session(Path(args.path), developer_key="DEV_DEBUG", codex_dir="cli")
        print(json.dumps(out, indent=2)[:4000])
    else:
        raw = collect()
        logger.info("Codex sessions: %d | turn_events: %d | busy_segments: %d",
                    len(raw["session_metas"]), len(raw["turn_events"]), len(raw["busy_segments"]))
        for m in raw["session_metas"][:5]:
            logger.info("  %s  dev=%s  turns=%d  tools=%s",
                        m["session_id"][:12], m["developer_key"][:10],
                        m["user_message_count"], m["tool_counts"])
