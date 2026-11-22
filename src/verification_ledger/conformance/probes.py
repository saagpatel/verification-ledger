"""Positive and adversarial probes, per invariant. See SPEC.md § "Conformance scoring".

Positive probes assert the legitimate path still works; adversarial probes assert
the attack is blocked. An invariant passes only when both hold, which is what
makes the score un-gameable: a block-everything store fails the positives, an
allow-everything store fails the adversarials.

Every probe is built from one rule. Before and after every mutating operation
(write, seed, activate, promote in-band and out-of-band, prune) the probe takes
a **full-state snapshot** of the store: every record the store lists, read back
through ``read()`` exactly as a consumer would get it (label, actionability,
payload, durability and the whole envelope), plus the record count. It then
checks the change against :data:`ALLOWED_DELTAS`, one table that says, per
operation, which fields of which record may change and to what; everything else
must be identical. Each kind of violation belongs to exactly one invariant
(:data:`OWNERS`), so a probe for VL-k fails only on VL-k's violations. On top of
the deltas, every record's envelope must agree with its current label after
every operation (VL-3), new record or not.

Each probe receives a store on a clean slate (the scorer calls ``fresh()``
first) and returns True iff the store behaved correctly for that probe.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

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


# --- the full-state snapshot ---

# The exposed fields of one record, as a reader gets them.
FIELDS = ("trust", "actionable", "payload", "durable", "envelope")


@dataclass(frozen=True)
class View:
    """One record's complete exposed state, read back through ``read()``."""

    trust: str
    actionable: bool
    payload: str | None
    durable: bool | None
    envelope: tuple[tuple[str, str], ...] | None  # the whole envelope, as read

    def value(self, name: str) -> object:
        return getattr(self, name)


@dataclass(frozen=True)
class Snapshot:
    """Every record the store lists, plus whether its listing is consistent."""

    records: Mapping[int, View]
    consistent: bool  # count_records() agrees with record_ids(), and each id reads


def snapshot(a: LedgerAdapter) -> Snapshot:
    """The store's complete exposed state."""
    ids = list(a.record_ids())
    records: dict[int, View] = {}
    consistent = a.count_records() == len(ids) == len(set(ids))
    for rid in ids:
        r = a.read(rid)
        if r is None:
            consistent = False
            continue
        env = None if r.envelope is None else tuple(sorted(r.envelope.items()))
        records[rid] = View(r.source_trust, r.actionable, r.payload, r.durable, env)
    return Snapshot(records, consistent)


def envelope_agrees(view: View) -> bool:
    """VL-3: the envelope is present, has the boundary kind and the record's label."""
    if view.envelope is None:
        return False
    env = dict(view.envelope)
    return (
        env.get("kind") == BOUNDARY_KIND
        and env.get("source_trust") == view.trust
        and bool(env.get("warning"))
    )


# --- the allowed-delta table: the whole contract in one place ---


@dataclass(frozen=True)
class Allowed:
    """What one operation may change.

    ``creates``: the number of new records the operation itself returns (its id
    is the target). ``new_record``: what that record must look like (fields not
    listed are free). ``target``: the fields of the existing target that may,
    and must, change, with their required new value. ``removes``: ``"none"``, or
    ``"prune"`` (non-durable records only, down to the store's cap). Anything
    not listed here must be identical before and after, on every record.
    """

    creates: int = 0
    new_record: Mapping[str, object] = field(default_factory=lambda: {})
    target: Mapping[str, object] = field(default_factory=lambda: {})
    removes: str = "none"


# The new record of a write is checked against the request instead: its label
# by the VL-1 clamp rule, its payload and durability as written, never born
# actionable. Those are filled in per call (see _expected_write).
ALLOWED_DELTAS: dict[str, Allowed] = {
    "write": Allowed(creates=1),
    "seed_operator": Allowed(
        creates=1,
        new_record={"trust": OPERATOR, "actionable": False, "durable": False},
    ),
    "activate_operator": Allowed(target={"actionable": True}),
    "activate_refused": Allowed(),
    "promote_in_band": Allowed(),
    "promote_out_of_band": Allowed(target={"trust": OPERATOR}),
    "prune": Allowed(removes="prune"),
}

# Which invariant owns each kind of violation, per operation. "created" is an
# extra record; "removed" is a record that disappeared. Every envelope
# violation, of any operation, is VL-3's.
_VL2_ALL = {
    f: "VL-2"
    for f in ("trust", "actionable", "payload", "durable", "created", "removed")
}
_VL4_ALL = {
    f: "VL-4"
    for f in ("trust", "actionable", "payload", "durable", "created", "removed")
}
OWNERS: dict[str, dict[str, str]] = {
    "write": {
        "trust": "VL-1",  # the label, per the clamp rule
        "payload": "VL-1",
        "created": "VL-1",  # a write creates exactly its own record
        "actionable": "VL-2",  # never born actionable
        "durable": "VL-4",  # the durable tag the retention exemption relies on
        "removed": "VL-4",  # a write must never drop a record
        "envelope": "VL-3",
    },
    "seed_operator": {**_VL2_ALL, "envelope": "VL-3"},
    "activate_operator": {**_VL2_ALL, "envelope": "VL-3"},
    "activate_refused": {**_VL2_ALL, "envelope": "VL-3"},
    "promote_in_band": {**_VL2_ALL, "envelope": "VL-3"},
    "promote_out_of_band": {**_VL2_ALL, "envelope": "VL-3"},
    "prune": {**_VL4_ALL, "envelope": "VL-3"},
}


def _expected_write(
    trust: str | None, channel: str, payload: str, durable: bool
) -> dict[str, object]:
    label = AGENT if trust is None else trust
    if label == OPERATOR and channel == IN_BAND:
        label = AGENT  # VL-1: clamped
    return {"trust": label, "actionable": False, "payload": payload, "durable": durable}


def violations(
    op: str,
    before: Snapshot,
    after: Snapshot,
    *,
    target: int | None,
    new_record: Mapping[str, object] | None = None,
    cap: int = 0,
) -> set[str]:
    """The invariants whose part of the contract this one operation broke."""
    allowed = ALLOWED_DELTAS[op]
    owner = OWNERS[op]
    found: set[str] = set()
    if not (before.consistent and after.consistent):
        found.update(owner.values())
    created = set(after.records) - set(before.records)
    removed = set(before.records) - set(after.records)
    expected_new: set[int] = (
        {target} if allowed.creates and target is not None else set()
    )
    if created - expected_new or expected_new - created:
        found.add(owner["created"])
    if allowed.removes == "prune":
        non_durable = [i for i, v in before.records.items() if v.durable is not True]
        survivors = [i for i in non_durable if i in after.records]
        if any(before.records[i].durable is not False for i in removed) or len(
            survivors
        ) != min(cap, len(non_durable)):
            found.add(owner["removed"])
    elif removed:
        found.add(owner["removed"])
    # The operation's own new record.
    if expected_new and target is not None and target in after.records:
        view = after.records[target]
        rules = {**allowed.new_record, **(new_record or {})}
        for name, want in rules.items():
            if view.value(name) != want:
                found.add(owner[name])
    # Every record present before and after.
    for rid in set(before.records) & set(after.records):
        was, now = before.records[rid], after.records[rid]
        for name in ("trust", "actionable", "payload", "durable"):
            want = allowed.target.get(name) if rid == target else None
            if want is not None:
                if now.value(name) != want:
                    found.add(owner[name])
            elif now.value(name) != was.value(name):
                found.add(owner[name])
        if now.trust == was.trust and now.envelope != was.envelope:
            found.add(owner["envelope"])
    # VL-3: every record's envelope tells the truth, new or not.
    if not all(envelope_agrees(v) for v in after.records.values()):
        found.add(owner["envelope"])
    return found


class _Run:
    """Applies operations through the adapter, checking each against the table."""

    def __init__(self, a: LedgerAdapter) -> None:
        self.a = a
        self.found: set[str] = set()

    def _step(
        self,
        op: str,
        call: Callable[[], object],
        target: int | None = None,
        new_record: Mapping[str, object] | None = None,
    ) -> object:
        before = snapshot(self.a)
        result = call()
        tid = (
            result
            if isinstance(result, int)
            and not isinstance(result, bool)
            and target is None
            else target
        )
        after = snapshot(self.a)
        cap = self.a.prune_cap() if op == "prune" else 0
        self.found |= violations(
            op, before, after, target=tid, new_record=new_record, cap=cap
        )
        return result

    def write(
        self,
        payload: str,
        trust: str | None,
        channel: str = IN_BAND,
        durable: bool = False,
    ) -> int:
        result = self._step(
            "write",
            lambda: self.a.write(
                payload, source_trust=trust, channel=channel, durable=durable
            ),
            new_record=_expected_write(trust, channel, payload, durable),
        )
        assert isinstance(result, int)
        return result

    def seed(self, payload: str) -> int:
        result = self._step(
            "seed_operator",
            lambda: self.a.seed_operator(payload),
            new_record={"payload": payload},
        )
        assert isinstance(result, int)
        return result

    def activate(self, rid: int) -> bool:
        """Activate in-band. The gate decides on the target's current label, so
        the expected outcome is read from the store, not assumed."""
        view = snapshot(self.a).records.get(rid)
        operator = view is not None and view.trust == OPERATOR
        op = "activate_operator" if operator else "activate_refused"
        result = self._step(
            op, lambda: self.a.activate(rid, channel=IN_BAND), target=rid
        )
        if result is not operator:
            self.found.add(OWNERS[op]["actionable"])
        return bool(result)

    def promote(self, rid: int, channel: str) -> bool:
        op = "promote_out_of_band" if channel == OUT_OF_BAND else "promote_in_band"
        return bool(
            self._step(op, lambda: self.a.promote(rid, channel=channel), target=rid)
        )

    def prune(self) -> None:
        self._step("prune", self.a.prune)

    def bystanders(self) -> None:
        # One record of every kind, so "changes nothing else" covers each label.
        self.write("b-agent", AGENT)
        self.write("b-ingested", INGESTED, durable=True)
        self.seed("b-operator")

    def clean(self, invariant: str) -> bool:
        return invariant not in self.found


# --- VL-1: provenance typing ---


def _vl1_stored(
    trust: str | None, expect_clamp: bool
) -> Callable[[LedgerAdapter], bool]:
    def check(a: LedgerAdapter) -> bool:
        run = _Run(a)
        run.bystanders()
        rid = run.write("x", trust)
        return run.clean("VL-1") and a.clamp_recorded(rid) is expect_clamp

    return check


# --- VL-2: promotion gate ---


def _vl2_pos_operator_activates(a: LedgerAdapter) -> bool:
    # The operator fast path: one in-band call, and the persisted state shows it
    # (only the target's actionability changes; it stays operator, intact).
    # seed_operator is the store's own out-of-band ceremony, whatever it is.
    run = _Run(a)
    run.bystanders()
    rid = run.seed("directive")
    run.activate(rid)
    return run.clean("VL-2")


def _vl2_pos_out_of_band_promotion(a: LedgerAdapter) -> bool:
    # The ceremony changes one exact record's label, and nothing else.
    run = _Run(a)
    run.bystanders()
    rid = run.write("work", AGENT)
    run.promote(rid, OUT_OF_BAND)
    return run.clean("VL-2")


def _vl2_refused_activation(trust: str) -> Callable[[LedgerAdapter], bool]:
    def check(a: LedgerAdapter) -> bool:
        run = _Run(a)
        run.bystanders()
        rid = run.write("work", trust)
        run.activate(rid)
        return run.clean("VL-2")

    return check


def _vl2_refused_promotion(trust: str) -> Callable[[LedgerAdapter], bool]:
    def check(a: LedgerAdapter) -> bool:
        run = _Run(a)
        run.bystanders()
        rid = run.write("work", trust)
        run.promote(rid, IN_BAND)
        return run.clean("VL-2")

    return check


def _vl2_adv_record_not_born_actionable(a: LedgerAdapter) -> bool:
    # A record must not be actionable before any activation call: a store that
    # pre-activates records launders untrusted content while activate() still
    # politely reports "refused".
    run = _Run(a)
    run.bystanders()
    run.write("work", AGENT)
    run.write("scraped", INGESTED)
    return run.clean("VL-2")


# --- VL-3: instruction-boundary envelope ---


def _vl3_pos_legitimate_operations(a: LedgerAdapter) -> bool:
    # Every envelope agrees with its record's current label after every
    # legitimate operation, including a record whose label just changed.
    run = _Run(a)
    run.bystanders()
    op_id = run.seed("directive")
    run.activate(op_id)
    rid = run.write("work", AGENT)
    run.promote(rid, OUT_OF_BAND)
    run.write("kept", AGENT, durable=True)
    for i in range(a.prune_cap() + 1):
        run.write(f"n{i}", AGENT)
    run.prune()
    return run.clean("VL-3")


def _vl3_adv_attacks(a: LedgerAdapter) -> bool:
    # The same after every attack: ingested content, an in-band operator mint,
    # refused activations and refused in-band promotions.
    run = _Run(a)
    run.bystanders()
    for trust in (INGESTED, OPERATOR):
        run.activate(run.write("refused", trust))
        run.promote(run.write("in-band", trust), IN_BAND)
    return run.clean("VL-3")


# --- VL-4: retention invariant ---


def _vl4_prune(durable_first: bool) -> Callable[[LedgerAdapter], bool]:
    def check(a: LedgerAdapter) -> bool:
        # Overflow the store's declared cap, then prune: exactly ``cap``
        # non-durable records survive, every durable one survives unchanged,
        # and the store still reports healthy.
        run = _Run(a)
        cap = a.prune_cap()
        if durable_first:
            run.write("keep", AGENT, durable=True)
        for i in range(cap + 3):
            run.write(f"n{i}", AGENT)
        if not durable_first:
            run.write("shipped", INGESTED, durable=True)
        run.prune()
        return (
            run.clean("VL-4") and a.count_records() == cap + 1 and a.health_ok() is True
        )

    return check


def _vl4_adv_durable_loss_is_reported(a: LedgerAdapter) -> bool:
    # SPEC VL-4: "a forced orphan or durable loss is reported by health with a
    # non-zero counter". Healthy first, then destroy the durable record behind
    # the API and require health to notice, with a counter.
    run = _Run(a)
    rid = run.write("shipped", AGENT, durable=True)
    run.write("n", AGENT)
    if (
        not run.clean("VL-4")
        or a.health_ok() is not True
        or a.durable_violations() != 0
    ):
        return False
    a.force_durable_loss(rid)
    return a.health_ok() is False and a.durable_violations() > 0


def all_probes() -> list[Probe]:
    """Every probe, tagged by invariant and kind."""
    return [
        Probe("VL-1", "positive", "agent stored as agent", _vl1_stored(AGENT, False)),
        Probe(
            "VL-1",
            "positive",
            "ingested stored as ingested",
            _vl1_stored(INGESTED, False),
        ),
        Probe(
            "VL-1", "positive", "no label defaults to agent", _vl1_stored(None, False)
        ),
        Probe(
            "VL-1",
            "adversarial",
            "in-band operator clamped to agent and recorded",
            _vl1_stored(OPERATOR, True),
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
            "out-of-band promotion changes one label",
            _vl2_pos_out_of_band_promotion,
        ),
        Probe(
            "VL-2",
            "adversarial",
            "agent not activatable in-band",
            _vl2_refused_activation(AGENT),
        ),
        Probe(
            "VL-2",
            "adversarial",
            "ingested not activatable in-band",
            _vl2_refused_activation(INGESTED),
        ),
        Probe(
            "VL-2",
            "adversarial",
            "agent not promotable in-band",
            _vl2_refused_promotion(AGENT),
        ),
        Probe(
            "VL-2",
            "adversarial",
            "ingested not promotable in-band",
            _vl2_refused_promotion(INGESTED),
        ),
        Probe(
            "VL-2",
            "adversarial",
            "record not born actionable",
            _vl2_adv_record_not_born_actionable,
        ),
        Probe(
            "VL-3",
            "positive",
            "envelopes stay true through legitimate operations",
            _vl3_pos_legitimate_operations,
        ),
        Probe(
            "VL-3",
            "adversarial",
            "envelopes stay true through attacks",
            _vl3_adv_attacks,
        ),
        Probe(
            "VL-4",
            "positive",
            "non-durable pruned to cap",
            _vl4_prune(durable_first=True),
        ),
        Probe(
            "VL-4",
            "adversarial",
            "durable survives aggressive prune",
            _vl4_prune(durable_first=False),
        ),
        Probe(
            "VL-4",
            "adversarial",
            "durable loss reported by health",
            _vl4_adv_durable_loss_is_reported,
        ),
    ]
