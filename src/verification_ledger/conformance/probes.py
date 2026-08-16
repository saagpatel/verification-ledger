"""Positive and adversarial probes, per invariant. See SPEC.md § "Conformance scoring".

Positive probes assert the legitimate path still works; adversarial probes assert
the attack is blocked. An invariant passes only when both hold — which is what
makes the score un-gameable: a block-everything store fails the positives, an
allow-everything store fails the adversarials.

Each probe receives a store on a clean slate (the scorer calls ``fresh()`` first)
and returns True iff the store behaved correctly for that probe.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from verification_ledger.conformance.contract import (
    AGENT,
    IN_BAND,
    INGESTED,
    OPERATOR,
    OUT_OF_BAND,
    LedgerAdapter,
)

BOUNDARY_KIND = "stored_data_not_instructions"


@dataclass(frozen=True)
class Probe:
    """One conformance check against a store."""

    invariant: str  # "VL-1" .. "VL-4"
    kind: str  # "positive" | "adversarial"
    name: str
    check: Callable[[LedgerAdapter], bool]


# --- VL-1: provenance typing ---


def _vl1_pos_agent_stored(a: LedgerAdapter) -> bool:
    r = a.read(a.write("x", source_trust=AGENT, channel=IN_BAND, durable=False))
    return r is not None and r.source_trust == AGENT


def _vl1_pos_ingested_stored(a: LedgerAdapter) -> bool:
    r = a.read(a.write("x", source_trust=INGESTED, channel=IN_BAND, durable=False))
    return r is not None and r.source_trust == INGESTED


def _vl1_adv_in_band_operator_clamped(a: LedgerAdapter) -> bool:
    # The attack: mint operator trust from the in-band channel. Must be clamped.
    r = a.read(a.write("x", source_trust=OPERATOR, channel=IN_BAND, durable=False))
    return r is not None and r.source_trust != OPERATOR


# --- VL-2: promotion gate ---


def _vl2_pos_operator_activates(a: LedgerAdapter) -> bool:
    # An operator record (seeded via the out-of-band channel) activates in one call,
    # and the PERSISTED state reflects it — not just the return value.
    rid = a.write(
        "directive", source_trust=OPERATOR, channel=OUT_OF_BAND, durable=False
    )
    activated = a.activate(rid, channel=IN_BAND)
    r = a.read(rid)
    return activated is True and r is not None and r.actionable is True


def _vl2_pos_out_of_band_promotion_makes_operator(a: LedgerAdapter) -> bool:
    rid = a.write("work", source_trust=AGENT, channel=IN_BAND, durable=False)
    a.promote(rid, channel=OUT_OF_BAND)
    r = a.read(rid)
    return r is not None and r.source_trust == OPERATOR


def _vl2_adv_agent_not_activatable_in_band(a: LedgerAdapter) -> bool:
    # Check persisted state, not only the return: a store that reports "refused"
    # while the record is actually actionable is laundering.
    rid = a.write("work", source_trust=AGENT, channel=IN_BAND, durable=False)
    activated = a.activate(rid, channel=IN_BAND)
    r = a.read(rid)
    return activated is False and r is not None and r.actionable is False


def _vl2_adv_record_not_born_actionable(a: LedgerAdapter) -> bool:
    # A non-operator record must not be actionable before any activation call — a
    # store that pre-activates records launders untrusted content while activate()
    # still politely reports "refused".
    rid = a.write("work", source_trust=AGENT, channel=IN_BAND, durable=False)
    r = a.read(rid)
    return r is not None and r.actionable is False


def _vl2_adv_agent_not_promotable_in_band(a: LedgerAdapter) -> bool:
    rid = a.write("work", source_trust=AGENT, channel=IN_BAND, durable=False)
    a.promote(rid, channel=IN_BAND)
    r = a.read(rid)
    return r is not None and r.source_trust != OPERATOR


def _vl2_adv_ingested_not_activatable_in_band(a: LedgerAdapter) -> bool:
    rid = a.write("scraped", source_trust=INGESTED, channel=IN_BAND, durable=False)
    activated = a.activate(rid, channel=IN_BAND)
    r = a.read(rid)
    return activated is False and r is not None and r.actionable is False


# --- VL-3: instruction-boundary envelope ---


def _vl3_pos_envelope_present(a: LedgerAdapter) -> bool:
    r = a.read(a.write("x", source_trust=AGENT, channel=IN_BAND, durable=False))
    return (
        r is not None
        and r.envelope is not None
        and r.envelope.get("kind") == BOUNDARY_KIND
    )


def _vl3_adv_envelope_reflects_real_trust(a: LedgerAdapter) -> bool:
    # The envelope must carry the record's real (most-suspicious) trust, not a
    # laundered, safer-looking label.
    r = a.read(
        a.write("scraped", source_trust=INGESTED, channel=IN_BAND, durable=False)
    )
    return (
        r is not None
        and r.envelope is not None
        and r.envelope.get("source_trust") == INGESTED
    )


# --- VL-4: retention invariant ---


def _vl4_pos_prunes_non_durable_to_cap(a: LedgerAdapter) -> bool:
    a.write("keep", source_trust=AGENT, channel=IN_BAND, durable=True)
    for i in range(5):
        a.write(f"n{i}", source_trust=AGENT, channel=IN_BAND, durable=False)
    a.prune(keep=2)
    # 1 durable + newest 2 non-durable = 3; and the store still reports healthy.
    return a.count_records() == 3 and a.health_ok() is True


def _vl4_adv_durable_survives_aggressive_prune(a: LedgerAdapter) -> bool:
    rid = a.write("shipped", source_trust=AGENT, channel=IN_BAND, durable=True)
    a.prune(keep=0)  # ask to prune everything
    return a.read(rid) is not None  # the durable record must remain


def all_probes() -> list[Probe]:
    """Every probe, tagged by invariant and kind."""
    return [
        Probe("VL-1", "positive", "agent stored as agent", _vl1_pos_agent_stored),
        Probe(
            "VL-1", "positive", "ingested stored as ingested", _vl1_pos_ingested_stored
        ),
        Probe(
            "VL-1",
            "adversarial",
            "in-band operator clamped",
            _vl1_adv_in_band_operator_clamped,
        ),
        Probe(
            "VL-2",
            "positive",
            "operator activates in one call",
            _vl2_pos_operator_activates,
        ),
        Probe(
            "VL-2",
            "positive",
            "out-of-band promotion mints operator",
            _vl2_pos_out_of_band_promotion_makes_operator,
        ),
        Probe(
            "VL-2",
            "adversarial",
            "agent not activatable in-band",
            _vl2_adv_agent_not_activatable_in_band,
        ),
        Probe(
            "VL-2",
            "adversarial",
            "record not born actionable",
            _vl2_adv_record_not_born_actionable,
        ),
        Probe(
            "VL-2",
            "adversarial",
            "agent not promotable in-band",
            _vl2_adv_agent_not_promotable_in_band,
        ),
        Probe(
            "VL-2",
            "adversarial",
            "ingested not activatable in-band",
            _vl2_adv_ingested_not_activatable_in_band,
        ),
        Probe(
            "VL-3", "positive", "read carries the envelope", _vl3_pos_envelope_present
        ),
        Probe(
            "VL-3",
            "adversarial",
            "envelope reflects real trust",
            _vl3_adv_envelope_reflects_real_trust,
        ),
        Probe(
            "VL-4",
            "positive",
            "non-durable pruned to cap",
            _vl4_pos_prunes_non_durable_to_cap,
        ),
        Probe(
            "VL-4",
            "adversarial",
            "durable survives aggressive prune",
            _vl4_adv_durable_survives_aggressive_prune,
        ),
    ]
