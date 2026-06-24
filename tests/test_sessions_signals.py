"""
U1 — per-tool-call signal extraction (collectors/sessions.py).

Runnable two ways:
  - pytest tests/test_sessions_signals.py
  - python3 tests/test_sessions_signals.py      (no pytest needed; self-running)

Fixtures are synthetic JSONL written to a temp file and parsed by
_signals_from_jsonl directly, so the tests assert on the correlation +
bucketing logic without needing real ~/.claude transcripts.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.sessions import (  # noqa: E402
    _signals_from_jsonl, _read_agent_meta, collect_segments,
)


def _write_jsonl(msgs: list[dict]) -> Path:
    fd, name = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for m in msgs:
            f.write(json.dumps(m) + "\n")
    return Path(name)


def _human(ts: str, text: str = "do the thing") -> dict:
    return {"type": "user", "timestamp": ts, "message": {"content": text}}


def _tool_use(ts: str, tuid: str, name: str, tool_input: dict) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"content": [{"type": "tool_use", "id": tuid, "name": name, "input": tool_input}]},
    }


def _tool_result(ts: str, tuid: str, tool_use_result: dict, block_is_error: bool = False) -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "toolUseResult": tool_use_result,
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": tuid, "content": "out", "is_error": block_is_error}
        ]},
    }


def _parse(msgs: list[dict]) -> list[dict]:
    path = _write_jsonl(msgs)
    try:
        return _signals_from_jsonl(path, "dev1", "S1", False)
    finally:
        path.unlink(missing_ok=True)


def test_happy_path_two_calls_no_error():
    """Two successful tool calls in one segment yield two tool_calls, is_error False."""
    recs = _parse([
        _human("2026-06-24T10:00:00Z"),
        _tool_use("2026-06-24T10:00:05Z", "t1", "Bash", {"command": "pytest -q"}),
        _tool_result("2026-06-24T10:00:08Z", "t1", {"stdout": "ok", "interrupted": False}),
        _tool_use("2026-06-24T10:00:10Z", "t2", "Edit", {"file_path": "src/app.py"}),
        _tool_result("2026-06-24T10:00:12Z", "t2", {"structuredPatch": [], "interrupted": False}),
    ])
    assert len(recs) == 1, f"expected 1 segment, got {len(recs)}"
    calls = recs[0]["tool_calls"]
    assert len(calls) == 2, f"expected 2 calls, got {len(calls)}"
    assert all(c["is_error"] is False for c in calls), calls
    assert {c["target"] for c in calls} == {"pytest", "src/app.py"}
    assert recs[0]["ended_in_interrupt"] is False


def test_error_read_from_content_block():
    """is_error is read from the content tool_result block — the authoritative
    per-call source (toolUseResult carries no is_error field on real transcripts)."""
    recs = _parse([
        _human("2026-06-24T10:00:00Z"),
        _tool_use("2026-06-24T10:00:05Z", "t1", "Bash", {"command": "npm run build"}),
        _tool_result("2026-06-24T10:00:09Z", "t1", {"stderr": "boom"}, block_is_error=True),
    ])
    calls = recs[0]["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["is_error"] is True, calls


def test_error_from_tooluseresult_supplement():
    """toolUseResult is_error/status still counts as a supplement for tools that
    set it, even when the content block does not."""
    recs = _parse([
        _human("2026-06-24T11:00:00Z"),
        _tool_use("2026-06-24T11:00:05Z", "t9", "Bash", {"command": "make"}),
        _tool_result("2026-06-24T11:00:09Z", "t9", {"status": "failed"}, block_is_error=False),
    ])
    assert recs[0]["tool_calls"][0]["is_error"] is True


def test_tool_use_without_result_is_unknown():
    """A tool_use with no matching tool_result (truncated session) records is_error=None."""
    recs = _parse([
        _human("2026-06-24T10:00:00Z"),
        _tool_use("2026-06-24T10:00:05Z", "t1", "Bash", {"command": "sleep 1"}),
        # no tool_result for t1; a later agent line keeps the segment open
        _tool_use("2026-06-24T10:00:30Z", "t2", "Read", {"file_path": "x.py"}),
        _tool_result("2026-06-24T10:00:31Z", "t2", {"interrupted": False}),
    ])
    by_target = {c["target"]: c for c in recs[0]["tool_calls"]}
    assert by_target["sleep"]["is_error"] is None, by_target
    assert by_target["x.py"]["is_error"] is False


def test_segment_ended_in_interrupt():
    """toolUseResult.interrupted marks the segment ended_in_interrupt and the call."""
    recs = _parse([
        _human("2026-06-24T10:00:00Z"),
        _tool_use("2026-06-24T10:00:05Z", "t1", "Bash", {"command": "long-running"}),
        _tool_result("2026-06-24T10:00:20Z", "t1", {"interrupted": True}),
    ])
    assert recs[0]["ended_in_interrupt"] is True
    assert recs[0]["tool_calls"][0]["interrupted"] is True


def test_collect_segments_shape_unchanged():
    """KTD2 regression guard: the existing busy-segment record stays exactly the
    5 timing fields — the new signal stream must not leak into it."""
    from collectors.sessions import _segments_from_jsonl
    path = _write_jsonl([
        _human("2026-06-24T10:00:00Z"),
        _tool_use("2026-06-24T10:00:05Z", "t1", "Bash", {"command": "echo hi"}),
        _tool_result("2026-06-24T10:00:06Z", "t1", {"interrupted": False}),
    ])
    try:
        segs = _segments_from_jsonl(path, "dev1", "S1", False)
    finally:
        path.unlink(missing_ok=True)
    assert segs, "expected at least one busy segment"
    assert set(segs[0].keys()) == {
        "session_id", "developer_key", "start_ts", "end_ts", "is_sidechain"
    }, segs[0].keys()


def test_verification_pass_detected():
    """A passing test run is captured as verification {kind: test, passed: True}."""
    recs = _parse([
        _human("2026-06-24T10:00:00Z"),
        _tool_use("2026-06-24T10:00:05Z", "t1", "Bash", {"command": "pytest -q tests/"}),
        _tool_result("2026-06-24T10:00:30Z", "t1", {"stdout": "5 passed"}, block_is_error=False),
    ])
    v = recs[0]["verification"]
    assert len(v) == 1 and v[0]["kind"] == "test" and v[0]["passed"] is True, v


def test_verification_failure_detected():
    """A failing build (content-block is_error) is captured as passed: False."""
    recs = _parse([
        _human("2026-06-24T11:00:00Z"),
        _tool_use("2026-06-24T11:00:05Z", "t1", "Bash", {"command": "npm run build"}),
        _tool_result("2026-06-24T11:00:20Z", "t1", {"stderr": "error"}, block_is_error=True),
    ])
    v = recs[0]["verification"]
    assert len(v) == 1 and v[0]["kind"] == "build" and v[0]["passed"] is False, v


def test_non_verification_command_ignored():
    """A plain command (git status) produces no verification signal."""
    recs = _parse([
        _human("2026-06-24T12:00:00Z"),
        _tool_use("2026-06-24T12:00:05Z", "t1", "Bash", {"command": "git status"}),
        _tool_result("2026-06-24T12:00:06Z", "t1", {"stdout": "clean"}, block_is_error=False),
    ])
    assert recs[0]["verification"] == [], recs[0]["verification"]


def test_typecheck_classified_not_build():
    """`tsc --noEmit` classifies as typecheck, not build (ordering matters)."""
    recs = _parse([
        _human("2026-06-24T13:00:00Z"),
        _tool_use("2026-06-24T13:00:05Z", "t1", "Bash", {"command": "tsc --noEmit"}),
        _tool_result("2026-06-24T13:00:10Z", "t1", {"stdout": ""}, block_is_error=False),
    ])
    assert recs[0]["verification"][0]["kind"] == "typecheck", recs[0]["verification"]


def test_unresolved_verification_has_no_outcome():
    """A verification command with no result (unresolved) yields no pass/fail."""
    recs = _parse([
        _human("2026-06-24T14:00:00Z"),
        _tool_use("2026-06-24T14:00:05Z", "t1", "Bash", {"command": "pytest"}),
        _tool_use("2026-06-24T14:00:30Z", "t2", "Read", {"file_path": "x.py"}),
        _tool_result("2026-06-24T14:00:31Z", "t2", {"stdout": ""}, block_is_error=False),
    ])
    assert recs[0]["verification"] == [], recs[0]["verification"]


def test_churn_survived_lines():
    """Lines added and never removed report survived == added, reverted == 0."""
    recs = _parse([
        _human("2026-06-24T10:00:00Z"),
        _tool_use("2026-06-24T10:00:05Z", "t1", "Edit", {"file_path": "a.py"}),
        _tool_result("2026-06-24T10:00:06Z", "t1",
                     {"filePath": "a.py", "structuredPatch": [{"lines": ["+x", "+y", " z"]}]}),
    ])
    assert recs[0]["churn"] == {"added": 2, "survived": 2, "reverted": 0}, recs[0]["churn"]


def test_churn_reverted_lines():
    """A line added then removed later in-session counts as reverted (not survived)."""
    recs = _parse([
        _human("2026-06-24T11:00:00Z"),
        _tool_use("2026-06-24T11:00:05Z", "t1", "Edit", {"file_path": "a.py"}),
        _tool_result("2026-06-24T11:00:06Z", "t1",
                     {"filePath": "a.py", "structuredPatch": [{"lines": ["+foo", "+bar"]}]}),
        _tool_use("2026-06-24T11:00:20Z", "t2", "Edit", {"file_path": "a.py"}),
        _tool_result("2026-06-24T11:00:21Z", "t2",
                     {"filePath": "a.py", "structuredPatch": [{"lines": ["-foo"]}]}),
    ])
    c = recs[0]["churn"]
    assert c["added"] == 2 and c["reverted"] == 1 and c["survived"] == 1, c


def test_churn_no_cross_file_attribution():
    """Removing an identical line from a different file must not revert another file's add."""
    recs = _parse([
        _human("2026-06-24T12:00:00Z"),
        _tool_use("2026-06-24T12:00:05Z", "t1", "Edit", {"file_path": "a.py"}),
        _tool_result("2026-06-24T12:00:06Z", "t1",
                     {"filePath": "a.py", "structuredPatch": [{"lines": ["+foo"]}]}),
        _tool_use("2026-06-24T12:00:20Z", "t2", "Edit", {"file_path": "b.py"}),
        _tool_result("2026-06-24T12:00:21Z", "t2",
                     {"filePath": "b.py", "structuredPatch": [{"lines": ["-foo"]}]}),
    ])
    assert recs[0]["churn"]["reverted"] == 0, recs[0]["churn"]


def test_no_patch_means_zero_churn():
    """A non-edit call (no structuredPatch) contributes no churn."""
    recs = _parse([
        _human("2026-06-24T13:00:00Z"),
        _tool_use("2026-06-24T13:00:05Z", "t1", "Bash", {"command": "ls"}),
        _tool_result("2026-06-24T13:00:06Z", "t1", {"stdout": "files"}),
    ])
    assert recs[0]["churn"] == {"added": 0, "survived": 0, "reverted": 0}, recs[0]["churn"]


def test_agent_identity_defaults_to_main():
    """Main-session records default agent_kind='main' and agent_id == session_id."""
    recs = _parse([
        _human("2026-06-24T10:00:00Z"),
        _tool_use("2026-06-24T10:00:05Z", "t1", "Bash", {"command": "ls"}),
        _tool_result("2026-06-24T10:00:06Z", "t1", {"stdout": "x"}),
    ])
    r = recs[0]
    assert r["agent_kind"] == "main" and r["agent_id"] == "S1", r
    assert r["agent_type"] is None and r["workflow_run_id"] is None


def test_agent_identity_stamped_for_subagent():
    """Sub-agent records carry agent_kind/agent_id/agent_type while keeping the
    parent session_id and developer_key (rollup to the developer stays intact)."""
    path = _write_jsonl([
        _human("2026-06-24T10:00:00Z"),
        _tool_use("2026-06-24T10:00:05Z", "t1", "Bash", {"command": "ls"}),
        _tool_result("2026-06-24T10:00:06Z", "t1", {"stdout": "x"}),
    ])
    try:
        recs = _signals_from_jsonl(path, "dev1", "parentS", True,
                                   agent_kind="subagent", agent_id="agent-abc",
                                   agent_type="Explore", spawn_tool_use_id="tu_9")
    finally:
        path.unlink(missing_ok=True)
    r = recs[0]
    assert r["agent_kind"] == "subagent" and r["agent_id"] == "agent-abc"
    assert r["agent_type"] == "Explore" and r["spawn_tool_use_id"] == "tu_9"
    assert r["session_id"] == "parentS" and r["developer_key"] == "dev1"
    assert r["is_sidechain"] is True


def test_agent_identity_workflow_run_id():
    """Workflow sub-agent records carry workflow_run_id."""
    path = _write_jsonl([
        _human("2026-06-24T10:00:00Z"),
        _tool_use("2026-06-24T10:00:05Z", "t1", "Bash", {"command": "ls"}),
        _tool_result("2026-06-24T10:00:06Z", "t1", {"stdout": "x"}),
    ])
    try:
        recs = _signals_from_jsonl(path, "dev1", "parentS", True,
                                   agent_kind="workflow", agent_id="agent-w",
                                   workflow_run_id="wf_run_1")
    finally:
        path.unlink(missing_ok=True)
    assert recs[0]["agent_kind"] == "workflow" and recs[0]["workflow_run_id"] == "wf_run_1"


def test_read_agent_meta():
    """Sibling agent-<id>.meta.json is read; missing meta returns {}."""
    import json as _json
    from pathlib import Path as _Path
    d = _Path(tempfile.mkdtemp())
    jp = d / "agent-xyz.jsonl"
    jp.write_text("{}\n")
    (d / "agent-xyz.meta.json").write_text(
        _json.dumps({"agentType": "code-reviewer", "toolUseId": "tu_1"}))
    meta = _read_agent_meta(jp)
    assert meta.get("agentType") == "code-reviewer" and meta.get("toolUseId") == "tu_1"
    assert _read_agent_meta(d / "agent-none.jsonl") == {}


# Discovered: collect_segments is imported to keep the module-level contract in view;
# its behavior is covered by the shape guard above via _segments_from_jsonl.
_ = collect_segments


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
