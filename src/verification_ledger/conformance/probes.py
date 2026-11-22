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
    StoredRecord,
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
    rid = a.write("x", source_trust=AGENT, channel=IN_BAND, durable=False)
    r = a.read(rid)
    return r is not None and r.source_trust == AGENT and not a.clamp_recorded(rid)


def _vl1_pos_ingested_stored(a: LedgerAdapter) -> bool:
    rid = a.write("x", source_trust=INGESTED, channel=IN_BAND, durable=False)
    r = a.read(rid)
    return r is not None and r.source_trust == INGESTED and not a.clamp_recorded(rid)


def _vl1_pos_no_label_defaults_to_agent(a: LedgerAdapter) -> bool:
    # SPEC VL-1: "a record with no label defaults to agent".
    rid = a.write("x", source_trust=None, channel=IN_BAND, durable=False)
    r = a.read(rid)
    return r is not None and r.source_trust == AGENT and not a.clamp_recorded(rid)


def _vl1_adv_in_band_operator_clamped(a: LedgerAdapter) -> bool:
    # The attack: mint operator trust from the in-band channel. SPEC VL-1 says
    # it is clamped to exactly ``agent`` and the clamp is recorded; a store that
    # silently relabels it (to anything) has not implemented the clamp.
    rid = a.write("x", source_trust=OPERATOR, channel=IN_BAND, durable=False)
    r = a.read(rid)
    return r is not None and r.source_trust == AGENT and a.clamp_recorded(rid)


# --- VL-2: promotion gate ---


def _vl2_pos_operator_activates(a: LedgerAdapter) -> bool:
    # An operator record (seeded via the store's own out-of-band ceremony —
    # whatever that is) activates in one in-band call, and the PERSISTED state
    # reflects it, not just the return value. Using the adapter's seed_operator
    # keeps this probe agnostic to how the store mints operator trust.
    rid = a.seed_operator("directive")
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


def _snapshot(a: LedgerAdapter, ids: list[int]) -> list[StoredRecord | None]:
    return [a.read(rid) for rid in ids]


def _bystanders(a: LedgerAdapter) -> list[int]:
    # One record of every kind, so "mutates nothing" covers each label.
    return [
        a.write("b-agent", source_trust=AGENT, channel=IN_BAND, durable=False),
        a.write("b-ingested", source_trust=INGESTED, channel=IN_BAND, durable=True),
        a.seed_operator("b-operator"),
    ]


def _in_band_promotion_refused(a: LedgerAdapter, trust: str) -> bool:
    # SPEC VL-2: an in-band promotion attempt is refused and mutates nothing.
    # Persisted state, not just the label: a store whose in-band promote() makes
    # the record actionable while leaving it labeled agent is laundering.
    others = _bystanders(a)
    rid = a.write("work", source_trust=trust, channel=IN_BAND, durable=False)
    before = _snapshot(a, [*others, rid])
    a.promote(rid, channel=IN_BAND)
    r = a.read(rid)
    return (
        r is not None
        and r.source_trust == trust
        and r.actionable is False
        and _snapshot(a, [*others, rid]) == before
    )


def _vl2_adv_agent_not_promotable_in_band(a: LedgerAdapter) -> bool:
    return _in_band_promotion_refused(a, AGENT)


def _vl2_adv_ingested_not_promotable_in_band(a: LedgerAdapter) -> bool:
    return _in_band_promotion_refused(a, INGESTED)


def _vl2_adv_out_of_band_promotion_is_exact(a: LedgerAdapter) -> bool:
    # SPEC VL-2: the ceremony is "applied to one exact record". Promoting the
    # middle of three records must leave the other two exactly as they were.
    ids = [
        a.write(f"w{i}", source_trust=AGENT, channel=IN_BAND, durable=False)
        for i in range(3)
    ]
    before = _snapshot(a, ids)
    a.promote(ids[1], channel=OUT_OF_BAND)
    after = _snapshot(a, ids)
    target = after[1]
    return (
        target is not None
        and target.source_trust == OPERATOR
        and after[0] == before[0]
        and after[2] == before[2]
    )


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


def _vl3_pos_operator_envelope_says_operator(a: LedgerAdapter) -> bool:
    # The legitimate path: an operator record's envelope carries its real label.
    r = a.read(a.seed_operator("directive"))
    return (
        r is not None
        and r.envelope is not None
        and r.envelope.get("kind") == BOUNDARY_KIND
        and r.envelope.get("source_trust") == OPERATOR
    )


def _vl3_adv_agent_envelope_says_agent(a: LedgerAdapter) -> bool:
    # An envelope that labels agent content "operator" launders it at read time
    # even if the stored label is honest.
    r = a.read(a.write("x", source_trust=AGENT, channel=IN_BAND, durable=False))
    return (
        r is not None
        and r.envelope is not None
        and r.envelope.get("source_trust") == AGENT
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
    # Overflow the store's own declared cap, then prune. The legitimate outcome:
    # exactly `cap` non-durable records survive, the one durable record is exempt,
    # and the store still reports healthy.
    cap = a.prune_cap()
    a.write("keep", source_trust=AGENT, channel=IN_BAND, durable=True)
    for i in range(cap + 3):
        a.write(f"n{i}", source_trust=AGENT, channel=IN_BAND, durable=False)
    a.prune()
    return a.count_records() == cap + 1 and a.health_ok() is True


def _vl4_adv_durable_survives_aggressive_prune(a: LedgerAdapter) -> bool:
    # Flood well past the cap so pruning is forced to act, then confirm the durable
    # record still survives — retention exemption must hold under real pressure.
    cap = a.prune_cap()
    rid = a.write("shipped", source_trust=AGENT, channel=IN_BAND, durable=True)
    for i in range(cap + 3):
        a.write(f"n{i}", source_trust=AGENT, channel=IN_BAND, durable=False)
    a.prune()
    return a.read(rid) is not None  # the durable record must remain


def _vl4_adv_durable_loss_is_reported(a: LedgerAdapter) -> bool:
    # SPEC VL-4: "a forced orphan or durable loss is reported by health with a
    # non-zero counter". Healthy first, then destroy the durable record behind
    # the API and require health to notice, with a counter.
    rid = a.write("shipped", source_trust=AGENT, channel=IN_BAND, durable=True)
    a.write("n", source_trust=AGENT, channel=IN_BAND, durable=False)
    if a.health_ok() is not True or a.durable_violations() != 0:
        return False
    a.force_durable_loss(rid)
    return a.health_ok() is False and a.durable_violations() > 0


def all_probes() -> list[Probe]:
    """Every probe, tagged by invariant and kind."""
    return [
        Probe("VL-1", "positive", "agent stored as agent", _vl1_pos_agent_stored),
        Probe(
            "VL-1", "positive", "ingested stored as ingested", _vl1_pos_ingested_stored
        ),
        Probe(
            "VL-1",
            "positive",
            "no label defaults to agent",
            _vl1_pos_no_label_defaults_to_agent,
        ),
        Probe(
            "VL-1",
            "adversarial",
            "in-band operator clamped to agent and recorded",
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
            "VL-2",
            "adversarial",
            "ingested not promotable in-band",
            _vl2_adv_ingested_not_promotable_in_band,
        ),
        Probe(
            "VL-2",
            "adversarial",
            "out-of-band promotion touches one exact record",
            _vl2_adv_out_of_band_promotion_is_exact,
        ),
        Probe(
            "VL-3", "positive", "read carries the envelope", _vl3_pos_envelope_present
        ),
        Probe(
            "VL-3",
            "positive",
            "operator envelope says operator",
            _vl3_pos_operator_envelope_says_operator,
        ),
        Probe(
            "VL-3",
            "adversarial",
            "envelope reflects real trust",
            _vl3_adv_envelope_reflects_real_trust,
        ),
        Probe(
            "VL-3",
            "adversarial",
            "agent envelope says agent",
            _vl3_adv_agent_envelope_says_agent,
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
        Probe(
            "VL-4",
            "adversarial",
            "durable loss reported by health",
            _vl4_adv_durable_loss_is_reported,
        ),
    ]
