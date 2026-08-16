"""VL-2 promotion gate — the keystone. See SPEC.md § VL-2.

The security property is a **channel separation**, not a Python guard: the
in-band surface (library calls a model can reach, the MCP tools) can never make a
non-operator record actionable, and can never promote. Operator trust is minted
only on the out-of-band channel — the operator's terminal running the promotion
CLI, a process a model cannot invoke. This module holds the pure decision logic
so it is exhaustively testable in isolation; the ledger consumes it and owns the
mutation.
"""

from __future__ import annotations

from dataclasses import dataclass

from verification_ledger.model import Channel, Trust


@dataclass(frozen=True)
class GateDecision:
    """The outcome of a gate evaluation: allow the transition, or refuse with a reason."""

    allowed: bool
    reason: str


def evaluate_activation(source_trust: Trust) -> GateDecision:
    """Decide whether a record may cross into actionable intent.

    Operator-trust records activate in one in-band call — the legitimate fast
    path must stay open. Every non-operator record is refuse-until-promoted: no
    in-band path makes it actionable; it must first be promoted out-of-band. The
    decision is trust-only by design — the channel cannot loosen it.
    """
    if source_trust is Trust.OPERATOR:
        return GateDecision(True, "operator-trust record: actionable")
    return GateDecision(
        False,
        f"non-operator source_trust '{source_trust}': refuse-until-promoted; "
        "promote the exact record out-of-band before activation",
    )


def evaluate_promotion(channel: Channel) -> GateDecision:
    """Decide whether a promotion-to-operator may proceed.

    Promotion mints operator trust and is available only on the out-of-band
    channel. An in-band promotion attempt is refused — a model cannot promote.
    """
    if channel is Channel.OUT_OF_BAND:
        return GateDecision(True, "out-of-band promotion: permitted")
    return GateDecision(
        False,
        "promotion requires the out-of-band channel; the in-band surface cannot "
        "mint operator trust",
    )
