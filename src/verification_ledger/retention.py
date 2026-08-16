"""VL-4 retention policy: the pure selection of what may be pruned. See SPEC.md § VL-4.

The durable exemption is absolute: a durable record is never selected for pruning
and durable records do not count against the cap. This module is pure (no I/O) so
the exemption is exhaustively testable; the ledger applies the selection, owns the
deletion, and runs the health check.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class RetentionCandidate:
    """One record considered for pruning: its id and whether it is durable."""

    record_id: int
    durable: bool


def select_prunable(
    candidates: Sequence[RetentionCandidate], *, keep: int
) -> list[int]:
    """Return the record ids to prune: non-durable records past the newest ``keep``.

    ``candidates`` are ordered newest first. Durable candidates are exempt — never
    selected, and never counted against the cap, so the newest ``keep``
    *non-durable* records are retained regardless of how many durable records exist.
    """
    if keep < 0:
        raise ValueError("keep must be non-negative")
    prunable = [c.record_id for c in candidates if not c.durable]
    return prunable[keep:]
