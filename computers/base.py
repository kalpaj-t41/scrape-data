"""
Compute-layer base abstractions: ComputeContext + MetricComputer.

Every metric is a MetricComputer subclass registered with the singleton
registry (computers/registry.py). Computers read all their inputs from a single
shared ComputeContext and return their result dict; later computers read earlier
results back from ctx.results, so cross-metric dependencies (velocity←agent_hours,
equity←composite) are expressed via the `deps` attribute and resolved by topo sort.

Two phases:
  - phase="metric" : runs once, produces a dev-keyed {by_week: ...} dict.
  - phase="score"  : runs once per ISO week (reads ctx.week + metric results).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ComputeContext:
    """Single bag of raw indexes + run params + accumulated results."""

    # ── prebuilt indexes (built once in batch_runner) ──────────────────────────
    sessions_by_dev:  dict
    meta_by_sid:      dict
    turns_by_session: dict
    skill_events:     list
    busy_segments:    list
    turn_events:      list
    facets:           dict
    plans:            dict
    app_state:        dict
    agent_tasks:      dict

    # ── per-tool-call signal stream (U1: efficiency/usefulness/QAAH backbone) ──
    # One record per busy segment: {session_id, developer_key, agent_kind,
    # agent_id, agent_type, workflow_run_id, spawn_tool_use_id, start_ts, end_ts,
    # is_sidechain, tool_calls: [{name, target, is_error, interrupted, ts}],
    # verification: [{kind, passed, ts}], churn: {added, survived, reverted},
    # ended_in_interrupt}. Empty when not collected (daily-only / pre-signal store).
    segment_signals:  list = field(default_factory=list)

    # ── run params ─────────────────────────────────────────────────────────────
    team_size:      int | None = None
    week:           str | None = None
    weekly_history: list = field(default_factory=list)
    dev_name_map:   dict = field(default_factory=dict)

    # ── accumulated outputs (metric phase fills; score phase reads) ────────────
    results: dict = field(default_factory=dict)

    def get(self, name: str):
        """Result of a previously-run computer (dict for metrics, list for composite)."""
        return self.results.get(name, {})


class MetricComputer(ABC):
    """Base class for all metric/score computers."""

    name: str = ""
    deps: tuple[str, ...] = ()
    phase: str = "metric"            # "metric" | "score"

    @abstractmethod
    def compute(self, ctx: ComputeContext):
        """Compute this metric from ctx; return its result (dict, or list for composite)."""
        ...

    def team_summary(self, results, ctx: ComputeContext) -> dict | None:
        """Optional team-level roll-up (overridden by agent_hours / skills / velocity)."""
        return None
