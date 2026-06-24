"""
Central store for raw collected data.

Supports two backends — detected automatically from the connection string:
  SQLite   : pass a Path  e.g. Path("/shared/central.db")
  PostgreSQL: pass a URL  e.g. "postgresql://user:pass@host:5432/dbname"

The public interface (push / pull_raw / pushed_session_ids / stats / close)
is identical for both backends, so push.py and batch_runner.py are unchanged.

PostgreSQL uses a dedicated 'scrape_data' schema with properly typed columns.
SQLite uses a flat layout with JSON blobs (simpler for local use).

Environment variable shortcut:
  POSTGRES_URL=postgresql://...  python push.py --central $POSTGRES_URL
"""

import json
import os
from datetime import datetime, date, timezone
from pathlib import Path


def _dumps(obj) -> str:
    def _default(o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        raise TypeError(f"Not JSON serializable: {type(o).__name__}")
    return json.dumps(obj, default=_default)


def _week_of(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    except Exception:
        return None


def CentralStore(db_path=None):
    """
    Factory — returns the right backend based on db_path type / value.

    db_path can be:
      - None                   → SQLite at ~/.claude-metrics/central.db
      - pathlib.Path           → SQLite at that path
      - str starting postgres  → PostgreSQL
      - str starting file://   → SQLite
      - other str              → treat as SQLite file path
    """
    if db_path is None:
        return _SQLiteStore(Path.home() / ".claude-metrics" / "central.db")

    if isinstance(db_path, Path):
        return _SQLiteStore(db_path)

    s = str(db_path)
    if s.startswith("postgresql://") or s.startswith("postgres://"):
        return _PostgresStore(s)
    if s.startswith("file://"):
        return _SQLiteStore(Path(s[7:]))

    # Any other string is treated as a file path
    return _SQLiteStore(Path(s))


# ── SQLite shared SQL (ANSI-compatible) ───────────────────────────────────────

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS session_metas (
    session_id              TEXT PRIMARY KEY,
    developer_key           TEXT NOT NULL,
    week                    TEXT,
    date                    TEXT,
    pushed_at               TEXT NOT NULL,
    claude_dir              TEXT,
    account_type            TEXT,
    project_path            TEXT,
    git_org                 TEXT,
    git_project             TEXT,
    team                    TEXT,
    source                  TEXT,
    start_time              TEXT,
    start_dt                TEXT,
    duration_minutes        REAL,
    user_message_count      INTEGER DEFAULT 0,
    assistant_message_count INTEGER DEFAULT 0,
    input_tokens            INTEGER DEFAULT 0,
    output_tokens           INTEGER DEFAULT 0,
    lines_added             INTEGER DEFAULT 0,
    lines_removed           INTEGER DEFAULT 0,
    files_modified          INTEGER DEFAULT 0,
    git_commits             INTEGER DEFAULT 0,
    git_pushes              INTEGER DEFAULT 0,
    tool_errors             INTEGER DEFAULT 0,
    user_interruptions      INTEGER DEFAULT 0,
    uses_task_agent         INTEGER DEFAULT 0,
    uses_mcp                INTEGER DEFAULT 0,
    uses_web_search         INTEGER DEFAULT 0,
    uses_web_fetch          INTEGER DEFAULT 0,
    first_prompt            TEXT,
    ai_title                TEXT,
    tool_counts             TEXT DEFAULT '{}',
    languages               TEXT DEFAULT '{}',
    user_response_times     TEXT DEFAULT '[]',
    agent_names             TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS turn_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id              TEXT NOT NULL,
    developer_key           TEXT,
    pushed_at               TEXT NOT NULL,
    event_type              TEXT DEFAULT 'turn',
    user_ts                 TEXT,
    assistant_ts            TEXT,
    agent_ms                REAL,
    is_sidechain            INTEGER DEFAULT 0,
    permission_mode         TEXT,
    tool_uses               TEXT DEFAULT '[]',
    agent_colors_in_session INTEGER DEFAULT 0,
    command                 TEXT,
    prompt_text             TEXT
);
CREATE TABLE IF NOT EXISTS facets (
    session_id    TEXT PRIMARY KEY,
    developer_key TEXT,
    pushed_at     TEXT NOT NULL,
    data          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL,
    developer_key    TEXT,
    pushed_at        TEXT NOT NULL,
    task_id          TEXT,
    agent_name       TEXT,
    task_description TEXT,
    status           TEXT,
    enqueued_at      TEXT,
    week             TEXT
);
CREATE TABLE IF NOT EXISTS background_tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    developer_key TEXT,
    pushed_at     TEXT NOT NULL,
    enqueued_at   TEXT,
    week          TEXT
);
CREATE TABLE IF NOT EXISTS app_state (
    developer_key TEXT PRIMARY KEY,
    pushed_at     TEXT NOT NULL,
    data          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id            TEXT PRIMARY KEY,
    developer_key TEXT NOT NULL,
    pushed_at     TEXT NOT NULL,
    data          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS developers (
    developer_key TEXT PRIMARY KEY,
    name          TEXT,
    email         TEXT,
    claude_dirs   TEXT,
    pushed_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sm_dev   ON session_metas(developer_key);
CREATE INDEX IF NOT EXISTS idx_sm_week  ON session_metas(week);
CREATE INDEX IF NOT EXISTS idx_sm_date  ON session_metas(date);
CREATE INDEX IF NOT EXISTS idx_te_sid   ON turn_events(session_id);
CREATE INDEX IF NOT EXISTS idx_te_dev   ON turn_events(developer_key);
CREATE INDEX IF NOT EXISTS idx_at_sid   ON agent_tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_at_dev   ON agent_tasks(developer_key);
CREATE INDEX IF NOT EXISTS idx_at_week  ON agent_tasks(week);
CREATE INDEX IF NOT EXISTS idx_at_name  ON agent_tasks(agent_name);
CREATE INDEX IF NOT EXISTS idx_bt_sid   ON background_tasks(session_id);
CREATE TABLE IF NOT EXISTS busy_segments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    developer_key TEXT NOT NULL,
    start_ts      TEXT,
    end_ts        TEXT,
    is_sidechain  INTEGER DEFAULT 0,
    week          TEXT
);
CREATE INDEX IF NOT EXISTS idx_bs_sid ON busy_segments(session_id);
CREATE INDEX IF NOT EXISTS idx_bs_dev ON busy_segments(developer_key);
CREATE INDEX IF NOT EXISTS idx_bs_week ON busy_segments(week);
CREATE TABLE IF NOT EXISTS segment_signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    developer_key TEXT NOT NULL,
    start_ts      TEXT,
    week          TEXT,
    pushed_at     TEXT NOT NULL,
    data          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ss_sid  ON segment_signals(session_id);
CREATE INDEX IF NOT EXISTS idx_ss_dev  ON segment_signals(developer_key);
CREATE INDEX IF NOT EXISTS idx_ss_week ON segment_signals(week)
"""


# ── PostgreSQL scrape_data schema DDL ─────────────────────────────────────────

_PG_CREATE = """
CREATE SCHEMA IF NOT EXISTS scrape_data;

CREATE TABLE IF NOT EXISTS scrape_data.session_metas (
    session_id              TEXT PRIMARY KEY,
    developer_key           TEXT NOT NULL,
    claude_dir              TEXT,
    account_type            TEXT,
    project_path            TEXT,
    start_time              TIMESTAMPTZ,
    week                    TEXT,
    date                    DATE,
    duration_minutes        INTEGER,
    user_message_count      INTEGER,
    assistant_message_count INTEGER,
    lines_added             INTEGER DEFAULT 0,
    lines_removed           INTEGER DEFAULT 0,
    files_modified          INTEGER DEFAULT 0,
    git_commits             INTEGER DEFAULT 0,
    git_pushes              INTEGER DEFAULT 0,
    first_prompt            TEXT,
    user_interruptions      INTEGER DEFAULT 0,
    tool_errors             INTEGER DEFAULT 0,
    uses_task_agent         BOOLEAN DEFAULT FALSE,
    uses_mcp                BOOLEAN DEFAULT FALSE,
    uses_web_search         BOOLEAN DEFAULT FALSE,
    uses_web_fetch          BOOLEAN DEFAULT FALSE,
    input_tokens            INTEGER DEFAULT 0,
    output_tokens           INTEGER DEFAULT 0,
    tool_counts             JSONB DEFAULT '{}',
    languages               JSONB DEFAULT '{}',
    user_response_times     JSONB DEFAULT '[]',
    pushed_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scrape_data.turn_events (
    id                      BIGSERIAL,
    session_id              TEXT NOT NULL,
    developer_key           TEXT NOT NULL,
    event_type              TEXT NOT NULL DEFAULT 'turn',
    user_ts                 TIMESTAMPTZ,
    assistant_ts            TIMESTAMPTZ,
    agent_ms                NUMERIC(12,1),
    is_sidechain            BOOLEAN DEFAULT FALSE,
    permission_mode         TEXT,
    tool_uses               JSONB DEFAULT '[]',
    agent_colors_in_session INTEGER DEFAULT 0,
    command                 TEXT,
    prompt_text             TEXT,
    pushed_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (session_id, id)
);

ALTER TABLE scrape_data.turn_events ADD COLUMN IF NOT EXISTS prompt_text TEXT;

CREATE TABLE IF NOT EXISTS scrape_data.facets (
    session_id          TEXT PRIMARY KEY,
    developer_key       TEXT,
    underlying_goal     TEXT,
    goal_categories     JSONB DEFAULT '{}',
    outcome             TEXT,
    session_type        TEXT,
    claude_helpfulness  TEXT,
    friction_counts     JSONB DEFAULT '{}',
    friction_detail     TEXT,
    primary_success     TEXT,
    brief_summary       TEXT,
    pushed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scrape_data.app_state (
    developer_key            TEXT PRIMARY KEY,
    total_startups           INTEGER DEFAULT 0,
    has_used_background_task BOOLEAN DEFAULT FALSE,
    install_methods          JSONB DEFAULT '[]',
    accounts                 JSONB DEFAULT '[]',
    pushed_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scrape_data.plans (
    developer_key            TEXT PRIMARY KEY,
    total_plans              INTEGER DEFAULT 0,
    new_plans_since_last_run INTEGER DEFAULT 0,
    plan_names               JSONB DEFAULT '[]',
    pushed_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE scrape_data.session_metas ADD COLUMN IF NOT EXISTS ai_title    TEXT;
ALTER TABLE scrape_data.session_metas ADD COLUMN IF NOT EXISTS agent_names JSONB DEFAULT '[]';
ALTER TABLE scrape_data.session_metas ADD COLUMN IF NOT EXISTS source      TEXT;

CREATE TABLE IF NOT EXISTS scrape_data.agent_tasks (
    id               BIGSERIAL PRIMARY KEY,
    session_id       TEXT NOT NULL,
    developer_key    TEXT NOT NULL,
    task_id          TEXT,
    agent_name       TEXT,
    task_description TEXT,
    status           TEXT,
    enqueued_at      TIMESTAMPTZ,
    week             TEXT,
    pushed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scrape_data.background_tasks (
    id            BIGSERIAL PRIMARY KEY,
    session_id    TEXT NOT NULL,
    developer_key TEXT NOT NULL,
    enqueued_at   TIMESTAMPTZ,
    week          TEXT,
    pushed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bt_sid ON scrape_data.background_tasks(session_id);

CREATE INDEX IF NOT EXISTS idx_sm_dev   ON scrape_data.session_metas(developer_key);
CREATE INDEX IF NOT EXISTS idx_sm_week  ON scrape_data.session_metas(week);
CREATE INDEX IF NOT EXISTS idx_sm_date  ON scrape_data.session_metas(date);
CREATE INDEX IF NOT EXISTS idx_te_sid   ON scrape_data.turn_events(session_id);
CREATE INDEX IF NOT EXISTS idx_te_dev   ON scrape_data.turn_events(developer_key);
CREATE INDEX IF NOT EXISTS idx_te_etype ON scrape_data.turn_events(event_type);
CREATE INDEX IF NOT EXISTS idx_at_sid   ON scrape_data.agent_tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_at_dev   ON scrape_data.agent_tasks(developer_key);
CREATE INDEX IF NOT EXISTS idx_at_week  ON scrape_data.agent_tasks(week);
CREATE INDEX IF NOT EXISTS idx_at_name  ON scrape_data.agent_tasks(agent_name);

CREATE TABLE IF NOT EXISTS scrape_data.developers (
    developer_key TEXT PRIMARY KEY,
    name          TEXT,
    email         TEXT,
    claude_dirs   JSONB DEFAULT '[]',
    pushed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scrape_data.busy_segments (
    id            BIGSERIAL PRIMARY KEY,
    session_id    TEXT NOT NULL,
    developer_key TEXT NOT NULL,
    start_ts      TIMESTAMPTZ,
    end_ts        TIMESTAMPTZ,
    is_sidechain  BOOLEAN DEFAULT FALSE,
    week          TEXT,
    pushed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bs_sid  ON scrape_data.busy_segments(session_id);
CREATE INDEX IF NOT EXISTS idx_bs_dev  ON scrape_data.busy_segments(developer_key);
CREATE INDEX IF NOT EXISTS idx_bs_week ON scrape_data.busy_segments(week);

CREATE TABLE IF NOT EXISTS scrape_data.segment_signals (
    id            BIGSERIAL PRIMARY KEY,
    session_id    TEXT NOT NULL,
    developer_key TEXT NOT NULL,
    start_ts      TIMESTAMPTZ,
    week          TEXT,
    data          JSONB NOT NULL,
    pushed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ss_sid  ON scrape_data.segment_signals(session_id);
CREATE INDEX IF NOT EXISTS idx_ss_dev  ON scrape_data.segment_signals(developer_key);
CREATE INDEX IF NOT EXISTS idx_ss_week ON scrape_data.segment_signals(week)
"""

_SM_COLS = [
    "session_id", "developer_key", "claude_dir", "account_type", "project_path",
    "start_time", "week", "date", "duration_minutes", "user_message_count",
    "assistant_message_count", "lines_added", "lines_removed", "files_modified",
    "git_commits", "git_pushes", "first_prompt", "user_interruptions", "tool_errors",
    "uses_task_agent", "uses_mcp", "uses_web_search", "uses_web_fetch",
    "input_tokens", "output_tokens", "tool_counts", "languages", "user_response_times",
    "ai_title", "agent_names",
]

_TE_COLS = [
    "session_id", "developer_key", "event_type", "user_ts", "assistant_ts",
    "agent_ms", "is_sidechain", "permission_mode", "tool_uses",
    "agent_colors_in_session", "command", "prompt_text",
]

_F_COLS = [
    "session_id", "developer_key", "underlying_goal", "goal_categories",
    "outcome", "session_type", "claude_helpfulness", "friction_counts",
    "friction_detail", "primary_success", "brief_summary",
]


class _BaseStore:
    """Shared push / pull / stats logic for SQLite — backends supply _execute / _commit / _fetchall."""

    def push(self, raw: dict, force: bool = False) -> dict:
        now = datetime.now(tz=timezone.utc).isoformat()
        inserted = {
            "session_metas": 0, "turn_events": 0, "facets": 0,
            "app_state": 0, "plans": 0, "agent_tasks": 0,
            "background_tasks": 0, "busy_segments": 0, "segment_signals": 0,
        }

        for meta in raw.get("session_metas", []):
            sid = meta.get("session_id")
            if not sid:
                continue
            sm_row = {
                "session_id":              sid,
                "developer_key":           meta.get("developer_key", ""),
                "week":                    meta.get("week"),
                "date":                    meta.get("date"),
                "pushed_at":               now,
                "claude_dir":              meta.get("claude_dir"),
                "account_type":            meta.get("account_type"),
                "project_path":            meta.get("project_path"),
                "git_org":                 meta.get("git_org"),
                "git_project":             meta.get("git_project"),
                "team":                    meta.get("team"),
                "source":                  meta.get("source"),
                "start_time":              meta.get("start_time"),
                "start_dt":                meta.get("start_dt"),
                "duration_minutes":        meta.get("duration_minutes"),
                "user_message_count":      meta.get("user_message_count", 0) or 0,
                "assistant_message_count": meta.get("assistant_message_count", 0) or 0,
                "input_tokens":            meta.get("input_tokens", 0) or 0,
                "output_tokens":           meta.get("output_tokens", 0) or 0,
                "lines_added":             meta.get("lines_added", 0) or 0,
                "lines_removed":           meta.get("lines_removed", 0) or 0,
                "files_modified":          meta.get("files_modified", 0) or 0,
                "git_commits":             meta.get("git_commits", 0) or 0,
                "git_pushes":              meta.get("git_pushes", 0) or 0,
                "tool_errors":             meta.get("tool_errors", 0) or 0,
                "user_interruptions":      meta.get("user_interruptions", 0) or 0,
                "uses_task_agent":         int(bool(meta.get("uses_task_agent", False))),
                "uses_mcp":                int(bool(meta.get("uses_mcp", False))),
                "uses_web_search":         int(bool(meta.get("uses_web_search", False))),
                "uses_web_fetch":          int(bool(meta.get("uses_web_fetch", False))),
                "first_prompt":            meta.get("first_prompt"),
                "ai_title":                meta.get("ai_title"),
                "tool_counts":             _dumps(meta.get("tool_counts") or {}),
                "languages":               _dumps(meta.get("languages") or {}),
                "user_response_times":     _dumps(meta.get("user_response_times") or []),
                "agent_names":             _dumps(meta.get("agent_names") or []),
            }
            # Always upsert — push.py sends refreshed sessions (web flags, agent_names
            # can change after the first push as sub-agents run in active sessions).
            n = self._upsert_replace("session_metas", sm_row, conflict_col="session_id")
            inserted["session_metas"] += n

        # turn_events — one row per turn; always replace all turns for sessions in this push
        turn_sids = {t.get("session_id") for t in raw.get("turn_events", []) if t.get("session_id")}
        for sid in turn_sids:
            self._execute("DELETE FROM turn_events WHERE session_id = {p}", (sid,))
        for t in raw.get("turn_events", []):
            sid = t.get("session_id", "")
            if not sid:
                continue
            self._execute(
                "INSERT INTO turn_events "
                "(session_id, developer_key, pushed_at, event_type, user_ts, assistant_ts, "
                "agent_ms, is_sidechain, permission_mode, tool_uses, agent_colors_in_session, "
                "command, prompt_text) "
                "VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})",
                (sid, t.get("developer_key", ""), now,
                 t.get("event_type", "turn"), t.get("user_ts"), t.get("assistant_ts"),
                 t.get("agent_ms"), 1 if t.get("is_sidechain") else 0,
                 t.get("permission_mode"), _dumps(t.get("tool_uses") or []),
                 t.get("agent_colors_in_session", 0), t.get("command"), t.get("prompt_text")),
            )
            inserted["turn_events"] += 1

        for sid, f in raw.get("facets", {}).items():
            n = self._upsert_ignore(
                "INSERT INTO facets (session_id, developer_key, pushed_at, data) "
                "VALUES ({p},{p},{p},{p})",
                (sid, f.get("developer_key", ""), now, _dumps(f)),
            )
            inserted["facets"] += n

        for dev_key, state in raw.get("app_state", {}).items():
            n = self._upsert_replace(
                "app_state",
                {"developer_key": dev_key, "pushed_at": now, "data": _dumps(state)},
                conflict_col="developer_key",
            )
            inserted["app_state"] += n

        for dev_key, plan_info in raw.get("plans", {}).items():
            n = self._upsert_replace(
                "plans",
                {"id": dev_key, "developer_key": dev_key, "pushed_at": now, "data": _dumps(plan_info)},
                conflict_col="id",
            )
            inserted["plans"] += n

        # agent_tasks — one row per named task; always replace all rows for sessions in this push
        for sid, at in raw.get("agent_tasks", {}).items():
            if not sid:
                continue
            dev_key = at.get("developer_key", "")
            self._execute("DELETE FROM agent_tasks WHERE session_id = {p}", (sid,))
            self._execute("DELETE FROM background_tasks WHERE session_id = {p}", (sid,))
            for task in at.get("tasks", []):
                self._execute(
                    "INSERT INTO agent_tasks "
                    "(session_id, developer_key, pushed_at, task_id, agent_name, "
                    "task_description, status, enqueued_at, week) "
                    "VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p})",
                    (sid, dev_key, now, task.get("task_id"), task.get("agent_name"),
                     task.get("task_description"), task.get("status"),
                     task.get("enqueued_at"), task.get("week")),
                )
                inserted["agent_tasks"] += 1
            for bt in at.get("background_tasks", []):
                self._execute(
                    "INSERT INTO background_tasks "
                    "(session_id, developer_key, pushed_at, enqueued_at, week) "
                    "VALUES ({p},{p},{p},{p},{p})",
                    (sid, dev_key, now, bt.get("enqueued_at"), bt.get("week")),
                )
                inserted["background_tasks"] += 1
            # Back-populate ai_title and agent_names into session_metas
            ai_title    = at.get("ai_title")
            agent_names = at.get("agent_names", [])
            if ai_title or agent_names:
                self._execute(
                    "UPDATE session_metas SET ai_title={p}, agent_names={p} WHERE session_id={p}",
                    (ai_title, _dumps(agent_names), sid),
                )

        # busy_segments — session-level replace (JSONL reparse is authoritative)
        seg_sids = {s.get("session_id") for s in raw.get("busy_segments", []) if s.get("session_id")}
        for sid in seg_sids:
            self._execute("DELETE FROM busy_segments WHERE session_id = {p}", (sid,))
        for s in raw.get("busy_segments", []):
            sid = s.get("session_id")
            if not sid:
                continue
            self._execute(
                "INSERT INTO busy_segments "
                "(session_id, developer_key, start_ts, end_ts, is_sidechain, week) "
                "VALUES ({p},{p},{p},{p},{p},{p})",
                (sid, s.get("developer_key", ""), s.get("start_ts"), s.get("end_ts"),
                 1 if s.get("is_sidechain") else 0, _week_of(s.get("start_ts"))),
            )
            inserted["busy_segments"] += 1

        # segment_signals — session-level replace (quality-layer backbone: per-segment
        # tool-call / verification / churn signals; nested lists stored as JSON blob).
        sig_sids = {s.get("session_id") for s in raw.get("segment_signals", []) if s.get("session_id")}
        for sid in sig_sids:
            self._execute("DELETE FROM segment_signals WHERE session_id = {p}", (sid,))
        for s in raw.get("segment_signals", []):
            sid = s.get("session_id")
            if not sid:
                continue
            self._execute(
                "INSERT INTO segment_signals "
                "(session_id, developer_key, start_ts, week, pushed_at, data) "
                "VALUES ({p},{p},{p},{p},{p},{p})",
                (sid, s.get("developer_key", ""), s.get("start_ts"),
                 _week_of(s.get("start_ts")), now, _dumps(s)),
            )
            inserted["segment_signals"] += 1

        self._commit()
        return inserted

    def upsert_developers(self, developer_map: list[dict]) -> int:
        import json as _json
        now = datetime.now(tz=timezone.utc).isoformat()
        count = 0
        for dev in developer_map:
            key = dev.get("developer_key")
            if not key:
                continue
            n = self._upsert_replace(
                "developers",
                {
                    "developer_key": key,
                    "name":          dev.get("name"),
                    "email":         dev.get("email"),
                    "claude_dirs":   _json.dumps(dev.get("claude_dirs", [])),
                    "pushed_at":     now,
                },
                conflict_col="developer_key",
            )
            count += n
        self._commit()
        return count

    def pushed_session_ids(self) -> set[str]:
        rows = self._fetchall("SELECT session_id FROM session_metas")
        return {r[0] for r in rows}

    def developer_names(self) -> dict[str, str]:
        """{developer_key: name} for report display (name, not the hash id)."""
        return {r[0]: r[1] for r in self._fetchall(
            "SELECT developer_key, name FROM developers") if r[1]}

    def pushed_turn_session_ids(self) -> set[str]:
        return set()  # SQLite — collect all sessions each run

    def pushed_agent_session_ids(self) -> set[str]:
        rows = self._fetchall("SELECT DISTINCT session_id FROM agent_tasks")
        return {r[0] for r in rows}

    def pull_raw(self, since: datetime | None = None) -> dict:
        since_date = since.date().isoformat() if since else None

        _SM_FIELDS = (
            "session_id", "developer_key", "week", "date", "claude_dir", "account_type",
            "project_path", "git_org", "git_project", "team", "source", "start_time",
            "start_dt", "duration_minutes", "user_message_count", "assistant_message_count",
            "input_tokens", "output_tokens", "lines_added", "lines_removed", "files_modified",
            "git_commits", "git_pushes", "tool_errors", "user_interruptions",
            "uses_task_agent", "uses_mcp", "uses_web_search", "uses_web_fetch",
            "first_prompt", "ai_title", "tool_counts", "languages", "user_response_times",
            "agent_names",
        )
        sel = ", ".join(_SM_FIELDS)
        if since_date:
            rows = self._fetchall(
                f"SELECT {sel} FROM session_metas WHERE date >= {{p}} OR date IS NULL",
                (since_date,),
            )
        else:
            rows = self._fetchall(f"SELECT {sel} FROM session_metas")

        session_metas = []
        for r in rows:
            d = dict(zip(_SM_FIELDS, r))
            for jf in ("tool_counts", "languages", "user_response_times", "agent_names"):
                raw_val = d.get(jf)
                d[jf] = json.loads(raw_val) if raw_val else ({} if jf in ("tool_counts", "languages") else [])
            d["uses_task_agent"] = bool(d.get("uses_task_agent"))
            d["uses_mcp"]        = bool(d.get("uses_mcp"))
            d["uses_web_search"] = bool(d.get("uses_web_search"))
            d["uses_web_fetch"]  = bool(d.get("uses_web_fetch"))
            session_metas.append(d)

        in_scope = {m["session_id"] for m in session_metas}
        turn_events: list[dict] = []
        facets: dict[str, dict] = {}

        if in_scope:
            ph = ",".join(["{p}"] * len(in_scope))
            args = tuple(in_scope)

            _TE_FIELDS = (
                "session_id", "developer_key", "event_type", "user_ts", "assistant_ts",
                "agent_ms", "is_sidechain", "permission_mode", "tool_uses",
                "agent_colors_in_session", "command", "prompt_text",
            )
            te_sel = ", ".join(_TE_FIELDS)
            for r in self._fetchall(
                f"SELECT {te_sel} FROM turn_events WHERE session_id IN ({ph})", args
            ):
                d = dict(zip(_TE_FIELDS, r))
                d["is_sidechain"] = bool(d.get("is_sidechain"))
                raw_tu = d.get("tool_uses")
                d["tool_uses"] = json.loads(raw_tu) if raw_tu else []
                turn_events.append(d)

            for r in self._fetchall(f"SELECT session_id, data FROM facets WHERE session_id IN ({ph})", args):
                facets[r[0]] = json.loads(r[1])

        rows = self._fetchall("SELECT developer_key, data FROM app_state")
        app_state = {r[0]: json.loads(r[1]) for r in rows}

        rows = self._fetchall("SELECT developer_key, data FROM plans")
        plans = {r[0]: json.loads(r[1]) for r in rows}

        # Reconstruct agent_tasks: {session_id: {session_id, developer_key, ai_title, agent_names, tasks, background_tasks}}
        # agent_tasks/background_tasks not gated by in_scope — JSONL-only sessions still count.
        agent_tasks: dict[str, dict] = {}

        # Seed ai_title and agent_names from session_metas columns
        for sm in session_metas:
            sid = sm["session_id"]
            if sm.get("ai_title") or sm.get("agent_names"):
                agent_tasks[sid] = {
                    "session_id":       sid,
                    "developer_key":    sm.get("developer_key", ""),
                    "ai_title":         sm.get("ai_title"),
                    "agent_names":      list(sm.get("agent_names") or []),
                    "tasks":            [],
                    "background_tasks": [],
                }

        # agent_tasks and background_tasks: not gated by in_scope (JSONL-only sessions
        # still count for harness M7), but DO respect the since window.
        _at_filter  = ("WHERE enqueued_at >= {p} OR enqueued_at IS NULL", (since_date,)) \
                      if since_date else ("", ())
        for r in self._fetchall(
            "SELECT session_id, developer_key, task_id, agent_name, task_description, "
            f"status, enqueued_at, week FROM agent_tasks {_at_filter[0]}",
            _at_filter[1],
        ):
            sid = r[0]
            if sid not in agent_tasks:
                agent_tasks[sid] = {
                    "session_id": sid, "developer_key": r[1],
                    "ai_title": None, "agent_names": [], "tasks": [], "background_tasks": [],
                }
            task = {
                "task_id":          r[2],
                "agent_name":       r[3],
                "task_description": r[4],
                "status":           r[5],
                "enqueued_at":      r[6],
                "week":             r[7],
            }
            agent_tasks[sid]["tasks"].append(task)
            if r[3] and r[3] not in agent_tasks[sid]["agent_names"]:
                agent_tasks[sid]["agent_names"].append(r[3])

        _bt_filter = ("WHERE enqueued_at >= {p} OR enqueued_at IS NULL", (since_date,)) \
                     if since_date else ("", ())
        for r in self._fetchall(
            f"SELECT session_id, enqueued_at, week FROM background_tasks {_bt_filter[0]}",
            _bt_filter[1],
        ):
            sid = r[0]
            if sid not in agent_tasks:
                agent_tasks[sid] = {
                    "session_id": sid, "developer_key": "",
                    "ai_title": None, "agent_names": [], "tasks": [], "background_tasks": [],
                }
            agent_tasks[sid]["background_tasks"].append({"enqueued_at": r[1], "week": r[2]})

        # busy_segments: not gated by in_scope (JSONL-only sessions still count for
        # agent_hours), but DO respect the since window so --since 1d vs 7d differ.
        busy_segments: list[dict] = []
        _seg_sql = (
            "SELECT session_id, developer_key, start_ts, end_ts, is_sidechain "
            "FROM busy_segments WHERE start_ts >= {p}"
            if since_date else
            "SELECT session_id, developer_key, start_ts, end_ts, is_sidechain "
            "FROM busy_segments"
        )
        _seg_args = (since_date,) if since_date else ()
        for r in self._fetchall(_seg_sql, _seg_args):
            busy_segments.append({
                "session_id":    r[0],
                "developer_key": r[1],
                "start_ts":      r[2],
                "end_ts":        r[3],
                "is_sidechain":  bool(r[4]),
            })

        # segment_signals: quality-layer backbone; respect the since window like
        # busy_segments. The full per-segment record is stored as a JSON blob.
        segment_signals: list[dict] = []
        _sig_sql = (
            "SELECT data FROM segment_signals WHERE start_ts >= {p}"
            if since_date else
            "SELECT data FROM segment_signals"
        )
        _sig_args = (since_date,) if since_date else ()
        for r in self._fetchall(_sig_sql, _sig_args):
            if r[0]:
                segment_signals.append(json.loads(r[0]))

        return {
            "session_metas": session_metas,
            "turn_events":   turn_events,
            "facets":        facets,
            "app_state":     app_state,
            "plans":         plans,
            "agent_tasks":   agent_tasks,
            "busy_segments": busy_segments,
            "segment_signals": segment_signals,
        }

    def stats(self) -> dict:
        return {
            "session_metas":    self._fetchall("SELECT COUNT(*) FROM session_metas")[0][0],
            "turn_events":      self._fetchall("SELECT COUNT(*) FROM turn_events")[0][0],
            "facets":           self._fetchall("SELECT COUNT(*) FROM facets")[0][0],
            "app_state":        self._fetchall("SELECT COUNT(*) FROM app_state")[0][0],
            "plans":            self._fetchall("SELECT COUNT(*) FROM plans")[0][0],
            "agent_tasks":      self._fetchall("SELECT COUNT(*) FROM agent_tasks")[0][0],
            "background_tasks": self._fetchall("SELECT COUNT(*) FROM background_tasks")[0][0],
            "busy_segments":    self._fetchall("SELECT COUNT(*) FROM busy_segments")[0][0],
            "segment_signals":  self._fetchall("SELECT COUNT(*) FROM segment_signals")[0][0],
            "developers":       self._fetchall(
                "SELECT COUNT(DISTINCT developer_key) FROM session_metas"
            )[0][0],
        }


# ── SQLite backend ────────────────────────────────────────────────────────────

class _SQLiteStore(_BaseStore):
    def __init__(self, db_path: Path):
        import sqlite3
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()

        # Detect old blob schema: session_metas has a 'data' column
        _old = False
        try:
            self._conn.execute("SELECT data FROM session_metas LIMIT 0")
            _old = True
        except Exception:
            pass

        if _old:
            self._migrate_from_blob_schema()
        else:
            for stmt in _CREATE_TABLES.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    self._conn.execute(stmt)
            self._conn.commit()

    def _fmt(self, sql: str) -> str:
        return sql.replace("{p}", "?")

    def _execute(self, sql: str, params=()):
        return self._conn.execute(self._fmt(sql), params)

    def _fetchall(self, sql: str, params=()):
        return self._execute(sql, params).fetchall()

    def _commit(self):
        self._conn.commit()

    def _upsert_ignore(self, sql: str, params) -> int:
        sql = "INSERT OR IGNORE " + sql.removeprefix("INSERT ").replace("{p}", "?")
        cur = self._conn.execute(sql, params)
        return cur.rowcount

    def _upsert_replace(self, table: str, row: dict, conflict_col: str) -> int:
        cols = ", ".join(row.keys())
        vals = tuple(row.values())
        ph   = ", ".join(["?"] * len(vals))
        cur  = self._conn.execute(
            f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({ph})", vals
        )
        return cur.rowcount

    def _migrate_from_blob_schema(self) -> None:
        """One-time migration from JSON-blob schema to normalized columns."""
        import logging as _log
        _log.getLogger(__name__).info(
            "central.db: migrating from blob schema to normalized columns…"
        )

        # 1. Read all data from old tables into memory
        old_sm: list[dict] = []
        try:
            for r in self._conn.execute(
                "SELECT data, pushed_at FROM session_metas"
            ).fetchall():
                d = json.loads(r[0])
                d.setdefault("pushed_at", r[1])
                old_sm.append(d)
        except Exception:
            pass

        old_te: dict = {}
        try:
            for r in self._conn.execute(
                "SELECT session_id, developer_key, pushed_at, data FROM turn_events"
            ).fetchall():
                old_te[r[0]] = {
                    "developer_key": r[1], "pushed_at": r[2],
                    "turns": json.loads(r[3]),
                }
        except Exception:
            pass

        old_at: dict = {}
        try:
            for r in self._conn.execute(
                "SELECT session_id, developer_key, pushed_at, data FROM agent_tasks"
            ).fetchall():
                d = json.loads(r[3])
                d["_pushed_at"] = r[2]
                d["developer_key"] = r[1]
                old_at[r[0]] = d
        except Exception:
            pass

        # 2. Drop old tables so CREATE TABLE IF NOT EXISTS picks up new DDL
        for tbl in ("session_metas", "turn_events", "agent_tasks"):
            self._conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        self._conn.commit()

        # 3. Create new normalized tables
        for stmt in _CREATE_TABLES.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                self._conn.execute(stmt)
        self._conn.commit()

        now = datetime.now(tz=timezone.utc).isoformat()

        # 4. Re-insert session_metas
        for d in old_sm:
            self._conn.execute(
                "INSERT OR IGNORE INTO session_metas "
                "(session_id, developer_key, week, date, pushed_at, claude_dir, account_type, "
                "project_path, git_org, git_project, team, source, start_time, start_dt, "
                "duration_minutes, user_message_count, assistant_message_count, "
                "input_tokens, output_tokens, lines_added, lines_removed, files_modified, "
                "git_commits, git_pushes, tool_errors, user_interruptions, "
                "uses_task_agent, uses_mcp, uses_web_search, uses_web_fetch, "
                "first_prompt, ai_title, tool_counts, languages, user_response_times, agent_names) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    d.get("session_id"), d.get("developer_key", ""),
                    d.get("week"), d.get("date"), d.get("pushed_at", now),
                    d.get("claude_dir"), d.get("account_type"), d.get("project_path"),
                    d.get("git_org"), d.get("git_project"), d.get("team"), d.get("source"),
                    d.get("start_time"), d.get("start_dt"), d.get("duration_minutes"),
                    d.get("user_message_count", 0) or 0,
                    d.get("assistant_message_count", 0) or 0,
                    d.get("input_tokens", 0) or 0, d.get("output_tokens", 0) or 0,
                    d.get("lines_added", 0) or 0, d.get("lines_removed", 0) or 0,
                    d.get("files_modified", 0) or 0, d.get("git_commits", 0) or 0,
                    d.get("git_pushes", 0) or 0, d.get("tool_errors", 0) or 0,
                    d.get("user_interruptions", 0) or 0,
                    int(bool(d.get("uses_task_agent", False))),
                    int(bool(d.get("uses_mcp", False))),
                    int(bool(d.get("uses_web_search", False))),
                    int(bool(d.get("uses_web_fetch", False))),
                    d.get("first_prompt"), d.get("ai_title"),
                    _dumps(d.get("tool_counts") or {}),
                    _dumps(d.get("languages") or {}),
                    _dumps(d.get("user_response_times") or []),
                    _dumps(d.get("agent_names") or []),
                ),
            )

        # 5. Re-insert turn_events (one row per turn)
        for sid, te_data in old_te.items():
            pushed_at = te_data.get("pushed_at", now)
            dev_key   = te_data.get("developer_key", "")
            for t in te_data.get("turns", []):
                self._conn.execute(
                    "INSERT INTO turn_events "
                    "(session_id, developer_key, pushed_at, event_type, user_ts, assistant_ts, "
                    "agent_ms, is_sidechain, permission_mode, tool_uses, agent_colors_in_session, "
                    "command, prompt_text) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        t.get("session_id", sid),
                        t.get("developer_key", dev_key),
                        pushed_at,
                        t.get("event_type", "turn"),
                        t.get("user_ts"), t.get("assistant_ts"), t.get("agent_ms"),
                        1 if t.get("is_sidechain") else 0,
                        t.get("permission_mode"),
                        _dumps(t.get("tool_uses") or []),
                        t.get("agent_colors_in_session", 0),
                        t.get("command"), t.get("prompt_text"),
                    ),
                )

        # 6. Re-insert agent_tasks and background_tasks
        for sid, at_data in old_at.items():
            pushed_at = at_data.get("_pushed_at", now)
            dev_key   = at_data.get("developer_key", "")
            for task in at_data.get("tasks", []):
                self._conn.execute(
                    "INSERT INTO agent_tasks "
                    "(session_id, developer_key, pushed_at, task_id, agent_name, "
                    "task_description, status, enqueued_at, week) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        sid, dev_key, pushed_at,
                        task.get("task_id"), task.get("agent_name"),
                        task.get("task_description"), task.get("status"),
                        task.get("enqueued_at"), task.get("week"),
                    ),
                )
            for bt in at_data.get("background_tasks", []):
                self._conn.execute(
                    "INSERT INTO background_tasks "
                    "(session_id, developer_key, pushed_at, enqueued_at, week) "
                    "VALUES (?,?,?,?,?)",
                    (sid, dev_key, pushed_at, bt.get("enqueued_at"), bt.get("week")),
                )
            ai_title    = at_data.get("ai_title")
            agent_names = at_data.get("agent_names", [])
            if ai_title or agent_names:
                self._conn.execute(
                    "UPDATE session_metas SET ai_title=?, agent_names=? WHERE session_id=?",
                    (ai_title, _dumps(agent_names), sid),
                )

        self._conn.commit()
        _log.getLogger(__name__).info(
            f"Migration complete: {len(old_sm)} sessions, "
            f"{sum(len(v.get('turns', [])) for v in old_te.values())} turns, "
            f"{sum(len(v.get('tasks', [])) for v in old_at.values())} tasks"
        )

    def close(self):
        self._conn.close()

    def stats(self) -> dict:
        return {**super().stats(), "backend": "sqlite", "db_path": str(self.db_path)}


# ── PostgreSQL backend (scrape_data schema) ───────────────────────────────────

class _PostgresStore:
    """
    PostgreSQL backend writing into the 'scrape_data' schema.
    All tables have properly typed columns instead of JSON blobs.
    """

    def __init__(self, url: str):
        try:
            import psycopg2
        except ImportError:
            raise ImportError(
                "psycopg2 is required for PostgreSQL support. "
                "Install it with: pip install psycopg2-binary"
            )
        self._url = url
        self._conn = psycopg2.connect(url)
        self._conn.autocommit = False
        cur = self._conn.cursor()
        for stmt in _PG_CREATE.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        self._conn.commit()

    # ── Low-level helpers ─────────────────────────────────────────────────────

    def _execute(self, sql: str, params=()):
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur

    def _fetchall(self, sql: str, params=()):
        return self._execute(sql, params).fetchall()

    def _commit(self):
        self._conn.commit()

    # ── push ──────────────────────────────────────────────────────────────────

    def push(self, raw: dict, force: bool = False) -> dict:
        from psycopg2.extras import Json, execute_values

        now = datetime.now(tz=timezone.utc)
        inserted = {"session_metas": 0, "turn_events": 0, "facets": 0, "app_state": 0, "plans": 0, "agent_tasks": 0, "background_tasks": 0, "busy_segments": 0, "segment_signals": 0}
        cur = self._conn.cursor()

        # In force mode, delete existing rows for all incoming sessions before re-inserting
        if force:
            all_sids = (
                {m.get("session_id") for m in raw.get("session_metas", []) if m.get("session_id")}
                | {t.get("session_id") for t in raw.get("turn_events", []) if t.get("session_id")}
                | {s.get("session_id") for s in raw.get("busy_segments", []) if s.get("session_id")}
                | {s.get("session_id") for s in raw.get("segment_signals", []) if s.get("session_id")}
                | set(raw.get("facets", {}).keys())
                | set(raw.get("agent_tasks", {}).keys())
            )
            if all_sids:
                sid_list = list(all_sids)
                cur.execute("DELETE FROM scrape_data.turn_events  WHERE session_id = ANY(%s)", (sid_list,))
                cur.execute("DELETE FROM scrape_data.agent_tasks  WHERE session_id = ANY(%s)", (sid_list,))
                cur.execute("DELETE FROM scrape_data.background_tasks WHERE session_id = ANY(%s)", (sid_list,))
                cur.execute("DELETE FROM scrape_data.busy_segments WHERE session_id = ANY(%s)", (sid_list,))
                cur.execute("DELETE FROM scrape_data.segment_signals WHERE session_id = ANY(%s)", (sid_list,))
                cur.execute("DELETE FROM scrape_data.facets       WHERE session_id = ANY(%s)", (sid_list,))
                cur.execute("DELETE FROM scrape_data.session_metas WHERE session_id = ANY(%s)", (sid_list,))

        # ── session_metas — bulk insert, skip duplicates ──────────────────────
        sm_rows = []
        for meta in raw.get("session_metas", []):
            sid = meta.get("session_id")
            if not sid:
                continue
            sm_rows.append((
                sid,
                meta.get("developer_key", ""),
                meta.get("claude_dir"),
                meta.get("account_type"),
                meta.get("project_path"),
                meta.get("start_time"),
                meta.get("week"),
                meta.get("date"),
                meta.get("duration_minutes"),
                meta.get("user_message_count"),
                meta.get("assistant_message_count"),
                meta.get("lines_added", 0),
                meta.get("lines_removed", 0),
                meta.get("files_modified", 0),
                meta.get("git_commits", 0),
                meta.get("git_pushes", 0),
                meta.get("first_prompt"),
                meta.get("user_interruptions", 0),
                meta.get("tool_errors", 0),
                bool(meta.get("uses_task_agent", False)),
                bool(meta.get("uses_mcp", False)),
                bool(meta.get("uses_web_search", False)),
                bool(meta.get("uses_web_fetch", False)),
                meta.get("input_tokens", 0),
                meta.get("output_tokens", 0),
                Json(meta.get("tool_counts") or {}),
                Json(meta.get("languages") or {}),
                Json(meta.get("user_response_times") or []),
                meta.get("source"),
                now,
            ))
        if sm_rows:
            execute_values(
                cur,
                """
                INSERT INTO scrape_data.session_metas (
                    session_id, developer_key, claude_dir, account_type, project_path,
                    start_time, week, date, duration_minutes,
                    user_message_count, assistant_message_count,
                    lines_added, lines_removed, files_modified, git_commits, git_pushes,
                    first_prompt, user_interruptions, tool_errors,
                    uses_task_agent, uses_mcp, uses_web_search, uses_web_fetch,
                    input_tokens, output_tokens,
                    tool_counts, languages, user_response_times, source, pushed_at
                ) VALUES %s ON CONFLICT (session_id) DO NOTHING
                """,
                sm_rows,
            )
            inserted["session_metas"] = cur.rowcount

        # ── turn_events — bulk insert, turn-level dedup ───────────────────────
        # Skills use session-level dedup (they don't accumulate mid-session).
        # Turn rows use turn-level dedup (max user_ts per session) so that active
        # sessions accumulate new turns on every push without re-inserting old ones.
        existing_te  = self._existing_turn_sessions(cur)
        max_turn_ts  = self._max_turn_ts_per_session(cur)   # {sid: datetime}
        skill_rows, turn_rows = [], []
        for t in raw.get("turn_events", []):
            sid = t.get("session_id", "")
            if t.get("event_type") == "skill":
                # Skills: session-level dedup — collected once per session at creation time
                if sid in existing_te:
                    continue
                skill_rows.append((
                    sid,
                    t.get("developer_key", ""),
                    "skill",
                    t.get("ts"),
                    bool(t.get("is_sidechain", False)),
                    t.get("agent_colors_in_session", 0),
                    t.get("command"),
                    now,
                ))
            else:
                # Turns: turn-level dedup via max user_ts — active sessions accumulate
                max_ts = max_turn_ts.get(sid)
                if max_ts:
                    user_ts_str = t.get("user_ts")
                    if not user_ts_str:
                        continue
                    try:
                        from datetime import datetime as _dt
                        user_ts_dt = _dt.fromisoformat(user_ts_str.replace("Z", "+00:00"))
                        if user_ts_dt <= max_ts:
                            continue
                    except Exception:
                        continue
                turn_rows.append((
                    sid,
                    t.get("developer_key", ""),
                    "turn",
                    t.get("user_ts"),
                    t.get("assistant_ts"),
                    t.get("agent_ms"),
                    bool(t.get("is_sidechain", False)),
                    t.get("permission_mode"),
                    Json(t.get("tool_uses") or []),
                    t.get("agent_colors_in_session", 0),
                    t.get("prompt_text") or None,
                    now,
                ))
        if skill_rows:
            execute_values(
                cur,
                """
                INSERT INTO scrape_data.turn_events (
                    session_id, developer_key, event_type,
                    user_ts, is_sidechain, agent_colors_in_session, command, pushed_at
                ) VALUES %s
                """,
                skill_rows,
            )
            inserted["turn_events"] += len(skill_rows)
        if turn_rows:
            execute_values(
                cur,
                """
                INSERT INTO scrape_data.turn_events (
                    session_id, developer_key, event_type,
                    user_ts, assistant_ts, agent_ms,
                    is_sidechain, permission_mode,
                    tool_uses, agent_colors_in_session, prompt_text, pushed_at
                ) VALUES %s
                """,
                turn_rows,
            )
            inserted["turn_events"] += len(turn_rows)

        # ── facets — bulk insert, skip duplicates ─────────────────────────────
        facet_rows = []
        for sid, f in raw.get("facets", {}).items():
            facet_rows.append((
                sid,
                f.get("developer_key"),
                f.get("underlying_goal"),
                Json(f.get("goal_categories") or {}),
                f.get("outcome"),
                f.get("session_type"),
                f.get("claude_helpfulness"),
                Json(f.get("friction_counts") or {}),
                f.get("friction_detail"),
                f.get("primary_success"),
                f.get("brief_summary"),
                now,
            ))
        if facet_rows:
            execute_values(
                cur,
                """
                INSERT INTO scrape_data.facets (
                    session_id, developer_key, underlying_goal, goal_categories,
                    outcome, session_type, claude_helpfulness,
                    friction_counts, friction_detail, primary_success, brief_summary, pushed_at
                ) VALUES %s ON CONFLICT (session_id) DO NOTHING
                """,
                facet_rows,
            )
            inserted["facets"] = cur.rowcount

        # ── app_state — bulk upsert ───────────────────────────────────────────
        as_rows = []
        for dev_key, state in raw.get("app_state", {}).items():
            as_rows.append((
                dev_key,
                state.get("total_startups", 0),
                bool(state.get("has_used_background_task", False)),
                Json(state.get("install_methods") or []),
                Json(state.get("accounts") or []),
                now,
            ))
        if as_rows:
            execute_values(
                cur,
                """
                INSERT INTO scrape_data.app_state (
                    developer_key, total_startups, has_used_background_task,
                    install_methods, accounts, pushed_at
                ) VALUES %s
                ON CONFLICT (developer_key) DO UPDATE SET
                    total_startups           = EXCLUDED.total_startups,
                    has_used_background_task = EXCLUDED.has_used_background_task,
                    install_methods          = EXCLUDED.install_methods,
                    accounts                 = EXCLUDED.accounts,
                    pushed_at                = EXCLUDED.pushed_at
                """,
                as_rows,
            )
            inserted["app_state"] = len(as_rows)

        # ── plans — bulk upsert ───────────────────────────────────────────────
        plan_rows = []
        for dev_key, plan_info in raw.get("plans", {}).items():
            plan_rows.append((
                dev_key,
                plan_info.get("total_plans", 0),
                plan_info.get("new_plans_since_last_run", 0),
                Json(plan_info.get("plan_names") or []),
                now,
            ))
        if plan_rows:
            execute_values(
                cur,
                """
                INSERT INTO scrape_data.plans (
                    developer_key, total_plans, new_plans_since_last_run, plan_names, pushed_at
                ) VALUES %s
                ON CONFLICT (developer_key) DO UPDATE SET
                    total_plans              = EXCLUDED.total_plans,
                    new_plans_since_last_run = EXCLUDED.new_plans_since_last_run,
                    plan_names               = EXCLUDED.plan_names,
                    pushed_at                = EXCLUDED.pushed_at
                """,
                plan_rows,
            )
            inserted["plans"] = len(plan_rows)

        # ── agent_tasks — bulk insert + bulk UPDATE session_metas ────────────
        existing_at = self._existing_agent_sessions(cur)
        at_rows = []
        title_updates = []
        for session_id, at_data in raw.get("agent_tasks", {}).items():
            ai_title    = at_data.get("ai_title")
            agent_names = at_data.get("agent_names") or []
            dev_key     = at_data.get("developer_key", "")
            if ai_title or agent_names:
                title_updates.append((ai_title, Json(agent_names), session_id))
            if session_id in existing_at:
                continue
            for task in at_data.get("tasks", []):
                if not task.get("agent_name") and not task.get("task_id"):
                    continue   # skip ghost rows with no identity
                at_rows.append((
                    session_id,
                    dev_key,
                    task.get("task_id"),
                    task.get("agent_name"),
                    task.get("task_description"),
                    task.get("status"),
                    task.get("enqueued_at"),
                    task.get("week"),
                    now,
                ))

        # Bulk UPDATE ai_title/agent_names via temp table
        if title_updates:
            cur.execute("""
                CREATE TEMP TABLE _tmp_titles (
                    ai_title TEXT, agent_names JSONB, session_id TEXT
                ) ON COMMIT DROP
            """)
            execute_values(cur, "INSERT INTO _tmp_titles VALUES %s", title_updates)
            cur.execute("""
                UPDATE scrape_data.session_metas sm
                   SET ai_title    = COALESCE(sm.ai_title, t.ai_title),
                       agent_names = t.agent_names
                  FROM _tmp_titles t
                 WHERE sm.session_id = t.session_id
            """)

        if at_rows:
            execute_values(
                cur,
                """
                INSERT INTO scrape_data.agent_tasks (
                    session_id, developer_key, task_id, agent_name,
                    task_description, status, enqueued_at, week, pushed_at
                ) VALUES %s
                """,
                at_rows,
            )
            inserted["agent_tasks"] = len(at_rows)

        # ── background_tasks — agent-less queue-operation enqueues (harness M7) ──
        bt_rows = []
        for session_id, at_data in raw.get("agent_tasks", {}).items():
            if session_id in existing_at:
                continue
            dev_key = at_data.get("developer_key", "")
            for bt in at_data.get("background_tasks", []) or []:
                bt_rows.append((session_id, dev_key, bt.get("enqueued_at"), bt.get("week"), now))
        if bt_rows:
            execute_values(
                cur,
                """
                INSERT INTO scrape_data.background_tasks (
                    session_id, developer_key, enqueued_at, week, pushed_at
                ) VALUES %s
                """,
                bt_rows,
            )
            inserted["background_tasks"] = len(bt_rows)

        # ── busy_segments — session-level replace (reparse is authoritative) ──
        seg_rows = []
        seg_sids = set()
        for s in raw.get("busy_segments", []):
            sid = s.get("session_id")
            if not sid:
                continue
            seg_sids.add(sid)
            seg_rows.append((
                sid,
                s.get("developer_key", ""),
                s.get("start_ts"),
                s.get("end_ts"),
                bool(s.get("is_sidechain", False)),
                _week_of(s.get("start_ts")),
                now,
            ))
        if seg_sids and not force:
            # force mode already deleted these above; otherwise refresh per session
            cur.execute(
                "DELETE FROM scrape_data.busy_segments WHERE session_id = ANY(%s)",
                (list(seg_sids),),
            )
        if seg_rows:
            execute_values(
                cur,
                """
                INSERT INTO scrape_data.busy_segments (
                    session_id, developer_key, start_ts, end_ts, is_sidechain, week, pushed_at
                ) VALUES %s
                """,
                seg_rows,
            )
            inserted["busy_segments"] = len(seg_rows)

        # ── segment_signals — session-level replace (quality-layer backbone) ──
        sig_rows = []
        sig_sids = set()
        for s in raw.get("segment_signals", []):
            sid = s.get("session_id")
            if not sid:
                continue
            sig_sids.add(sid)
            sig_rows.append((
                sid,
                s.get("developer_key", ""),
                s.get("start_ts"),
                _week_of(s.get("start_ts")),
                Json(s),
                now,
            ))
        if sig_sids and not force:
            # force mode already deleted these above; otherwise refresh per session
            cur.execute(
                "DELETE FROM scrape_data.segment_signals WHERE session_id = ANY(%s)",
                (list(sig_sids),),
            )
        if sig_rows:
            execute_values(
                cur,
                """
                INSERT INTO scrape_data.segment_signals (
                    session_id, developer_key, start_ts, week, data, pushed_at
                ) VALUES %s
                """,
                sig_rows,
            )
            inserted["segment_signals"] = len(sig_rows)

        self._conn.commit()
        return inserted

    def _existing_agent_sessions(self, cur=None) -> set[str]:
        if cur is None:
            cur = self._conn.cursor()
        cur.execute("SELECT DISTINCT session_id FROM scrape_data.agent_tasks")
        return {r[0] for r in cur.fetchall()}

    def _existing_turn_sessions(self, cur=None) -> set[str]:
        if cur is None:
            cur = self._conn.cursor()
        cur.execute("SELECT DISTINCT session_id FROM scrape_data.turn_events")
        return {r[0] for r in cur.fetchall()}

    def _max_turn_ts_per_session(self, cur=None) -> dict:
        """Return {session_id: max_user_ts datetime} for sessions in turn_events."""
        if cur is None:
            cur = self._conn.cursor()
        cur.execute(
            "SELECT session_id, MAX(user_ts) FROM scrape_data.turn_events "
            "WHERE event_type = 'turn' AND user_ts IS NOT NULL GROUP BY session_id"
        )
        return {r[0]: r[1] for r in cur.fetchall() if r[1]}

    # ── pushed_session_ids ────────────────────────────────────────────────────

    def pushed_session_ids(self) -> set[str]:
        rows = self._fetchall("SELECT session_id FROM scrape_data.session_metas")
        return {r[0] for r in rows}

    def developer_names(self) -> dict[str, str]:
        """{developer_key: name} for report display (name, not the hash id)."""
        return {r[0]: r[1] for r in self._fetchall(
            "SELECT developer_key, name FROM scrape_data.developers") if r[1]}

    def pushed_turn_session_ids(self) -> set[str]:
        """Session IDs already in turn_events — used for incremental turn collection."""
        rows = self._fetchall("SELECT DISTINCT session_id FROM scrape_data.turn_events")
        return {r[0] for r in rows}

    def pushed_agent_session_ids(self) -> set[str]:
        """Session IDs already in agent_tasks — used for incremental agent collection."""
        rows = self._fetchall("SELECT DISTINCT session_id FROM scrape_data.agent_tasks")
        return {r[0] for r in rows}

    # ── pull_raw ──────────────────────────────────────────────────────────────

    def pull_raw(self, since: datetime | None = None) -> dict:
        since_date = since.date().isoformat() if since else None
        sm_select = (
            "SELECT " + ", ".join(_SM_COLS) +
            " FROM scrape_data.session_metas"
        )

        if since_date:
            rows = self._fetchall(
                sm_select + " WHERE date >= %s OR date IS NULL", (since_date,)
            )
        else:
            rows = self._fetchall(sm_select)

        session_metas = []
        for row in rows:
            d = dict(zip(_SM_COLS, row))
            # TIMESTAMPTZ comes back as aware datetime — convert to ISO string
            st = d.get("start_time")
            if st and hasattr(st, "isoformat"):
                d["start_time"] = st.isoformat()
            # DATE comes back as date object — convert to string
            dt = d.get("date")
            if dt and hasattr(dt, "isoformat"):
                d["date"] = dt.isoformat()
            session_metas.append(d)

        in_scope = {m["session_id"] for m in session_metas}
        turn_events: list[dict] = []
        facets: dict[str, dict] = {}

        if in_scope:
            ph = ",".join(["%s"] * len(in_scope))
            args = tuple(in_scope)

            te_select = (
                "SELECT " + ", ".join(_TE_COLS) +
                f" FROM scrape_data.turn_events WHERE session_id IN ({ph})"
            )
            for row in self._fetchall(te_select, args):
                d = dict(zip(_TE_COLS, row))
                event_type = d.pop("event_type")
                command    = d.pop("command")
                if event_type == "skill":
                    ts_val = d.get("user_ts")
                    turn_events.append({
                        "session_id":              d["session_id"],
                        "developer_key":           d["developer_key"],
                        "event_type":              "skill",
                        "command":                 command,
                        "ts":                      ts_val.isoformat() if hasattr(ts_val, "isoformat") else ts_val,
                        "is_sidechain":            d.get("is_sidechain", False),
                        "agent_colors_in_session": d.get("agent_colors_in_session", 0),
                    })
                else:
                    for k in ("user_ts", "assistant_ts"):
                        v = d.get(k)
                        if v and hasattr(v, "isoformat"):
                            d[k] = v.isoformat()
                    # NUMERIC comes back as Decimal — computers expect float
                    if d.get("agent_ms") is not None:
                        d["agent_ms"] = float(d["agent_ms"])
                    turn_events.append(d)

            f_select = (
                "SELECT " + ", ".join(_F_COLS) +
                f" FROM scrape_data.facets WHERE session_id IN ({ph})"
            )
            for row in self._fetchall(f_select, args):
                d = dict(zip(_F_COLS, row))
                facets[d["session_id"]] = d

        # app_state — all rows
        app_state: dict[str, dict] = {}
        for row in self._fetchall(
            "SELECT developer_key, total_startups, has_used_background_task, "
            "install_methods, accounts FROM scrape_data.app_state"
        ):
            app_state[row[0]] = {
                "developer_key":           row[0],
                "total_startups":          row[1],
                "has_used_background_task": row[2],
                "install_methods":         row[3],
                "accounts":                row[4],
            }

        # plans — all rows
        plans: dict[str, dict] = {}
        for row in self._fetchall(
            "SELECT developer_key, total_plans, new_plans_since_last_run, plan_names "
            "FROM scrape_data.plans"
        ):
            plans[row[0]] = {
                "developer_key":            row[0],
                "total_plans":              row[1],
                "new_plans_since_last_run": row[2],
                "plan_names":               row[3],
            }

        # agent_tasks — keyed by session_id, same shape as agent_tasks.collect()
        # {session_id: {developer_key, tasks: [...]}}
        agent_tasks_result: dict[str, dict] = {}
        if in_scope:
            at_rows = self._fetchall(
                f"SELECT session_id, developer_key, task_id, agent_name, task_description, status, enqueued_at "
                f"FROM scrape_data.agent_tasks WHERE session_id IN ({ph})",
                args,
            )
            for row in at_rows:
                enq = row[6]
                sid = row[0]
                if sid not in agent_tasks_result:
                    agent_tasks_result[sid] = {"developer_key": row[1], "tasks": []}
                agent_tasks_result[sid]["tasks"].append({
                    "task_id":          row[2],
                    "agent_name":       row[3],
                    "task_description": row[4],
                    "status":           row[5],
                    "enqueued_at":      enq.isoformat() if hasattr(enq, "isoformat") else enq,
                })

            # background_tasks — agent-less enqueues (harness M7 background component)
            for row in self._fetchall(
                f"SELECT session_id, developer_key, enqueued_at, week "
                f"FROM scrape_data.background_tasks WHERE session_id IN ({ph})",
                args,
            ):
                sid = row[0]
                if sid not in agent_tasks_result:
                    agent_tasks_result[sid] = {"developer_key": row[1], "tasks": []}
                enq = row[2]
                agent_tasks_result[sid].setdefault("background_tasks", []).append({
                    "enqueued_at": enq.isoformat() if hasattr(enq, "isoformat") else enq,
                    "week":        row[3],
                })

        # busy_segments — accurate agent-hours source. Not gated by session_metas:
        # segments carry developer_key + start_ts, so JSONL-only sessions still count.
        busy_segments: list[dict] = []
        seg_sql = ("SELECT session_id, developer_key, start_ts, end_ts, is_sidechain "
                   "FROM scrape_data.busy_segments")
        if since_date:
            seg_rows = self._fetchall(seg_sql + " WHERE start_ts >= %s OR start_ts IS NULL", (since,))
        else:
            seg_rows = self._fetchall(seg_sql)
        for row in seg_rows:
            busy_segments.append({
                "session_id":    row[0],
                "developer_key": row[1],
                "start_ts":      row[2].isoformat() if hasattr(row[2], "isoformat") else row[2],
                "end_ts":        row[3].isoformat() if hasattr(row[3], "isoformat") else row[3],
                "is_sidechain":  bool(row[4]),
            })

        # segment_signals — quality-layer backbone (JSONB blob = full per-segment record).
        segment_signals: list[dict] = []
        sig_sql = "SELECT data FROM scrape_data.segment_signals"
        if since_date:
            sig_rows = self._fetchall(sig_sql + " WHERE start_ts >= %s OR start_ts IS NULL", (since,))
        else:
            sig_rows = self._fetchall(sig_sql)
        for row in sig_rows:
            if row[0]:
                # JSONB comes back as dict (psycopg2) or str depending on adapter.
                segment_signals.append(row[0] if isinstance(row[0], dict) else json.loads(row[0]))

        return {
            "session_metas": session_metas,
            "turn_events":   turn_events,
            "facets":        facets,
            "app_state":     app_state,
            "plans":         plans,
            "agent_tasks":   agent_tasks_result,
            "busy_segments": busy_segments,
            "segment_signals": segment_signals,
        }

    # ── stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "session_metas": self._fetchall("SELECT COUNT(*) FROM scrape_data.session_metas")[0][0],
            "turn_events":   self._fetchall("SELECT COUNT(*) FROM scrape_data.turn_events")[0][0],
            "facets":        self._fetchall("SELECT COUNT(*) FROM scrape_data.facets")[0][0],
            "app_state":     self._fetchall("SELECT COUNT(*) FROM scrape_data.app_state")[0][0],
            "plans":         self._fetchall("SELECT COUNT(*) FROM scrape_data.plans")[0][0],
            "agent_tasks":   self._fetchall("SELECT COUNT(*) FROM scrape_data.agent_tasks")[0][0],
            "background_tasks": self._fetchall("SELECT COUNT(*) FROM scrape_data.background_tasks")[0][0],
            "busy_segments": self._fetchall("SELECT COUNT(*) FROM scrape_data.busy_segments")[0][0],
            "segment_signals": self._fetchall("SELECT COUNT(*) FROM scrape_data.segment_signals")[0][0],
            "developers":    self._fetchall(
                "SELECT COUNT(DISTINCT developer_key) FROM scrape_data.session_metas"
            )[0][0],
            "backend": "postgresql",
        }

    def upsert_developers(self, developer_map: list[dict]) -> int:
        from psycopg2.extras import Json
        now = datetime.now(tz=timezone.utc)
        cur = self._conn.cursor()
        count = 0
        for dev in developer_map:
            key = dev.get("developer_key")
            if not key:
                continue
            cur.execute(
                """
                INSERT INTO scrape_data.developers (developer_key, name, email, claude_dirs, pushed_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (developer_key) DO UPDATE
                    SET name        = COALESCE(EXCLUDED.name, scrape_data.developers.name),
                        email       = COALESCE(EXCLUDED.email, scrape_data.developers.email),
                        claude_dirs = EXCLUDED.claude_dirs,
                        pushed_at   = EXCLUDED.pushed_at
                """,
                (key, dev.get("name"), dev.get("email"), Json(dev.get("claude_dirs", [])), now),
            )
            count += cur.rowcount
        self._conn.commit()
        return count

    def close(self):
        self._conn.close()
