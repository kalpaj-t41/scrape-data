import tempfile
import unittest
from pathlib import Path

from push import LocalCursorStore, _filter_turn_events, _parse_gap, _select_sessions_for_push


class IncrementalPushTests(unittest.TestCase):
    def test_select_sessions_uses_local_cursor_and_gap(self):
        selected = _select_sessions_for_push(
            {
                "same": "2026-07-01T10:00:00+00:00",
                "new": "2026-07-01T12:00:00+00:00",
                "fresh": "2026-07-01T10:20:00+00:00",
            },
            {
                "same": "2026-07-01T10:00:00+00:00",
                "new": "2026-07-01T10:30:00+00:00",
                "fresh": "2026-07-01T10:15:00+00:00",
            },
            force=False,
            min_gap=_parse_gap("1h"),
        )
        self.assertEqual(selected, {"new"})

    def test_force_selects_every_current_session(self):
        selected = _select_sessions_for_push(
            {"same": "2026-07-01T10:00:00+00:00", "new": "2026-07-01T12:00:00+00:00"},
            {"same": "2026-07-01T10:00:00+00:00"},
            force=True,
            min_gap=_parse_gap("1h"),
        )
        self.assertEqual(selected, {"same", "new"})

    def test_filter_turn_events_uses_stored_cursor_boundary(self):
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

    def test_local_cursor_store_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "cursor.json"
            store = LocalCursorStore(state_path)
            store.update_session("s1", "2026-07-01T10:00:00+00:00")
            store.update_session("s2", "2026-07-01T11:00:00+00:00")
            store.save()

            reloaded = LocalCursorStore(state_path)
            self.assertEqual(
                reloaded.session_cursors(),
                {
                    "s1": "2026-07-01T10:00:00+00:00",
                    "s2": "2026-07-01T11:00:00+00:00",
                },
            )


if __name__ == "__main__":
    unittest.main()
