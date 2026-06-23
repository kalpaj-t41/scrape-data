"""
MetricRegistry — singleton registry for all metric/score computers.

Computers register themselves via the @registry.register decorator at import
time. The registry topologically orders them by their `deps` and runs them in
two phases against a shared ComputeContext:

  run_metrics(ctx) → all phase="metric" computers, once.
  run_scores(ctx)  → all phase="score" computers, for the current ctx.week.

Adding a metric = a new registered class + one import line in __init__.py.
No batch_runner changes.
"""

from computers.base import ComputeContext, MetricComputer


class MetricRegistry:
    """Process-wide singleton holding all registered computers."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._computers = {}
        return cls._instance

    # ── registration ───────────────────────────────────────────────────────────

    def register(self, computer_cls):
        """Class decorator: instantiate and register by .name (dedup by name)."""
        inst = computer_cls()
        if not inst.name:
            raise ValueError(f"{computer_cls.__name__} has no name")
        if inst.name in self._computers:
            raise ValueError(f"duplicate metric name: {inst.name}")
        self._computers[inst.name] = inst
        return computer_cls

    def get(self, name: str) -> MetricComputer:
        return self._computers[name]

    def names(self) -> set[str]:
        return set(self._computers)

    # ── ordering ───────────────────────────────────────────────────────────────

    def _ordered(self, phase: str) -> list[MetricComputer]:
        """
        Topologically sort by deps, emit only computers matching `phase`.

        Deps are visited across phases (a score computer can depend on a metric
        computer for ordering) but only matching-phase nodes are emitted, so a
        score dep on a metric node resolves order without re-running it here.
        Raises ValueError on a dependency cycle or an unknown dep.
        """
        order: list[MetricComputer] = []
        seen: set[str] = set()

        def visit(name: str, stack: set[str]):
            if name in seen:
                return
            if name in stack:
                raise ValueError(f"dependency cycle at {name}")
            if name not in self._computers:
                raise ValueError(f"unknown dependency: {name}")
            c = self._computers[name]
            for dep in c.deps:
                visit(dep, stack | {name})
            seen.add(name)
            if c.phase == phase:
                order.append(c)

        for name in self._computers:
            visit(name, set())
        return order

    # ── execution ──────────────────────────────────────────────────────────────

    def run_metrics(self, ctx: ComputeContext) -> dict:
        """Run every phase="metric" computer once; store results in ctx.results."""
        for c in self._ordered("metric"):
            ctx.results[c.name] = c.compute(ctx)
        return ctx.results

    def run_scores(self, ctx: ComputeContext) -> dict:
        """Run every phase="score" computer for the current ctx.week."""
        out: dict = {}
        for c in self._ordered("score"):
            out[c.name] = ctx.results[c.name] = c.compute(ctx)
        return out


registry = MetricRegistry()
