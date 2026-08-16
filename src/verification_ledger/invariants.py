"""Loud-assertion vocabulary with a configurable failure policy. See SPEC.md § VL-4.

``always(condition, message)`` enforces a protocol invariant. Under ``RAISE`` a
violation raises ``InvariantViolation``; under ``REPORT`` it records the violation
for a health check to surface and returns ``False``. Either way the violation is
never silent — a library embedded in a host fleet can choose to be crashed on
corruption (RAISE) or to degrade its health and keep serving (REPORT), but not to
pass a violation unnoticed.
"""

from __future__ import annotations

import logging
from enum import StrEnum

logger = logging.getLogger("verification_ledger.invariants")


class FailurePolicy(StrEnum):
    """What happens when an invariant is violated."""

    RAISE = "raise"
    REPORT = "report"


class InvariantViolation(AssertionError):
    """A protocol invariant was violated."""


class InvariantMonitor:
    """Evaluates invariants under a chosen failure policy and records violations."""

    def __init__(self, policy: FailurePolicy = FailurePolicy.RAISE) -> None:
        self.policy = policy
        self._violations: list[str] = []

    def always(self, condition: bool, message: str) -> bool:
        """Assert an invariant. Returns True if it holds.

        On violation the message is always logged; under ``RAISE`` it then raises
        ``InvariantViolation``; under ``REPORT`` it records the message and returns
        ``False``.
        """
        if condition:
            return True
        logger.error("invariant violated: %s", message)
        self._violations.append(message)
        if self.policy is FailurePolicy.RAISE:
            raise InvariantViolation(message)
        return False

    @property
    def violations(self) -> tuple[str, ...]:
        """The violations recorded so far (non-empty only under ``REPORT``)."""
        return tuple(self._violations)
