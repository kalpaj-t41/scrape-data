"""
Build session_meta-shaped records from JSONL transcripts — the always-present source.

Why: usage-data/session-meta/*.json is optional telemetry, frequently absent (one account
here has none). JSONL (`projects/<encoded>/*.jsonl`) is always written. This collector
reconstructs the same record shape from JSONL so every per-session metric stays populated.

Used as a GAP-FILL union, not a source-switch (see merge_gap_fill): telemetry stays
authoritative where present; these records are added only for sessions telemetry never wrote.
Each record is tagged source='jsonl'. This deliberately avoids re-deriving already-covered
sessions, so historical numbers do not step at cutover.

Sub-agent transcripts (`subagents/*.jsonl`) are NOT sessions and are excluded (non-recursive
glob). Lines they edited are therefore NOT folded into the parent here — a documented
parent-only undercount for delegating sessions (see docs/jsonl-session-index-plan.md R5).
"""

import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from collectors.sessions import _parse_iso, _extract_user_text
from collectors.session_meta import _account_type

_GIT_COMMIT_RE = re.compile(r"\bgit\s+(?:[a-z-]+\s+)*commit\b")
_GIT_PUSH_RE = re.compile(r"\bgit\s+(?:[a-z-]+\s+)*push\b")
_MCP_PREFIX = "mcp__"

# Parse "org/repo" from a git remote URL (git@host:org/repo.git or https://host/org/repo.git).
_REMOTE_RE = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$")
_git_cache: dict[str, tuple[str | None, str | None]] = {}


def _git_org_project(cwd: str | None) -> tuple[str | None, str | None]:
    """(org, project) from the git remote of `cwd`; fallback to the directory layout.

    Run at collect time on the developer's machine (repos present). Cached per path.
    """
    if not cwd:
        return None, None
    if cwd in _git_cache:
        return _git_cache[cwd]
    org = project = None
    try:
        url = subprocess.run(
            ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        m = _REMOTE_RE.search(url) if url else None
        if m:
            org, project = m.group(1), m.group(2)
    except Exception:
        pass
    if not project:                      # fallback: leaf dir = project, parent = org-ish
        p = Path(cwd)
        project = p.name or None
        org = org or (p.parent.name or None)
    _git_cache[cwd] = (org, project)
    return org, project


def _team_label(org: str | None, project: str | None) -> str | None:
    if org and project:
        return f"{org}/{project}"
    return org or project


def _week_date(dt: datetime | None) -> tuple[str | None, str | None]:
    if not dt:
        return None, None
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}", dt.date().isoformat()


def _ext_lang(path: str) -> str | None:
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix or None


class _Acc:
    """Per-session accumulator (one logical session = one sessionId)."""

    def __init__(self, session_id: str, developer_key: str, claude_dir: Path):
        self.session_id = session_id
        self.developer_key = developer_key
        self.claude_dir = claude_dir
        self.first_dt: datetime | None = None
        self.last_dt: datetime | None = None
        self.last_prompt_dt: datetime | None = None
        self.first_prompt = ""
        self.cwd: str | None = None
        self.user_msgs = 0
        self.assistant_msgs = 0
        self.tool_counts: Counter = Counter()
        self.languages: Counter = Counter()
        self.files: set[str] = set()
        self.lines_added = 0
        self.lines_removed = 0
        self.git_commits = 0
        self.git_pushes = 0
        self.tool_errors = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.uses_task_agent = False
        self.uses_mcp = False
        self.uses_web_search = False
        self.uses_web_fetch = False
        self.response_times: list[float] = []
        self._last_assistant_dt: datetime | None = None

    def _touch_time(self, dt: datetime | None):
        if not dt:
            return
        if self.first_dt is None or dt < self.first_dt:
            self.first_dt = dt
        if self.last_dt is None or dt > self.last_dt:
            self.last_dt = dt

    def add_message(self, msg: dict):
        mtype = msg.get("type", "")
        if self.cwd is None and msg.get("cwd"):
            self.cwd = msg.get("cwd")
        ts = _parse_iso(msg.get("timestamp", "")) if msg.get("timestamp") else None
        self._touch_time(ts)

        # structuredPatch line accounting (lives on toolUseResult, any message).
        tur = msg.get("toolUseResult")
        if isinstance(tur, dict):
            fp = tur.get("filePath")
            if fp:
                self.files.add(fp)
                lang = _ext_lang(fp)
                if lang:
                    self.languages[lang] += 1
            for hunk in tur.get("structuredPatch", []) or []:
                for ln in hunk.get("lines", []) or []:
                    if ln.startswith("+"):
                        self.lines_added += 1
                    elif ln.startswith("-"):
                        self.lines_removed += 1
            status = str(tur.get("status", "")).lower()
            if tur.get("is_error") or status in ("error", "failed"):
                self.tool_errors += 1

        if mtype == "user":
            content = msg.get("message", {}).get("content", "")
            # real human prompt = string content or a text block (not tool_result)
            is_human = isinstance(content, str) and content.strip()
            if not is_human and isinstance(content, list):
                is_human = any(
                    isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
                    for b in content
                )
            if is_human:
                self.user_msgs += 1
                if ts and (self.last_prompt_dt is None or ts > self.last_prompt_dt):
                    self.last_prompt_dt = ts
                if not self.first_prompt:
                    self.first_prompt = _extract_user_text(msg)[:500]
                # idle "response time" = gap from last assistant reply to this human prompt
                if ts and self._last_assistant_dt and ts > self._last_assistant_dt:
                    self.response_times.append((ts - self._last_assistant_dt).total_seconds())

        elif mtype == "assistant":
            self.assistant_msgs += 1
            if ts:
                self._last_assistant_dt = ts
            message = msg.get("message", {})
            usage = message.get("usage", {}) or {}
            self.input_tokens += usage.get("input_tokens", 0) or 0
            self.output_tokens += usage.get("output_tokens", 0) or 0
            for block in message.get("content", []) or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name", "")
                self.tool_counts[name] += 1
                if name in ("Agent", "Task"):
                    self.uses_task_agent = True
                elif name == "WebSearch":
                    self.uses_web_search = True
                elif name == "WebFetch":
                    self.uses_web_fetch = True
                elif name.startswith(_MCP_PREFIX):
                    self.uses_mcp = True
                if name == "Bash":
                    cmd = str(block.get("input", {}).get("command", ""))
                    if _GIT_COMMIT_RE.search(cmd):
                        self.git_commits += 1
                    if _GIT_PUSH_RE.search(cmd):
                        self.git_pushes += 1

    def to_record(self) -> dict:
        week, date = _week_date(self.first_dt)
        duration_min = 0
        if self.first_dt and self.last_dt and self.last_dt > self.first_dt:
            duration_min = round((self.last_dt - self.first_dt).total_seconds() / 60, 1)
        org, project = _git_org_project(self.cwd)
        return {
            "session_id": self.session_id,
            "developer_key": self.developer_key,
            "claude_dir": str(self.claude_dir),
            "account_type": _account_type(self.claude_dir),
            "project_path": self.cwd,
            "git_org": org,
            "git_project": project,
            "team": _team_label(org, project),
            "start_time": self.first_dt.isoformat() if self.first_dt else "",
            "start_dt": self.first_dt,
            "week": week,
            "date": date,
            "duration_minutes": duration_min,
            "user_message_count": self.user_msgs,
            "assistant_message_count": self.assistant_msgs,
            "tool_counts": dict(self.tool_counts),
            "languages": dict(self.languages),
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "files_modified": len(self.files),
            "git_commits": self.git_commits,
            "git_pushes": self.git_pushes,
            "first_prompt": self.first_prompt,
            "user_interruptions": 0,            # not reliably in JSONL — approximated (R7)
            "user_response_times": self.response_times,
            "tool_errors": self.tool_errors,
            "uses_task_agent": self.uses_task_agent,
            "uses_mcp": self.uses_mcp,
            "uses_web_search": self.uses_web_search,
            "uses_web_fetch": self.uses_web_fetch,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "source": "jsonl",
            "last_prompt_ts": self.last_prompt_dt.isoformat() if self.last_prompt_dt else None,
        }


def _too_old(path: Path, since: datetime) -> bool:
    try:
        from datetime import timezone
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < since
    except Exception:
        return False


_WF_SKIP_STEMS = {"journal", "history"}


def _latest_prompt_from_jsonl(path: Path) -> tuple[str, datetime | None]:
    latest: datetime | None = None
    session_id = path.stem

    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return session_id, None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue

        session_id = msg.get("sessionId") or session_id
        if msg.get("type") != "user":
            continue

        content = msg.get("message", {}).get("content", "")
        is_human = isinstance(content, str) and content.strip()
        if not is_human and isinstance(content, list):
            is_human = any(
                isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
                for b in content
            )
        if not is_human:
            continue

        ts = _parse_iso(msg.get("timestamp", "")) if msg.get("timestamp") else None
        if ts and (latest is None or ts > latest):
            latest = ts

    return session_id, latest


def collect_latest_prompts(
    developer_map: list[dict],
    since: datetime | None = None,
) -> dict[str, str | None]:
    results: dict[str, str | None] = {}

    for dev in developer_map:
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
                    session_id, latest = _latest_prompt_from_jsonl(jsonl_file)
                    latest_iso = latest.isoformat() if latest else None
                    previous = results.get(session_id)
                    if previous is None or (latest_iso is not None and previous < latest_iso):
                        results[session_id] = latest_iso

    return results


def collect(developer_map: list[dict], since: datetime | None = None) -> list[dict]:
    """Reconstruct session_meta-shaped records from JSONL across all claude dirs.

    Excludes sub-agent transcripts from the main ingestion (non-recursive *.jsonl
    glob) so lines/tokens are not double-counted. A targeted second pass over
    sub-agent files still sets uses_web_fetch / uses_web_search correctly, since
    those tools are only invoked inside delegated sub-agents, never in the parent.
    """
    # session_id -> _Acc (across files, so resumes merge)
    accs: dict[str, _Acc] = {}

    for dev in developer_map:
        key = dev["developer_key"]
        for claude_dir_str in dev["claude_dirs"]:
            projects_dir = Path(claude_dir_str) / "projects"
            if not projects_dir.exists():
                continue
            claude_dir = Path(claude_dir_str)
            for project_dir in projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                # Non-recursive: project-root *.jsonl only — never subagents/*.jsonl.
                for jsonl_file in project_dir.glob("*.jsonl"):
                    if since and _too_old(jsonl_file, since):
                        continue
                    _ingest_file(jsonl_file, key, claude_dir, accs)

                # Second pass: scan sub-agent files only for web-tool flags.
                # WebFetch/WebSearch are invoked by sub-agents, not the parent session.
                _scan_subagents_for_web_tools(project_dir, accs, since)

    return [a.to_record() for a in accs.values() if a.first_dt is not None]


def _ingest_file(path: Path, dev_key: str, claude_dir: Path, accs: dict[str, _Acc]):
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return
    stem = path.stem
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        sid = msg.get("sessionId") or stem
        acc = accs.get(sid)
        if acc is None:
            acc = accs[sid] = _Acc(sid, dev_key, claude_dir)
        acc.add_message(msg)


def _mark_web_tools(path: Path, acc: "_Acc") -> None:
    """Set uses_web_fetch / uses_web_search on acc from one sub-agent file."""
    if acc.uses_web_fetch and acc.uses_web_search:
        return  # already both set — skip
    try:
        for line in path.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("type") != "assistant":
                continue
            for block in msg.get("message", {}).get("content", []) or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name", "")
                if name == "WebSearch":
                    acc.uses_web_search = True
                elif name == "WebFetch":
                    acc.uses_web_fetch = True
            if acc.uses_web_fetch and acc.uses_web_search:
                return
    except Exception:
        pass


def _scan_subagents_for_web_tools(
    project_dir: Path, accs: dict, since: datetime | None
) -> None:
    """Mark web-tool flags on parent session accumulators from sub-agent files."""
    # Agent tool sub-agents: <session>/subagents/<agent>.jsonl
    for sub_file in project_dir.glob("*/subagents/*.jsonl"):
        if since and _too_old(sub_file, since):
            continue
        parent_sid = sub_file.parent.parent.name
        acc = accs.get(parent_sid)
        if acc:
            _mark_web_tools(sub_file, acc)

    # Workflow tool sub-agents: <session>/subagents/workflows/<run-id>/<agent>.jsonl
    for wf_file in project_dir.glob("*/subagents/workflows/*/*.jsonl"):
        if wf_file.stem in _WF_SKIP_STEMS:
            continue
        if since and _too_old(wf_file, since):
            continue
        parent_sid = wf_file.parents[3].name
        acc = accs.get(parent_sid)
        if acc:
            _mark_web_tools(wf_file, acc)


# Fields JSONL can't reliably derive — overlay from telemetry where it exists.
# (Everything else — lines, tools, tokens, duration — comes from JSONL.)
_TELEMETRY_ENRICH_FIELDS = ("user_interruptions",)


def merge_jsonl_primary(jsonl_derived: list[dict], telemetry: list[dict]) -> list[dict]:
    """JSONL is the source of truth; telemetry only fills gaps.

    - Every real session uses its JSONL-derived record (one consistent methodology,
      full coverage — usage-data's gaps no longer drop sessions).
    - For overlapping sessions, a few fields JSONL can't reliably produce
      (`_TELEMETRY_ENRICH_FIELDS`, e.g. user_interruptions) are overlaid from telemetry
      only when JSONL's value is empty — strictly additive, never overrides JSONL.
    - Telemetry-only sessions (usage-data exists but JSONL is gone) are kept as orphans,
      tagged source='telemetry'.

    Note: this is a one-time cutover vs the old telemetry-primary numbers (R2). Intended.
    """
    by_tele = {m.get("session_id"): m for m in telemetry}
    out: list[dict] = []
    seen: set[str] = set()
    for j in jsonl_derived:
        sid = j.get("session_id")
        seen.add(sid)
        t = by_tele.get(sid)
        if t:
            for f in _TELEMETRY_ENRICH_FIELDS:
                if not j.get(f) and t.get(f):
                    j[f] = t[f]
        out.append(j)
    for t in telemetry:
        if t.get("session_id") not in seen:
            t.setdefault("source", "telemetry")
            out.append(t)
    return out


def merge_gap_fill(telemetry: list[dict], jsonl_derived: list[dict]) -> list[dict]:
    """DEPRECATED telemetry-primary union — kept for reference. Use merge_jsonl_primary."""
    out: list[dict] = []
    seen: set[str] = set()
    for m in telemetry:
        m.setdefault("source", "telemetry")
        seen.add(m.get("session_id"))
        out.append(m)
    for s in jsonl_derived:
        if s.get("session_id") not in seen:
            out.append(s)
    return out
