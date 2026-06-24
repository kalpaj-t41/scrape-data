"""
Parse session JSONL files across all .claude* directories.

Extracts per-turn events needed for:
  - Agent hours (M3): user_ts → assistant_ts gap per turn
  - Parallel agents (M4): isSidechain, agentColor per message
  - Skills (M7): system/local_command messages with slash commands
  - Trust (M8): permissionMode per message

Output: list of turn event dicts.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path


_SKILL_RE = re.compile(r"<command-name>(/[^<]+)</command-name>")


def _extract_user_text(msg: dict) -> str:
    """Extract plain text from a user message's content field."""
    content = msg.get("message", {}).get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_command(content: str) -> str | None:
    m = _SKILL_RE.search(content)
    return m.group(1) if m else None


def _process_jsonl(path: Path, developer_key: str) -> list[dict]:
    """Parse one session JSONL file. Returns turn events."""
    events = []
    pending_user: dict | None = None
    session_id = None
    agent_colors: set[str] = set()

    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue

        mtype = msg.get("type", "")
        session_id = session_id or msg.get("sessionId", path.stem)

        # Track agent colors (each color = distinct parallel agent stream)
        if mtype == "agent-color":
            color = msg.get("agentColor")
            if color:
                agent_colors.add(color)
            continue

        ts_raw = msg.get("timestamp")
        ts = _parse_iso(ts_raw) if ts_raw else None
        is_sidechain = bool(msg.get("isSidechain", False))
        permission_mode = msg.get("permissionMode")

        if mtype == "user":
            pending_user = {
                "ts": ts,
                "permission_mode": permission_mode,
                "is_sidechain": is_sidechain,
                "prompt_text": _extract_user_text(msg),
            }

        elif mtype == "assistant" and pending_user is not None:
            user_ts = pending_user["ts"]
            agent_ms = None
            if user_ts and ts:
                diff = (ts - user_ts).total_seconds() * 1000
                # Sanity: ignore negative gaps or gaps > 10 minutes (idle time)
                if 0 < diff < 600_000:
                    agent_ms = round(diff, 1)

            # Extract tool use counts from assistant message content
            tool_uses = []
            content_blocks = msg.get("message", {}).get("content", [])
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_uses.append(block.get("name", ""))

            events.append({
                "session_id":    session_id,
                "developer_key": developer_key,
                "user_ts":       user_ts.isoformat() if user_ts else None,
                "assistant_ts":  ts.isoformat() if ts else None,
                "agent_ms":      agent_ms,
                "is_sidechain":  pending_user["is_sidechain"] or is_sidechain,
                "permission_mode": pending_user["permission_mode"] or permission_mode,
                "tool_uses":     tool_uses,
                "prompt_text":   pending_user["prompt_text"],
            })
            pending_user = None

        elif mtype == "system":
            subtype = msg.get("subtype", "")
            content = msg.get("content", "")
            if subtype == "local_command" and isinstance(content, str):
                command = _extract_command(content)
                if command:
                    events.append({
                        "session_id": session_id,
                        "developer_key": developer_key,
                        "event_type": "skill",
                        "command": command,
                        "ts": ts.isoformat() if ts else None,
                        "is_sidechain": is_sidechain,
                    })

    # Attach agent_colors count to all events from this session
    for e in events:
        e.setdefault("agent_colors_in_session", len(agent_colors))

    return events


# ── Busy-segment extraction (agent-hours M3, accurate path) ───────────────────
#
# The per-turn agent_ms above only measures the gap from a user message to the
# FIRST assistant reply. That misses tool runtime (assistant→tool_result gaps)
# and drops whole turns > 10 min. For agent hours we instead build "busy
# segments": [human_prompt → last agent message before the next human prompt].
# Everything inside a segment (model thinking, tool execution, sub-agent work)
# counts; only true human-idle gaps between segments are excluded.

_MAX_GAP_S = 600.0  # 10 min — split a segment here (user walked away mid-turn).
# Matches the per-turn idle cutoff (_process_jsonl drops gaps > 600_000 ms), so
# both agent-hours paths use one idle definition. Long tool/sub-agent runs are
# their own segments (internal gaps stay < 10 min), so this won't truncate them.


def _classify(msg: dict) -> str | None:
    """human = real user prompt, agent = assistant or tool_result, None = ignore."""
    t = msg.get("type")
    if t == "assistant":
        return "agent"
    if t != "user":
        return None
    content = msg.get("message", {}).get("content", "")
    if isinstance(content, str):
        return "human" if content.strip() else None
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return "agent"
        if any(isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
               for b in content):
            return "human"
    return None


def _segments_from_jsonl(
    path: Path, developer_key: str, session_id: str, is_sidechain: bool
) -> list[dict]:
    """Build busy segments from one JSONL file (main session or sub-agent)."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return []

    raw_segs: list[tuple[datetime, datetime]] = []
    cur_start: datetime | None = None
    last_ts: datetime | None = None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        kind = _classify(msg)
        if kind is None:
            continue
        ts_raw = msg.get("timestamp")
        ts = _parse_iso(ts_raw) if ts_raw else None
        if not ts:
            continue

        if kind == "human":
            # Close the previous segment; human-idle gap before this prompt excluded.
            if cur_start and last_ts and last_ts > cur_start:
                raw_segs.append((cur_start, last_ts))
            cur_start, last_ts = ts, ts
        else:  # agent activity
            if cur_start is None:
                cur_start = last_ts = ts
            else:
                # Long stall mid-turn → treat as idle, split the segment.
                if last_ts and (ts - last_ts).total_seconds() > _MAX_GAP_S:
                    if last_ts > cur_start:
                        raw_segs.append((cur_start, last_ts))
                    cur_start = ts
                last_ts = ts

    if cur_start and last_ts and last_ts > cur_start:
        raw_segs.append((cur_start, last_ts))

    return [
        {
            "session_id":    session_id,
            "developer_key": developer_key,
            "start_ts":      s.isoformat(),
            "end_ts":        e.isoformat(),
            "is_sidechain":  is_sidechain,
        }
        for s, e in raw_segs
    ]


def _too_old(path: Path, since: datetime) -> bool:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < since
    except Exception:
        return False


_WF_SKIP_STEMS = {"journal", "history"}


def collect_segments(
    developer_map: list[dict],
    since: datetime | None = None,
) -> list[dict]:
    """
    Busy segments across all claude dirs — primary source for agent hours.

    Three sources, all tagged is_sidechain appropriately:
      1. Main session files:  <project>/<session>.jsonl          is_sidechain=False
      2. Agent tool sub-agents: <project>/<session>/subagents/agent-*.jsonl
                                                                  is_sidechain=True
      3. Workflow tool sub-agents:
           <project>/<session>/subagents/workflows/<run-id>/<agent>.jsonl
                                                                  is_sidechain=True
         Workflow sub-agents do NOT inject isSidechain entries into the parent
         session JSONL (unlike Agent tool calls), so their execution windows would
         be invisible to the pipeline without this explicit scan.
         journal.jsonl and history.jsonl inside workflow run dirs are skipped.
    """
    all_segs: list[dict] = []
    for dev in developer_map:
        key = dev["developer_key"]
        for claude_dir_str in dev["claude_dirs"]:
            projects_dir = Path(claude_dir_str) / "projects"
            if not projects_dir.exists():
                continue
            for project_dir in projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue

                # 1. Main session files
                for jsonl_file in project_dir.glob("*.jsonl"):
                    if since and _too_old(jsonl_file, since):
                        continue
                    all_segs.extend(
                        _segments_from_jsonl(jsonl_file, key, jsonl_file.stem, False)
                    )

                # 2. Agent tool sub-agents: <session>/subagents/<agent>.jsonl
                #    (**/subagents/*.jsonl matches only one level inside subagents/)
                for sub_file in project_dir.glob("*/subagents/*.jsonl"):
                    if since and _too_old(sub_file, since):
                        continue
                    parent_session = sub_file.parent.parent.name
                    all_segs.extend(
                        _segments_from_jsonl(sub_file, key, parent_session, True)
                    )

                # 3. Workflow tool sub-agents:
                #    <session>/subagents/workflows/<run-id>/<agent>.jsonl
                for wf_file in project_dir.glob("*/subagents/workflows/*/*.jsonl"):
                    if wf_file.stem in _WF_SKIP_STEMS:
                        continue
                    if since and _too_old(wf_file, since):
                        continue
                    # wf_file.parents: [run-dir, workflows/, subagents/, session-dir, project-dir]
                    parent_session = wf_file.parents[3].name
                    all_segs.extend(
                        _segments_from_jsonl(wf_file, key, parent_session, True)
                    )

    return all_segs


# ── Per-tool-call signal stream (U1: efficiency / usefulness / QAAH backbone) ──
#
# collect_segments() above carries only timing. The quality layer needs to know,
# per busy segment, which tool calls failed, were interrupted, and on what target
# (so a failure can be matched to its later successful retry).
#
# IMPLEMENTATION-TIME CORRECTION (U1): the plan specified reading `is_error` from the
# top-level `toolUseResult` object to match collectors/session_index.py. Real
# transcripts contradict this — `toolUseResult` carries NO `is_error`/`status` field
# (0 of 3.5k sampled results), so that source reports zero failures. The error flag
# lives on the content `tool_result` block (`is_error`), which is the authoritative
# per-call source here. We read `is_error` from the content block (with the
# toolUseResult is_error/status kept as a supplement for tools that do set it), and
# `interrupted` from `toolUseResult` (which IS present). NOTE: session_index.py's
# `tool_errors` reads only toolUseResult and is therefore near-blind — flagged for a
# follow-up fix, out of U1 scope. The tool_use->tool_result correlation by
# tool_use_id is new code (no prior linkage existed in the codebase).


def _tool_target(tool_input) -> str | None:
    """Best-effort normalized target for a tool call: edited file path, or command
    head. Lets retry-of-the-same-target detection (efficiency, U4) key on it."""
    if not isinstance(tool_input, dict):
        return None
    for key in ("file_path", "filePath", "path", "notebook_path"):
        v = tool_input.get(key)
        if v:
            return str(v)
    cmd = tool_input.get("command")
    if isinstance(cmd, str) and cmd.strip():
        return cmd.strip().split()[0]
    return None


# Verification-run detection (U2). Best-effort + tunable: a Bash command matching
# one of these is treated as a test/lint/typecheck/build run, and its pass/fail
# feeds Usefulness (U5). Typecheck is listed before build so `tsc` classifies as
# typecheck, not build. Matched against the full command string.
_VERIFICATION_PATTERNS = [
    ("test", re.compile(
        r"\b(pytest|py\.test|jest|vitest|mocha|rspec|phpunit|cargo test|go test|"
        r"npm (?:run )?test|yarn test|pnpm test|python -m unittest)\b", re.I)),
    ("lint", re.compile(
        r"\b(eslint|ruff(?! format)|flake8|pylint|rubocop|golangci-lint|"
        r"npm run lint|yarn lint|prettier --check|black --check)\b", re.I)),
    ("typecheck", re.compile(
        r"\b(mypy|pyright|tsc\b|flow check)\b", re.I)),
    ("build", re.compile(
        r"\b(npm run build|yarn build|pnpm build|make\b|cargo build|go build|"
        r"webpack|vite build|gradle\b|mvn (?:package|compile|verify))\b", re.I)),
]


def _verification_kind(name: str, tool_input) -> str | None:
    """Classify a Bash command as a verification run (test / lint / typecheck /
    build), or None. Best-effort and tunable (U2)."""
    if name != "Bash" or not isinstance(tool_input, dict):
        return None
    cmd = tool_input.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    for kind, rx in _VERIFICATION_PATTERNS:
        if rx.search(cmd):
            return kind
    return None


def _msg_supplemental_error(msg: dict) -> bool:
    """toolUseResult-level is_error/status (rarely set in practice) — kept as a
    supplement to the content-block is_error for tools that do populate it."""
    tur = msg.get("toolUseResult")
    if not isinstance(tur, dict):
        return False
    status = str(tur.get("status", "")).lower()
    return bool(tur.get("is_error") or status in ("error", "failed"))


def _msg_interrupted(msg: dict) -> bool:
    """Per-call interrupt flag — lives on the top-level `toolUseResult`."""
    tur = msg.get("toolUseResult")
    return bool(tur.get("interrupted")) if isinstance(tur, dict) else False


def _read_agent_meta(jsonl_path: Path) -> dict:
    """Read the sibling `agent-<id>.meta.json` ({agentType, description, toolUseId})
    written next to an Agent-tool / workflow sub-agent transcript (U11). Returns {}
    when absent or unreadable — best-effort, never raises."""
    try:
        meta_path = jsonl_path.with_suffix(".meta.json")
        if meta_path.exists():
            return json.loads(meta_path.read_text(errors="replace")) or {}
    except Exception:
        pass
    return {}


def _patch_lines(msg: dict) -> tuple[str | None, list[str], list[str]]:
    """(filePath, added_lines, removed_lines) from the toolUseResult.structuredPatch
    of an edit tool result (U3 churn). structuredPatch lives on toolUseResult."""
    tur = msg.get("toolUseResult")
    if not isinstance(tur, dict):
        return None, [], []
    added, removed = [], []
    for hunk in tur.get("structuredPatch", []) or []:
        for ln in hunk.get("lines", []) or []:
            if ln.startswith("+"):
                added.append(ln[1:])
            elif ln.startswith("-"):
                removed.append(ln[1:])
    return tur.get("filePath"), added, removed


def _result_blocks(msg: dict) -> list[tuple[str, bool]]:
    """(tool_use_id, is_error) from content tool_result blocks. `is_error` lives on
    the content block (the authoritative per-call error source — see header note),
    and tool_use_id is the join key back to the originating assistant tool_use."""
    content = msg.get("message", {}).get("content", "")
    if not isinstance(content, list):
        return []
    return [
        (b["tool_use_id"], bool(b.get("is_error")))
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id")
    ]


def _signals_from_jsonl(
    path: Path, developer_key: str, session_id: str, is_sidechain: bool,
    agent_kind: str = "main", agent_id: str | None = None,
    agent_type: str | None = None, workflow_run_id: str | None = None,
    spawn_tool_use_id: str | None = None,
) -> list[dict]:
    """Build per-segment tool-call signal records from one JSONL file.

    Single pass: segment boundaries are built with the SAME human/agent classify +
    idle-split logic as _segments_from_jsonl (so windows match collect_segments),
    while tool calls are correlated (tool_use -> toolUseResult) and bucketed into
    those windows by timestamp containment.
    """
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return []

    raw_segs: list[tuple[datetime, datetime]] = []
    cur_start: datetime | None = None
    last_ts: datetime | None = None

    pending: dict[str, dict] = {}        # tool_use_id -> {name, target, ts}
    calls: list[dict] = []               # correlated tool calls (with native ts)
    interrupt_ts: list[datetime] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue

        ts = _parse_iso(msg.get("timestamp")) if msg.get("timestamp") else None
        mtype = msg.get("type")

        # ── tool-call correlation ──
        if mtype == "assistant" and ts:
            for b in msg.get("message", {}).get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                    pending[b["id"]] = {
                        "name": b.get("name", ""),
                        "target": _tool_target(b.get("input")),
                        "vkind": _verification_kind(b.get("name", ""), b.get("input")),
                        "ts": ts,
                    }
        elif mtype == "user":
            blocks = _result_blocks(msg)
            if blocks:
                interrupted = _msg_interrupted(msg)
                supplemental_err = _msg_supplemental_error(msg)
                fp, added, removed = _patch_lines(msg)
                if interrupted and ts:
                    interrupt_ts.append(ts)
                for tuid, block_err in blocks:
                    call = pending.pop(tuid, None)
                    if call is not None:
                        calls.append({
                            "name": call["name"], "target": call["target"],
                            "vkind": call["vkind"],
                            "is_error": bool(block_err or supplemental_err),
                            "interrupted": interrupted,
                            "ts": call["ts"],
                            "patch_fp": fp, "added": added, "removed": removed,
                        })

        # ── segment building (mirrors _segments_from_jsonl) ──
        kind = _classify(msg)
        if kind is None or not ts:
            continue
        if kind == "human":
            if cur_start and last_ts and last_ts > cur_start:
                raw_segs.append((cur_start, last_ts))
            cur_start = last_ts = ts
        else:
            if cur_start is None:
                cur_start = last_ts = ts
            else:
                if last_ts and (ts - last_ts).total_seconds() > _MAX_GAP_S:
                    if last_ts > cur_start:
                        raw_segs.append((cur_start, last_ts))
                    cur_start = ts
                last_ts = ts

    if cur_start and last_ts and last_ts > cur_start:
        raw_segs.append((cur_start, last_ts))

    # tool_use blocks with no matching result (truncated session / no result msg).
    for call in pending.values():
        calls.append({
            "name": call["name"], "target": call["target"], "vkind": call["vkind"],
            "is_error": None, "interrupted": False, "ts": call["ts"],
            "patch_fp": None, "added": [], "removed": [],
        })

    # ── churn (U3): agent-added lines later reverted/overwritten in the same session ──
    # Walk edits in time order; a removed line that matches a line added earlier this
    # session marks that earlier addition as not-survived. added_records keep the
    # adding ts so churn attributes to the segment where the line was written.
    added_records: list[dict] = []
    pool: dict[str, list[dict]] = {}   # filePath -> still-alive added records
    for c in sorted(calls, key=lambda c: c["ts"]):
        fp = c.get("patch_fp")
        if not fp:
            continue
        alive = pool.setdefault(fp, [])
        for text in c.get("removed", []):
            for i, rec in enumerate(alive):
                if rec["text"] == text:
                    rec["reverted"] = True
                    alive.pop(i)
                    break
        for text in c.get("added", []):
            rec = {"ts": c["ts"], "text": text, "reverted": False}
            added_records.append(rec)
            alive.append(rec)

    records = []
    for s, e in raw_segs:
        in_seg = [c for c in calls if s <= c["ts"] <= e]
        seg_calls = [
            {"name": c["name"], "target": c["target"], "is_error": c["is_error"],
             "interrupted": c["interrupted"], "ts": c["ts"].isoformat()}
            for c in in_seg
        ]
        # U2: verification runs with a resolved outcome (unresolved → no pass/fail).
        verification = [
            {"kind": c["vkind"],
             "passed": c["is_error"] is False and not c["interrupted"],
             "ts": c["ts"].isoformat()}
            for c in in_seg if c["vkind"] and c["is_error"] is not None
        ]
        seg_added = [r for r in added_records if s <= r["ts"] <= e]
        reverted_n = sum(1 for r in seg_added if r["reverted"])
        churn = {
            "added":    len(seg_added),
            "survived": len(seg_added) - reverted_n,
            "reverted": reverted_n,
        }
        records.append({
            "session_id":         session_id,
            "developer_key":      developer_key,
            "agent_kind":         agent_kind,
            "agent_id":           agent_id or session_id,
            "agent_type":         agent_type,
            "workflow_run_id":    workflow_run_id,
            "spawn_tool_use_id":  spawn_tool_use_id,
            "start_ts":           s.isoformat(),
            "end_ts":             e.isoformat(),
            "is_sidechain":       is_sidechain,
            "tool_calls":         seg_calls,
            "verification":       verification,
            "churn":              churn,
            "ended_in_interrupt": any(s <= it <= e for it in interrupt_ts),
        })
    return records


def collect_segment_signals(
    developer_map: list[dict],
    since: datetime | None = None,
) -> list[dict]:
    """Per-segment tool-call signals across all claude dirs — parallel to
    collect_segments(), same three sources and is_sidechain tagging. Carries the
    error / interrupt / target signals the time-only busy segments omit."""
    all_sigs: list[dict] = []
    for dev in developer_map:
        key = dev["developer_key"]
        for claude_dir_str in dev["claude_dirs"]:
            projects_dir = Path(claude_dir_str) / "projects"
            if not projects_dir.exists():
                continue
            for project_dir in projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                for jsonl_file in project_dir.glob("*.jsonl"):
                    if since and _too_old(jsonl_file, since):
                        continue
                    all_sigs.extend(_signals_from_jsonl(
                        jsonl_file, key, jsonl_file.stem, False,
                        agent_kind="main", agent_id=jsonl_file.stem,
                    ))
                for sub_file in project_dir.glob("*/subagents/*.jsonl"):
                    if since and _too_old(sub_file, since):
                        continue
                    meta = _read_agent_meta(sub_file)
                    all_sigs.extend(_signals_from_jsonl(
                        sub_file, key, sub_file.parent.parent.name, True,
                        agent_kind="subagent", agent_id=sub_file.stem,
                        agent_type=meta.get("agentType"),
                        spawn_tool_use_id=meta.get("toolUseId"),
                    ))
                for wf_file in project_dir.glob("*/subagents/workflows/*/*.jsonl"):
                    if wf_file.stem in _WF_SKIP_STEMS:
                        continue
                    if since and _too_old(wf_file, since):
                        continue
                    meta = _read_agent_meta(wf_file)
                    all_sigs.extend(_signals_from_jsonl(
                        wf_file, key, wf_file.parents[3].name, True,
                        agent_kind="workflow", agent_id=wf_file.stem,
                        workflow_run_id=wf_file.parent.name,
                        agent_type=meta.get("agentType"),
                        spawn_tool_use_id=meta.get("toolUseId"),
                    ))
    return all_sigs


def collect(
    developer_map: list[dict],
    processed_sessions: set[str] | None = None,
    since: datetime | None = None,
) -> list[dict]:
    """
    Parse JSONL session files across all claude dirs.
    Skips session_ids already in processed_sessions (incremental).
    """
    processed_sessions = processed_sessions or set()
    all_events = []

    for dev in developer_map:
        key = dev["developer_key"]
        for claude_dir_str in dev["claude_dirs"]:
            claude_dir = Path(claude_dir_str)
            projects_dir = claude_dir / "projects"
            if not projects_dir.exists():
                continue

            for project_dir in projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                for jsonl_file in project_dir.glob("*.jsonl"):
                    session_id = jsonl_file.stem
                    if since:
                        mtime = datetime.fromtimestamp(
                            jsonl_file.stat().st_mtime, tz=timezone.utc
                        )
                        if mtime < since:
                            continue
                        # File was modified within the window: re-parse even if
                        # already pushed — active sessions accumulate new turns.
                    elif session_id in processed_sessions:
                        # No time window: safe to skip fully-pushed sessions.
                        continue
                    events = _process_jsonl(jsonl_file, key)
                    all_events.extend(events)

    return all_events
