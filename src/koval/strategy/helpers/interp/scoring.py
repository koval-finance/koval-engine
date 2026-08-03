"""Pure additive scoring for INTERPRETATION nodes.

No Backtrader, no state, no entities — just points arithmetic, so the scoring
policy is a single swappable function behind a stable signature. The setup-score
and context-score nodes both call this; a weighted or learned scorer would
implement the same signature.
"""

from __future__ import annotations

from collections.abc import Iterable


def score_additive(points: dict[str, int], keys: Iterable[str]) -> tuple[int, str]:
    """Sum ``points[k]`` for each key in ``keys`` that has an entry.

    Returns ``(total, breakdown)`` where ``breakdown`` is a human string like
    ``"bos +20, choch +25"`` (empty when nothing scored). Keys absent from
    ``points`` contribute nothing and are omitted from the breakdown.
    """
    total = 0
    parts: list[str] = []
    for key in keys:
        if key in points:
            pts = points[key]
            total += pts
            parts.append(f"{key} {pts:+d}")
    return total, ", ".join(parts)
