"""
M8 — Trust Index (0–100).

Measures how much developers trust Claude's output.
Low trust = high interruptions, slow response times, always on default permission mode.
High trust = low interruptions, fast iteration, autoEdit or bypassPermissions.
"""

from collections import defaultdict

from computers.base import ComputeContext, MetricComputer
from computers.registry import registry

_PERMISSION_WEIGHTS = {
    "bypassPermissions": 1.0,
    "autoEdit":          0.75,
    "default":           0.40,
}


def _session_trust(meta: dict, session_turns: list[dict]) -> float:
    total_turns = (meta.get("user_message_count") or 1)
    interruptions = meta.get("user_interruptions") or 0

    # How often did the user let Claude finish without stopping it?
    interruption_factor = max(0.0, 1.0 - interruptions / total_turns)

    # Completion factor: a session with zero interruptions = developer trusts
    # Claude to run to completion; partial runs = scepticism.
    # Scaled so that >50% interruption rate → near zero.
    completion_factor = 1.0 if interruptions == 0 else max(0.0, 1.0 - (interruptions / total_turns) * 2)

    # Permission mode: use the most common mode from turns
    mode_counts: dict[str, int] = defaultdict(int)
    for t in session_turns:
        mode = t.get("permission_mode")
        if mode:
            mode_counts[mode] += 1
    if mode_counts:
        dominant_mode = max(mode_counts, key=lambda m: mode_counts[m])
    else:
        dominant_mode = "default"
    permission_factor = _PERMISSION_WEIGHTS.get(dominant_mode, 0.40)

    # Weights: interruption behaviour (50%) + completion signal (20%) + permission mode (30%)
    # Removed response-speed factor — fast response ≠ trust; a developer can be
    # quick AND sceptical (rapid-fire rejections). Completion is a cleaner signal.
    return (interruption_factor * 0.50) + (completion_factor * 0.20) + (permission_factor * 0.30)


@registry.register
class Trust(MetricComputer):
    name = "trust"

    def compute(self, ctx: ComputeContext) -> dict:
        sessions_by_dev = ctx.sessions_by_dev
        turns_by_session = ctx.turns_by_session

        dev_trusts: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        dev_interruptions: dict[str, int] = defaultdict(int)
        dev_permission_modes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for key, sessions in sessions_by_dev.items():
            for meta in sessions:
                sid = meta["session_id"]
                week = meta.get("week") or "unknown"
                session_turns = [t for t in turns_by_session.get(sid, []) if t.get("event_type") != "skill"]

                score = _session_trust(meta, session_turns)
                dev_trusts[key][week].append(score)
                dev_interruptions[key] += meta.get("user_interruptions") or 0

                for t in session_turns:
                    mode = t.get("permission_mode")
                    if mode:
                        dev_permission_modes[key][mode] += 1

        results: dict[str, dict] = {}
        for key in dev_trusts:
            all_scores = [s for scores in dev_trusts[key].values() for s in scores]
            avg = round(sum(all_scores) / len(all_scores) * 100, 1) if all_scores else 0.0

            modes = dev_permission_modes.get(key, {})
            total_mode_events = sum(modes.values()) or 1
            permission_dist = {
                m: round(c / total_mode_events * 100, 1) for m, c in modes.items()
            }

            by_week = {
                w: round(sum(scores) / len(scores) * 100, 1)
                for w, scores in dev_trusts[key].items()
            }

            results[key] = {
                "developer_key": key,
                "trust_index": avg,
                "total_interruptions": dev_interruptions.get(key, 0),
                "permission_mode_distribution": permission_dist,
                "by_week": by_week,
            }

        return results
