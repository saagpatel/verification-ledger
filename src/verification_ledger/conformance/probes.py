"""Positive and adversarial probes, per invariant. See SPEC.md § "Conformance scoring".

Positive probes assert the legitimate path still works; adversarial probes assert
the attack is blocked. An invariant passes only when both hold, so neither
degenerate store scores well: a block-everything store fails the positives, an
allow-everything store fails the adversarials.

The suite is one rule applied to one set of scenarios. :data:`SCENARIOS` is a
fixed list of scripted operations (writes of every kind, seeding, activation,
in-band and out-of-band promotion followed by activation, pruning, attacks and
a forced loss), shared with the independent oracle in the property tests.
:func:`run_scenario` applies each step through the adapter and, before and
after it, takes a **full-state snapshot**: every record the store lists, read
back through ``read()`` exactly as a consumer would get it, plus the record
count. The change is checked against :data:`ALLOWED_DELTAS`, one table of which
fields of which record each operation may change; everything else must be
identical. Every violation is credited to the invariant that owns it
(:data:`OWNERS`), whichever scenario found it, and on top of the deltas every
record's envelope must agree with its current label after every step (VL-3).

Each invariant is graded on every scenario: a positive scenario is one of its
positive probes, an adversarial scenario one of its adversarial probes, and a
probe passes only if that scenario credited the invariant nothing. Each probe
receives a store on a clean slate (``fresh()``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
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
    and must, change, with their required new value. ``removes``: ``"none"``;
    ``"prune"`` (non-durable records only, down to exactly the store's cap); or
    ``"cap"`` (a store that keeps to its cap automatically may remove the oldest
    non-durable records, but never a durable one and never below the cap).
    Anything not listed here must be identical before and after, on every
    record.
    """

    creates: int = 0
    new_record: Mapping[str, object] = field(default_factory=lambda: {})
    target: Mapping[str, object] = field(default_factory=lambda: {})
    removes: str = "none"


# The new record of a write is checked against the request instead: its label
# by the VL-1 clamp rule, its payload and durability as written, never born
# actionable. Those are filled in per call (see _expected_write).
ALLOWED_DELTAS: dict[str, Allowed] = {
    "write": Allowed(creates=1, removes="cap"),
    "seed_operator": Allowed(
        creates=1,
        new_record={"trust": OPERATOR, "actionable": False, "durable": False},
        removes="cap",
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
    # A seed that drops a record breaks retention, like a write that does.
    "seed_operator": {**_VL2_ALL, "removed": "VL-4", "envelope": "VL-3"},
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
    order: Sequence[int] = (),
) -> set[str]:
    """The invariants whose part of the contract this one operation broke.

    ``order`` is the creation order of the records the caller created, oldest
    first, as the caller observed it. Record ids carry no order: the adapter
    contract lets a store hand out ids in any order.
    """
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
    elif allowed.removes == "cap":
        if removed:
            plain = [
                i
                for i in order
                if i in before.records and before.records[i].durable is False
            ]
            left = [i for i, v in after.records.items() if v.durable is False]
            if removed != set(plain[: len(removed)]) or len(left) < cap:
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


# --- the scenarios: shared with the independent oracle in the property tests ---


@dataclass(frozen=True)
class Step:
    """One scripted operation. ``name`` labels the record a write or seed
    creates, or names the record an activate, promote or forced loss acts on;
    ``expect_*`` steps check the store's health report."""

    op: str  # write | seed | activate | promote | prune | expect_healthy | force_loss | expect_loss_reported
    name: str = ""
    trust: str | None = None
    channel: str = IN_BAND
    durable: bool = False


@dataclass(frozen=True)
class Scenario:
    name: str
    kind: str  # "positive" | "adversarial"
    steps: Callable[[int], list[Step]]  # the store's prune cap -> the script


def _others() -> list[Step]:
    # One record of every kind, so "changes nothing else" covers each label.
    return [
        Step("write", "o-agent", AGENT),
        Step("write", "o-ingested", INGESTED, durable=True),
        Step("seed", "o-operator"),
    ]


def _legitimate_writes(cap: int) -> list[Step]:
    return [
        *_others(),
        Step("write", "agent", AGENT),
        Step("write", "ingested", INGESTED),
        Step("write", "unlabeled", None),
        Step("write", "durable", AGENT, durable=True),
    ]


# Every kind of record a writer can ask for: agent, ingested, unlabeled, and an
# in-band operator request (stored clamped to agent).
WRITE_REQUESTS: tuple[tuple[str, str | None], ...] = (
    ("agent", AGENT),
    ("ingested", INGESTED),
    ("unlabeled", None),
    ("clamped", OPERATOR),
)


def _operator_fast_path(cap: int) -> list[Step]:
    # Seeded operator records, and records of every kind once promoted
    # out-of-band, activate in one in-band call. An in-band promotion of a
    # record that is already operator reports that it is operator.
    steps = [
        *_others(),
        Step("seed", "directive"),
        Step("promote", "directive", channel=IN_BAND),
        Step("activate", "directive"),
    ]
    for label, trust in WRITE_REQUESTS:
        steps += [
            Step("write", f"ceremony-{label}", trust),
            Step("promote", f"ceremony-{label}", channel=OUT_OF_BAND),
            Step("activate", f"ceremony-{label}"),
        ]
    return steps


def _retention_to_cap(cap: int) -> list[Step]:
    return [
        Step("write", "kept", AGENT, durable=True),
        *(Step("write", f"n{i}", AGENT) for i in range(cap + 3)),
        Step("prune"),
        Step("expect_healthy"),
    ]


def _laundering_attacks(cap: int) -> list[Step]:
    # Every kind of non-operator record, written, then activated in-band and
    # promoted in-band, which must all be refused.
    steps = _others()
    for label, trust in WRITE_REQUESTS:
        steps += [
            Step("write", label, trust),
            Step("activate", label),
            Step("write", f"{label}-2", trust),
            Step("promote", f"{label}-2", channel=IN_BAND),
        ]
    return steps


def _aggressive_prune(cap: int) -> list[Step]:
    return [
        *(Step("write", f"n{i}", AGENT) for i in range(cap + 3)),
        Step("write", "shipped", INGESTED, durable=True),
        Step("prune"),
        Step("expect_healthy"),
    ]


def _durable_loss(cap: int) -> list[Step]:
    # SPEC VL-4: "a forced orphan or durable loss is reported by health with a
    # non-zero counter".
    return [
        Step("write", "lost", AGENT, durable=True),
        Step("write", "n", AGENT),
        Step("expect_healthy"),
        Step("force_loss", "lost"),
        Step("expect_loss_reported"),
    ]


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("legitimate writes", "positive", _legitimate_writes),
    Scenario("operator fast path", "positive", _operator_fast_path),
    Scenario("retention to cap", "positive", _retention_to_cap),
    Scenario("laundering attacks", "adversarial", _laundering_attacks),
    Scenario("aggressive prune", "adversarial", _aggressive_prune),
    Scenario("durable loss", "adversarial", _durable_loss),
)


# --- the step function: every operation checked against the table ---


def run_scenario(a: LedgerAdapter, scenario: Scenario) -> set[str]:
    """Run one scenario on a clean store; return every invariant it caught
    violating, whichever step or invariant the scenario was written for.

    Besides the state deltas, every value the contract defines an operation to
    return is checked: a write or seed returns the id of the one record it
    created, activate whether the record is now actionable, promote whether it
    is now operator, prune nothing. Creation order is the order this run saw
    records appear, never the numeric order of their ids.
    """
    a.fresh()
    found: set[str] = set()
    ids: dict[str, int] = {}
    order: list[int] = []
    cap = a.prune_cap()

    def step(
        op: str,
        call: Callable[[], object],
        target: int | None,
        new: Mapping[str, object] | None = None,
    ) -> object:
        before = snapshot(a)
        result = call()
        after = snapshot(a)
        created = set(after.records) - set(before.records)
        returned_id = isinstance(result, int) and not isinstance(result, bool)
        tid = result if target is None and returned_id else target
        assert tid is None or isinstance(tid, int)
        if ALLOWED_DELTAS[op].creates and tid not in created:
            # The returned value does not name the record the call created.
            found.add(OWNERS[op]["created"])
            if len(created) == 1:
                tid = next(iter(created))  # judge the record it did create
        found.update(
            violations(
                op,
                before,
                after,
                target=tid,
                new_record=new,
                cap=cap,
                order=order,
            )
        )
        order.extend(i for i in [tid] if i in created)
        order.extend(sorted(created - set(order)))
        return tid if ALLOWED_DELTAS[op].creates else result

    for s in scenario.steps(cap):
        if s.op == "write":
            rid = step(
                "write",
                lambda s=s: a.write(
                    s.name, source_trust=s.trust, channel=s.channel, durable=s.durable
                ),
                None,
                _expected_write(s.trust, s.channel, s.name, s.durable),
            )
            if isinstance(rid, int) and not isinstance(rid, bool):
                ids[s.name] = rid
                clamp = s.trust == OPERATOR and s.channel == IN_BAND
                if a.clamp_recorded(rid) is not clamp:
                    found.add("VL-1")
        elif s.op == "seed":
            rid = step(
                "seed_operator",
                lambda s=s: a.seed_operator(s.name),
                None,
                {"payload": s.name},
            )
            if isinstance(rid, int) and not isinstance(rid, bool):
                ids[s.name] = rid
        elif s.op in ("activate", "promote"):
            rid = ids.get(s.name)
            view = None if rid is None else snapshot(a).records.get(rid)
            if rid is None or view is None:
                continue  # already reported
            if s.op == "activate":
                operator = view.trust == OPERATOR
                op = "activate_operator" if operator else "activate_refused"
                result = step(op, lambda rid=rid: a.activate(rid, channel=IN_BAND), rid)
                if result is not operator:
                    found.add(OWNERS[op]["actionable"])
            else:
                oob = s.channel == OUT_OF_BAND
                op = "promote_out_of_band" if oob else "promote_in_band"
                result = step(
                    op, lambda rid=rid, s=s: a.promote(rid, channel=s.channel), rid
                )
                if result is not (oob or view.trust == OPERATOR):
                    found.add(OWNERS[op]["trust"])  # "whether it is now operator"
        elif s.op == "prune":
            if step("prune", a.prune, None) is not None:
                found.add(OWNERS["prune"]["removed"])
        elif s.op == "expect_healthy":
            if a.health_ok() is not True or a.durable_violations() != 0:
                found.add("VL-4")
        elif s.op == "force_loss":
            if s.name in ids and ids[s.name] in snapshot(a).records:
                a.force_durable_loss(ids[s.name])
        elif s.op == "expect_loss_reported":
            if a.health_ok() is not False or a.durable_violations() <= 0:
                found.add("VL-4")
    return found


def _probe(invariant: str, scenario: Scenario) -> Probe:
    def check(a: LedgerAdapter) -> bool:
        return invariant not in run_scenario(a, scenario)

    return Probe(invariant, scenario.kind, scenario.name, check)


def all_probes() -> list[Probe]:
    """Every probe: each invariant judged on every scenario.

    A scenario credits each violation it finds to the invariant that owns it,
    so a fault the clamp scenario happens to expose (say, a clamped write that
    comes out actionable) fails VL-2 there, not only where VL-2's own
    scenarios would look.
    """
    return [
        _probe(inv, scenario)
        for inv in ("VL-1", "VL-2", "VL-3", "VL-4")
        for scenario in SCENARIOS
    ]
