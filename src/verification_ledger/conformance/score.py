"""Scoring: per-invariant pass plus the top-line Ledger Conformance Score.

An invariant passes only if its positive-probe pass rate and its adversarial-probe
block rate are both 1.0 — no partial credit within an invariant. The top-line
score is the fraction of the four invariants fully passed. See SPEC.md §
"Conformance scoring".
"""

from __future__ import annotations

from dataclasses import dataclass

from verification_ledger.conformance.contract import LedgerAdapter
from verification_ledger.conformance.probes import Probe, all_probes

INVARIANTS = ("VL-1", "VL-2", "VL-3", "VL-4")


@dataclass(frozen=True)
class InvariantScore:
    """One invariant's result across its positive and adversarial probes."""

    invariant: str
    positive_passed: int
    positive_total: int
    adversarial_passed: int
    adversarial_total: int

    @property
    def positive_rate(self) -> float:
        return (
            self.positive_passed / self.positive_total if self.positive_total else 1.0
        )

    @property
    def adversarial_rate(self) -> float:
        return (
            self.adversarial_passed / self.adversarial_total
            if self.adversarial_total
            else 1.0
        )

    @property
    def passed(self) -> bool:
        return self.positive_rate == 1.0 and self.adversarial_rate == 1.0


@dataclass(frozen=True)
class ConformanceReport:
    """The full result: per-invariant scores and the top-line conformance score."""

    invariants: tuple[InvariantScore, ...]

    @property
    def passed_count(self) -> int:
        return sum(1 for s in self.invariants if s.passed)

    @property
    def total_invariants(self) -> int:
        return len(self.invariants)

    @property
    def score(self) -> float:
        return self.passed_count / self.total_invariants if self.invariants else 0.0

    @property
    def perfect(self) -> bool:
        return self.passed_count == self.total_invariants


def _run_probe(adapter: LedgerAdapter, probe: Probe) -> bool:
    # Each probe runs on a clean slate; a probe that raises counts as a failure,
    # never as a pass — a store that errors has not enforced the invariant.
    adapter.fresh()
    try:
        return probe.check(adapter) is True
    except Exception:
        return False


def score_adapter(
    adapter: LedgerAdapter, probes: list[Probe] | None = None
) -> ConformanceReport:
    """Grade ``adapter`` against every probe and return the conformance report."""
    probe_list = all_probes() if probes is None else probes
    scores: list[InvariantScore] = []
    for invariant in INVARIANTS:
        pos = [
            p for p in probe_list if p.invariant == invariant and p.kind == "positive"
        ]
        adv = [
            p
            for p in probe_list
            if p.invariant == invariant and p.kind == "adversarial"
        ]
        scores.append(
            InvariantScore(
                invariant=invariant,
                positive_passed=sum(1 for p in pos if _run_probe(adapter, p)),
                positive_total=len(pos),
                adversarial_passed=sum(1 for p in adv if _run_probe(adapter, p)),
                adversarial_total=len(adv),
            )
        )
    return ConformanceReport(invariants=tuple(scores))
