import tempfile
import unittest
from pathlib import Path

from central_store import CentralStore
from push import _filter_turn_events, _select_sessions_for_push


class IncrementalPushTests(unittest.TestCase):
    def test_select_sessions_uses_prompt_cursor_advancement(self):
        selected = _select_sessions_for_push(
            {
                "same": "2026-07-01T10:00:00+00:00",
                "new": "2026-07-01T11:00:00+00:00",
                "fresh": "2026-07-01T09:00:00+00:00",
            },
            {
                "same": "2026-07-01T10:00:00+00:00",
                "new": "2026-07-01T10:30:00+00:00",
            },
            force=False,
        )
        self.assertEqual(selected, {"new", "fresh"})

    def test_filter_turn_events_uses_stored_prompt_cursor(self):
        events = [
            {
                "session_id": "s1",
                "user_ts": "2026-07-01T10:00:00+00:00",
                "assistant_ts": "2026-07-01T10:00:05+00:00",
            },
            {
                "session_id": "s1",
                "user_ts": "2026-07-01T11:00:00+00:00",
                "assistant_ts": "2026-07-01T11:00:05+00:00",
            },
            {
                "session_id": "s1",
                "event_type": "skill",
                "ts": "2026-07-01T11:05:00+00:00",
            },
        ]
        filtered = _filter_turn_events(
            events,
            {"s1"},
            {"s1": "2026-07-01T10:30:00+00:00"},
            force=False,
        )
        self.assertEqual(len(filtered), 2)
        self.assertEqual(filtered[0]["user_ts"], "2026-07-01T11:00:00+00:00")
        self.assertEqual(filtered[1]["event_type"], "skill")

    def test_sqlite_store_upserts_prompt_cursor_and_appends_turns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "central.db"
            store = CentralStore(db_path)
            raw1 = {
                "session_metas": [{
                    "session_id": "s1",
                    "developer_key": "dev",
                    "last_prompt_ts": "2026-07-01T10:00:00+00:00",
                    "tool_counts": {},
                    "languages": {},
                    "user_response_times": [],
                    "agent_names": [],
                }],
                "turn_events": [{
                    "session_id": "s1",
                    "developer_key": "dev",
                    "user_ts": "2026-07-01T10:00:00+00:00",
                    "assistant_ts": "2026-07-01T10:00:05+00:00",
                    "agent_ms": 5000,
                    "tool_uses": [],
                    "agent_colors_in_session": 0,
                }],
                "busy_segments": [],
                "facets": {},
                "app_state": {},
                "plans": {},
                "agent_tasks": {},
            }
            raw2 = {
                "session_metas": [{
                    "session_id": "s1",
                    "developer_key": "dev",
                    "last_prompt_ts": "2026-07-01T11:00:00+00:00",
                    "tool_counts": {},
                    "languages": {},
                    "user_response_times": [],
                    "agent_names": [],
                }],
                "turn_events": [{
                    "session_id": "s1",
                    "developer_key": "dev",
                    "user_ts": "2026-07-01T11:00:00+00:00",
                    "assistant_ts": "2026-07-01T11:00:06+00:00",
                    "agent_ms": 6000,
                    "tool_uses": [],
                    "agent_colors_in_session": 0,
                }],
                "busy_segments": [],
                "facets": {},
                "app_state": {},
                "plans": {},
                "agent_tasks": {},
            }

            store.push(raw1, force=False)
            store.push(raw2, force=False)

            self.assertEqual(store.session_prompt_cursors()["s1"], "2026-07-01T11:00:00+00:00")
            self.assertEqual(store.stats()["session_metas"], 1)
            self.assertEqual(store.stats()["turn_events"], 2)
            store.close()


if __name__ == "__main__":
    unittest.main()
