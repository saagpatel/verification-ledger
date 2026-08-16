"""The invariant monitor and its configurable failure policy."""

from __future__ import annotations

import pytest

from verification_ledger.invariants import (
    FailurePolicy,
    InvariantMonitor,
    InvariantViolation,
)


def test_holding_invariant_returns_true_and_records_nothing() -> None:
    monitor = InvariantMonitor(FailurePolicy.RAISE)
    assert monitor.always(True, "should hold") is True
    assert monitor.violations == ()


def test_violation_raises_under_raise_policy() -> None:
    monitor = InvariantMonitor(FailurePolicy.RAISE)
    with pytest.raises(InvariantViolation):
        monitor.always(False, "broken")


def test_violation_is_recorded_not_raised_under_report_policy() -> None:
    monitor = InvariantMonitor(FailurePolicy.REPORT)
    assert monitor.always(False, "broken") is False
    assert monitor.violations == ("broken",)


def test_default_policy_is_raise() -> None:
    with pytest.raises(InvariantViolation):
        InvariantMonitor().always(False, "broken")
