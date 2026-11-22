"""Property tests for the conformance scorer's claims (SPEC.md § "Conformance scoring").

Three kinds of claim are tested here:

1. Arithmetic. An invariant passes only if its positive pass rate and its
   adversarial block rate are both 1.0; the Ledger Conformance Score is the
   fraction of invariants passed; each probe runs on a clean slate; a probe that
   raises counts as a failure.
2. Degenerate stores. Block-everything and allow-everything stores both fail.
3. Soundness ("the suite mechanically proves a given store enforces them").
   A family of configurable stores, each a mix of honest and broken mechanisms,
   is graded twice: once by the suite, and once by an independent oracle that
   checks each invariant's SPEC.md pass condition directly. Completeness (a store
   the oracle accepts passes the suite) and soundness (a store the suite passes
   satisfies the spec) both hold over the whole family. Soundness first failed
   for every invariant; the probes were tightened, and each former 1.00-scoring
   laundering store is pinned as a case that now fails exactly its invariant.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from verification_ledger.conformance import contract as c
from verification_ledger.conformance.adapters.reference import ReferenceAdapter
from verification_ledger.conformance.contract import LedgerAdapter, StoredRecord
from verification_ledger.conformance.probes import (
    BOUNDARY_KIND,
    SCENARIOS,
    Probe,
    Scenario,
    all_probes,
)
from verification_ledger.conformance.score import INVARIANTS, score_adapter

CAP = 3
TRUSTS = (c.OPERATOR, c.AGENT, c.INGESTED)
CHANNELS = (c.IN_BAND, c.OUT_OF_BAND)

Clamp = Literal["agent", "ingested", "none", "agent_plus_shadow"]
Activate = Literal[
    "gate", "allow", "block", "insert_operator", "gate_spill", "activate_and_demote"
]
PromoteInBand = Literal["refuse", "mint", "make_actionable", "insert_operator"]
PromoteOob = Literal["exact", "all_records", "refuse", "insert_operator"]
EnvelopeMode = Literal["honest", "agent_as_operator", "operator_label", "missing"]
PruneMode = Literal["correct", "drop_durable", "noop"]
HealthMode = Literal["honest", "always_ok"]


@dataclass(frozen=True)
class Policy:
    """One configurable store. The first value of every field is the honest one."""

    clamp: Clamp = "agent"  # what an in-band operator write is stored as
    activate: Activate = "gate"
    promote_in_band: PromoteInBand = "refuse"
    promote_oob: PromoteOob = "exact"
    born_actionable: bool = False
    envelope: EnvelopeMode = "honest"
    prune: PruneMode = "correct"
    health: HealthMode = "honest"
    # Keep to the cap on every write and seed (oldest non-durable first), as the
    # adapter contract allows; still an honest store.
    auto_prune: bool = False


HONEST = Policy()


@dataclass
class _Row:
    trust: str
    actionable: bool
    durable: bool
    payload: str = ""


@dataclass
class PolicyStore:
    """A ``LedgerAdapter`` whose every mechanism is chosen by a ``Policy``."""

    policy: Policy
    rows: dict[int, _Row] = field(default_factory=lambda: {})
    receipts: set[int] = field(default_factory=lambda: set())
    clamped: set[int] = field(default_factory=lambda: set())
    next_id: int = 1

    def fresh(self) -> None:
        self.rows = {}
        self.receipts = set()
        self.clamped = set()
        self.next_id = 1

    def write(
        self, payload: str, *, source_trust: str | None, channel: str, durable: bool
    ) -> int:
        trust = c.AGENT if source_trust is None else source_trust
        relabeled = False
        if trust == c.OPERATOR and channel == c.IN_BAND:
            trust = {
                "agent": c.AGENT,
                "ingested": c.INGESTED,
                "none": c.OPERATOR,
                "agent_plus_shadow": c.AGENT,
            }[self.policy.clamp]
            relabeled = trust != c.OPERATOR
        rid = self._insert(trust, durable, payload)
        if relabeled:
            self.clamped.add(rid)
            if self.policy.clamp == "agent_plus_shadow":
                self._insert(c.OPERATOR, False, payload)  # an operator copy on the side
        if self.policy.auto_prune:
            self._keep_to_cap()
        return rid

    def _keep_to_cap(self) -> None:
        plain = sorted(i for i, r in self.rows.items() if not r.durable)
        for i in plain[: max(0, len(plain) - CAP)]:
            del self.rows[i]

    def _insert(self, trust: str, durable: bool, payload: str = "side") -> int:
        rid = self.next_id
        self.next_id += 1
        self.rows[rid] = _Row(trust, self.policy.born_actionable, durable, payload)
        if durable:
            self.receipts.add(rid)
        return rid

    def clamp_recorded(self, record_id: int) -> bool:
        return record_id in self.clamped

    def read(self, record_id: int) -> StoredRecord | None:
        row = self.rows.get(record_id)
        if row is None:
            return None
        mode = self.policy.envelope
        if mode == "missing":
            env = None
        else:
            label = row.trust
            if mode == "operator_label" or (
                mode == "agent_as_operator" and row.trust == c.AGENT
            ):
                label = c.OPERATOR
            env = {"kind": BOUNDARY_KIND, "source_trust": label, "warning": "w"}
        return StoredRecord(
            record_id,
            row.trust,
            row.actionable,
            env,
            payload=row.payload,
            durable=row.durable,
        )

    def activate(self, record_id: int, *, channel: str) -> bool:
        row = self.rows[record_id]
        if self.policy.activate == "allow":
            row.actionable = True
            return True
        if self.policy.activate == "block":
            return False
        if row.trust == c.OPERATOR:
            row.actionable = True
            if self.policy.activate == "gate_spill":
                for other in self.rows.values():
                    other.actionable = True
            elif self.policy.activate == "activate_and_demote":
                row.trust = c.AGENT  # leaves an actionable non-operator record
        elif self.policy.activate == "insert_operator":
            self._insert(c.OPERATOR, False)  # refuses, but mints a record
        return row.actionable

    def promote(self, record_id: int, *, channel: str) -> bool:
        row = self.rows[record_id]
        if channel == c.IN_BAND:
            if self.policy.promote_in_band == "mint":
                row.trust = c.OPERATOR
            elif self.policy.promote_in_band == "make_actionable":
                row.actionable = True
            elif self.policy.promote_in_band == "insert_operator":
                self._insert(c.OPERATOR, False)  # refuses, but mints a record
        elif self.policy.promote_oob in ("exact", "insert_operator"):
            row.trust = c.OPERATOR
            if self.policy.promote_oob == "insert_operator":
                self._insert(c.OPERATOR, False)
        elif self.policy.promote_oob == "all_records":
            for other in self.rows.values():
                other.trust = c.OPERATOR
        return row.trust == c.OPERATOR

    def seed_operator(self, payload: str) -> int:
        return self.write(
            payload, source_trust=c.OPERATOR, channel=c.OUT_OF_BAND, durable=False
        )

    def prune(self) -> None:
        if self.policy.prune == "noop":
            return
        newest_first = sorted(self.rows, reverse=True)
        if self.policy.prune == "correct":
            newest_first = [i for i in newest_first if not self.rows[i].durable]
        for i in newest_first[CAP:]:
            del self.rows[i]

    def prune_cap(self) -> int:
        return CAP

    def count_records(self) -> int:
        return len(self.rows)

    def record_ids(self) -> list[int]:
        return list(self.rows)

    def durable_violations(self) -> int:
        if self.policy.health == "always_ok":
            return 0
        return sum(
            1 for r in self.receipts if r not in self.rows or not self.rows[r].durable
        )

    def health_ok(self) -> bool:
        return self.durable_violations() == 0

    def force_durable_loss(self, record_id: int) -> None:
        self.force_lose(record_id)

    # --- oracle-only access: not part of the adapter contract ---

    def snapshot(self) -> dict[int, tuple[str, bool, bool, str]]:
        return {
            i: (r.trust, r.actionable, r.durable, r.payload)
            for i, r in self.rows.items()
        }

    def force_lose(self, record_id: int) -> None:
        del self.rows[record_id]


policies = st.builds(
    Policy,
    clamp=st.sampled_from(["agent", "ingested", "none", "agent_plus_shadow"]),
    activate=st.sampled_from(
        [
            "gate",
            "allow",
            "block",
            "insert_operator",
            "gate_spill",
            "activate_and_demote",
        ]
    ),
    promote_in_band=st.sampled_from(
        ["refuse", "mint", "make_actionable", "insert_operator"]
    ),
    promote_oob=st.sampled_from(["exact", "all_records", "refuse", "insert_operator"]),
    born_actionable=st.booleans(),
    envelope=st.sampled_from(
        ["honest", "agent_as_operator", "operator_label", "missing"]
    ),
    prune=st.sampled_from(["correct", "drop_durable", "noop"]),
    health=st.sampled_from(["honest", "always_ok"]),
    auto_prune=st.booleans(),
)


# --- the oracle: each invariant's SPEC.md pass condition, checked directly ---
#
# Written independently of probes.py: its own copy of the allowed-delta
# contract, and a model-based check instead of the probes' snapshot diff. Only
# the scenario list (SCENARIOS) is shared, so the suite and the oracle exercise
# the same operations and the suite can never look at less than the oracle. After every operation the oracle predicts the
# store's complete exposed state from SPEC.md and compares every field of
# every record, then adopts what it observed so each operation is judged on
# its own change.

_ORACLE_FIELDS = ("trust", "actionable", "payload", "durable")

# Per operation: which invariant a violation of each kind belongs to. Mirrors
# SPEC.md: labels at write time are VL-1, actionability and everything the
# gate's operations touch are VL-2, durability and record loss are VL-4, the
# envelope is always VL-3.
_ORACLE_OWNER: dict[str, dict[str, str]] = {
    "write": {
        "trust": "VL-1",
        "payload": "VL-1",
        "extra": "VL-1",
        "actionable": "VL-2",
        "durable": "VL-4",
        "lost": "VL-4",
    },
    **{
        op: {
            k: "VL-2"
            for k in ("trust", "payload", "extra", "actionable", "durable", "lost")
        }
        for op in ("seed", "activate", "refuse", "promote", "promote_in_band")
    },
    # A seed that drops a record breaks retention.
    "seed": {
        **{k: "VL-2" for k in ("trust", "payload", "extra", "actionable", "durable")},
        "lost": "VL-4",
    },
    "prune": {
        k: "VL-4"
        for k in ("trust", "payload", "extra", "actionable", "durable", "lost")
    },
}


def _observe(a: LedgerAdapter) -> dict[int, StoredRecord] | None:
    ids = a.record_ids()
    if a.count_records() != len(ids):
        return None
    out: dict[int, StoredRecord] = {}
    for rid in ids:
        r = a.read(rid)
        if r is None:
            return None
        out[rid] = r
    return out


def _true_envelope(r: StoredRecord) -> bool:
    env = r.envelope
    return (
        env is not None
        and env.get("kind") == BOUNDARY_KIND
        and env.get("source_trust") == r.source_trust
        and bool(env.get("warning"))
    )


class _Oracle:
    """Runs a scenario against a store and collects which invariants it broke."""

    def __init__(self, a: LedgerAdapter) -> None:
        self.a = a
        a.fresh()
        self.state: dict[int, StoredRecord] = {}
        self.broken: set[str] = set()

    def _judge(
        self,
        op: str,
        predicted: dict[int, dict[str, object]],
        *,
        prune_cap: int | None = None,
        keeps_cap: int | None = None,
    ) -> None:
        owner = _ORACLE_OWNER[op]
        seen = _observe(self.a)
        if seen is None:
            self.broken.update(owner.values())
            self.broken.add("VL-3")
            return
        extra = set(seen) - set(predicted)
        gone = set(predicted) - set(seen)
        if extra:
            self.broken.add(owner["extra"])
        if prune_cap is not None:
            plain = [i for i in predicted if predicted[i]["durable"] is False]
            kept = [i for i in plain if i in seen]
            if (gone - set(plain)) or len(kept) != min(prune_cap, len(plain)):
                self.broken.add(owner["lost"])
        elif keeps_cap is not None and gone:
            # A write or seed may keep the store at its cap on its own: it may
            # drop the oldest non-durable records, never a durable one, and
            # never so many that fewer than the cap remain.
            existing = sorted(i for i in self.state if predicted[i]["durable"] is False)
            oldest_first = set(existing[: len(gone)])
            remaining = [i for i in seen if seen[i].durable is False]
            if gone != oldest_first or len(remaining) < keeps_cap:
                self.broken.add(owner["lost"])
        elif gone:
            self.broken.add(owner["lost"])
        for rid in set(seen) & set(predicted):
            r = seen[rid]
            actual = {
                "trust": r.source_trust,
                "actionable": r.actionable,
                "payload": r.payload,
                "durable": r.durable,
            }
            for name in _ORACLE_FIELDS:
                if actual[name] != predicted[rid][name]:
                    self.broken.add(owner[name])
            old = self.state.get(rid)
            if (
                old is not None
                and old.source_trust == r.source_trust
                and old.envelope != r.envelope
            ):
                self.broken.add("VL-3")
        if not all(_true_envelope(r) for r in seen.values()):
            self.broken.add("VL-3")
        self.state = seen

    def _now(self) -> dict[int, dict[str, object]]:
        return {
            rid: {
                "trust": r.source_trust,
                "actionable": r.actionable,
                "payload": r.payload,
                "durable": r.durable,
            }
            for rid, r in self.state.items()
        }

    def write(
        self,
        payload: str,
        trust: str | None,
        durable: bool = False,
        channel: str = c.IN_BAND,
    ) -> int:
        rid = self.a.write(
            payload, source_trust=trust, channel=channel, durable=durable
        )
        clamped = trust == c.OPERATOR and channel == c.IN_BAND
        label = c.AGENT if trust is None or clamped else trust
        predicted = self._now()
        predicted[rid] = {
            "trust": label,
            "actionable": False,
            "payload": payload,
            "durable": durable,
        }
        self._judge("write", predicted, keeps_cap=self.a.prune_cap())
        if self.a.clamp_recorded(rid) is not clamped:
            self.broken.add("VL-1")
        return rid

    def seed(self, payload: str) -> int:
        rid = self.a.seed_operator(payload)
        predicted = self._now()
        predicted[rid] = {
            "trust": c.OPERATOR,
            "actionable": False,
            "payload": payload,
            "durable": False,
        }
        self._judge("seed", predicted, keeps_cap=self.a.prune_cap())
        return rid

    def activate(self, rid: int) -> None:
        if rid not in self.state:
            return  # already reported as lost
        operator = self.state[rid].source_trust == c.OPERATOR
        result = self.a.activate(rid, channel=c.IN_BAND)
        predicted = self._now()
        if operator:
            predicted[rid] = {**predicted[rid], "actionable": True}
        if result is not operator:
            self.broken.add("VL-2")
        self._judge("activate" if operator else "refuse", predicted)

    def promote(self, rid: int, channel: str) -> None:
        if rid not in self.state:
            return  # already reported as lost
        self.a.promote(rid, channel=channel)
        predicted = self._now()
        if channel == c.OUT_OF_BAND:
            predicted[rid] = {**predicted[rid], "trust": c.OPERATOR}
            self._judge("promote", predicted)
        else:
            self._judge("promote_in_band", predicted)

    def prune(self) -> None:
        self.a.prune()
        self._judge("prune", self._now(), prune_cap=self.a.prune_cap())


def _oracle_run(a: LedgerAdapter, scenario: Scenario) -> set[str]:
    """The oracle's own interpretation of one shared scenario."""
    o = _Oracle(a)
    named: dict[str, int] = {}
    for step in scenario.steps(a.prune_cap()):
        if step.op == "write":
            named[step.name] = o.write(
                step.name, step.trust, step.durable, step.channel
            )
        elif step.op == "seed":
            named[step.name] = o.seed(step.name)
        elif step.op == "activate":
            o.activate(named[step.name])
        elif step.op == "promote":
            o.promote(named[step.name], step.channel)
        elif step.op == "prune":
            o.prune()
        elif step.op == "expect_healthy":
            if a.health_ok() is not True or a.durable_violations() != 0:
                o.broken.add("VL-4")
        elif step.op == "force_loss":
            if named[step.name] in o.state:
                a.force_durable_loss(named[step.name])
        elif step.op == "expect_loss_reported":
            if a.health_ok() is not False or a.durable_violations() <= 0:
                o.broken.add("VL-4")
        else:  # pragma: no cover - a new step kind must be taught to the oracle
            raise AssertionError(f"unknown step {step.op}")
    return o.broken


def _oracle_broken(a: LedgerAdapter) -> set[str]:
    broken: set[str] = set()
    for scenario in SCENARIOS:
        broken |= _oracle_run(a, scenario)
    return broken


def spec_vl1(a: LedgerAdapter) -> bool:
    return "VL-1" not in _oracle_broken(a)


def spec_vl2(a: LedgerAdapter) -> bool:
    return "VL-2" not in _oracle_broken(a)


def spec_vl3(a: LedgerAdapter) -> bool:
    return "VL-3" not in _oracle_broken(a)


def spec_vl4(a: LedgerAdapter) -> bool:
    return "VL-4" not in _oracle_broken(a)


SPEC: dict[str, Callable[[LedgerAdapter], bool]] = {
    "VL-1": spec_vl1,
    "VL-2": spec_vl2,
    "VL-3": spec_vl3,
    "VL-4": spec_vl4,
}


def suite_passes(policy: Policy) -> dict[str, bool]:
    report = score_adapter(PolicyStore(policy))
    return {s.invariant: s.passed for s in report.invariants}


# --- 1. arithmetic ---


@given(policy=policies)
def test_score_is_the_fraction_of_invariants_fully_passed(policy: Policy) -> None:
    store = PolicyStore(policy)
    report = score_adapter(store)
    probes = all_probes()
    assert [s.invariant for s in report.invariants] == list(INVARIANTS)
    for s in report.invariants:
        mine = [p for p in probes if p.invariant == s.invariant]
        pos = [p for p in mine if p.kind == "positive"]
        adv = [p for p in mine if p.kind == "adversarial"]
        assert (s.positive_total, s.adversarial_total) == (len(pos), len(adv))

        def ran(p: Probe) -> bool:
            store.fresh()
            return p.check(store) is True

        assert s.positive_passed == sum(ran(p) for p in pos)
        assert s.adversarial_passed == sum(ran(p) for p in adv)
        assert s.passed is (
            s.positive_passed == len(pos) and s.adversarial_passed == len(adv)
        )
    assert report.score == report.passed_count / 4
    assert report.perfect is (report.passed_count == 4)


Outcome = Literal["pass", "fail", "raise", "truthy"]


def _probe(invariant: str, kind: str, outcome: Outcome) -> Probe:
    def check(_a: LedgerAdapter) -> bool:
        if outcome == "raise":
            raise RuntimeError("store error")
        if outcome == "truthy":
            return 1  # type: ignore[return-value]  # truthy but not True
        return outcome == "pass"

    return Probe(invariant, kind, f"{invariant}-{kind}-{outcome}", check)


@given(
    specs=st.lists(
        st.tuples(
            st.sampled_from(INVARIANTS),
            st.sampled_from(["positive", "adversarial"]),
            st.sampled_from(["pass", "fail", "raise", "truthy"]),
        ),
        max_size=30,
    )
)
def test_scoring_rule_on_arbitrary_probe_outcomes(
    specs: list[tuple[str, str, Outcome]],
) -> None:
    probes = [_probe(i, k, o) for i, k, o in specs]
    fresh_calls = 0

    class Counting(PolicyStore):
        def fresh(self) -> None:
            nonlocal fresh_calls
            fresh_calls += 1
            super().fresh()

    report = score_adapter(Counting(HONEST), probes)
    assert fresh_calls == len(probes)  # every probe runs on a clean slate
    passed = 0
    for s in report.invariants:
        mine = [(k, o) for i, k, o in specs if i == s.invariant]
        # A raise, or a truthy non-True, counts as a failure, never as a pass.
        pos_ok = all(o == "pass" for k, o in mine if k == "positive")
        adv_ok = all(o == "pass" for k, o in mine if k == "adversarial")
        assert s.passed is (pos_ok and adv_ok)
        passed += s.passed
    assert report.score == passed / 4


def test_every_invariant_has_positive_and_adversarial_probes() -> None:
    # Without this, an invariant with no probes of one kind would pass vacuously
    # (score.py treats an empty kind as rate 1.0).
    probes = all_probes()
    for inv in INVARIANTS:
        kinds = {p.kind for p in probes if p.invariant == inv}
        assert kinds == {"positive", "adversarial"}, inv


# --- 2. degenerate stores ---

ALLOW_ALL = Policy(
    clamp="none", activate="allow", promote_in_band="mint", promote_oob="exact"
)
BLOCK_ALL = Policy(activate="block", promote_in_band="refuse", promote_oob="refuse")


def test_allow_everything_fails_the_adversarial_probes_of_vl1_and_vl2() -> None:
    report = score_adapter(PolicyStore(ALLOW_ALL))
    by = {s.invariant: s for s in report.invariants}
    for inv in ("VL-1", "VL-2"):
        assert by[inv].adversarial_rate < 1.0 and not by[inv].passed
    assert report.score < 1.0


def test_block_everything_fails_the_positive_probes_of_vl2() -> None:
    report = score_adapter(PolicyStore(BLOCK_ALL))
    by = {s.invariant: s for s in report.invariants}
    assert by["VL-2"].positive_rate < 1.0 and not by["VL-2"].passed
    assert report.score < 1.0


def test_block_everything_passes_vl1_as_corrected_spec_says() -> None:
    # SPEC.md used to say block-everything also fails VL-1's positives. It does
    # not: blocking the gate leaves label storage intact, so only VL-2 fails
    # (the repo's own _BlockEverythingAdapter test agrees). The spec sentence
    # was corrected; this pins the corrected claim.
    report = score_adapter(PolicyStore(BLOCK_ALL))
    failed = {s.invariant for s in report.invariants if not s.passed}
    assert failed == {"VL-2"}
    assert report.score == 0.75


class _StoresEverythingAsIngested(PolicyStore):
    """A store that also blocks honest labels: every write is kept as ingested."""

    def write(
        self, payload: str, *, source_trust: str | None, channel: str, durable: bool
    ) -> int:
        rid = super().write(
            payload, source_trust=c.INGESTED, channel=channel, durable=durable
        )
        return rid


def test_a_store_that_blocks_labels_fails_vl1_positives() -> None:
    # The other half of the corrected sentence: refusing to keep labels is what
    # costs VL-1's positive probes.
    by = {
        s.invariant: s
        for s in score_adapter(_StoresEverythingAsIngested(BLOCK_ALL)).invariants
    }
    assert by["VL-1"].positive_rate < 1.0 and not by["VL-1"].passed


@given(policy=policies)
def test_any_blocking_or_allowing_gate_scores_below_one(policy: Policy) -> None:
    # The site's claim, over the whole family: blocking a legitimate path or
    # allowing a laundering path always costs at least one invariant.
    passes = suite_passes(policy)
    if policy.activate == "block" or policy.promote_oob == "refuse":
        assert passes["VL-2"] is False
    if (
        policy.activate == "allow"
        or policy.promote_in_band == "mint"
        or policy.born_actionable
    ):
        assert passes["VL-2"] is False
    if policy.clamp == "none":
        assert passes["VL-1"] is False


# --- 3. completeness and soundness against the oracle ---


def test_oracle_accepts_the_honest_store_and_the_reference() -> None:
    store = PolicyStore(HONEST)
    assert all(check(store) for check in SPEC.values())
    assert all(suite_passes(HONEST).values())
    assert score_adapter(ReferenceAdapter()).perfect


@given(policy=policies)
def test_suite_is_complete_a_spec_conformant_store_passes(policy: Policy) -> None:
    passes = suite_passes(policy)
    store = PolicyStore(policy)
    for inv, check in SPEC.items():
        if check(store):
            assert passes[inv], f"{inv}: spec-conformant store failed the suite"


def _sound(policy: Policy, inv: str) -> None:
    if suite_passes(policy)[inv]:
        assert SPEC[inv](PolicyStore(policy)), f"{inv} PASS on a store that violates it"


# The four stores below scored a perfect 1.00 against the first version of the
# probes while each violated one invariant (Hypothesis shrank each to a single
# changed mechanism). They are pinned twice: as @example cases of the soundness
# properties, and as direct tests that each now fails exactly its invariant.

LAUNDERING_STORES: dict[str, list[Policy]] = {
    "VL-1": [
        replace(HONEST, clamp="ingested"),
        # Round 2 review: side records the first tightened probes did not see.
        replace(HONEST, clamp="agent_plus_shadow"),
    ],
    "VL-2": [
        replace(HONEST, promote_in_band="make_actionable"),
        replace(HONEST, promote_oob="all_records"),
        replace(HONEST, promote_in_band="insert_operator"),
        replace(HONEST, promote_oob="insert_operator"),
        replace(HONEST, activate="insert_operator"),
        replace(HONEST, activate="gate_spill"),
        # Round 3 review: activation that demotes the record it activates.
        replace(HONEST, activate="activate_and_demote"),
    ],
    "VL-3": [replace(HONEST, envelope="agent_as_operator")],
    "VL-4": [replace(HONEST, health="always_ok")],
}


@pytest.mark.parametrize(
    ("invariant", "policy"),
    [(inv, p) for inv, ps in LAUNDERING_STORES.items() for p in ps],
)
def test_former_perfect_scorer_now_fails_exactly_its_invariant(
    invariant: str, policy: Policy
) -> None:
    report = score_adapter(PolicyStore(policy))
    failed = {s.invariant for s in report.invariants if not s.passed}
    assert failed == {invariant}
    assert report.score == 0.75
    assert not SPEC[invariant](PolicyStore(policy))


class _SilentClamp(PolicyStore):
    """Clamps in-band operator to agent but records nothing."""

    def clamp_recorded(self, record_id: int) -> bool:
        return False


class _DefaultIngested(PolicyStore):
    """Honest clamp, but an unlabeled write is stored as ingested."""

    def write(
        self, payload: str, *, source_trust: str | None, channel: str, durable: bool
    ) -> int:
        label = c.INGESTED if source_trust is None else source_trust
        return super().write(
            payload, source_trust=label, channel=channel, durable=durable
        )


class _UncountedLoss(PolicyStore):
    """Health turns false on a loss but reports a zero counter."""

    def durable_violations(self) -> int:
        return 0

    def health_ok(self) -> bool:
        return all(r in self.rows and self.rows[r].durable for r in self.receipts)


@pytest.mark.parametrize(
    ("invariant", "store"),
    [
        ("VL-1", _SilentClamp(HONEST)),
        ("VL-1", _DefaultIngested(HONEST)),
        ("VL-4", _UncountedLoss(HONEST)),
    ],
    ids=["silent-clamp", "default-not-agent", "loss-without-counter"],
)
def test_other_pass_condition_breaks_fail_exactly_their_invariant(
    invariant: str, store: PolicyStore
) -> None:
    # Parts of the pass conditions the configurable-store family does not vary.
    failed = {s.invariant for s in score_adapter(store).invariants if not s.passed}
    assert failed == {invariant}
    assert not SPEC[invariant](store)


@given(policy=policies)
@example(policy=replace(HONEST, clamp="ingested"))
@example(policy=replace(HONEST, clamp="agent_plus_shadow"))
def test_suite_is_sound_for_vl1(policy: Policy) -> None:
    _sound(policy, "VL-1")


@given(policy=policies)
@example(policy=replace(HONEST, promote_in_band="make_actionable"))
@example(policy=replace(HONEST, promote_oob="all_records"))
@example(policy=replace(HONEST, promote_in_band="insert_operator"))
@example(policy=replace(HONEST, promote_oob="insert_operator"))
@example(policy=replace(HONEST, activate="insert_operator"))
@example(policy=replace(HONEST, activate="gate_spill"))
@example(policy=replace(HONEST, activate="activate_and_demote"))
def test_suite_is_sound_for_vl2(policy: Policy) -> None:
    _sound(policy, "VL-2")


@given(policy=policies)
@example(policy=replace(HONEST, envelope="agent_as_operator"))
def test_suite_is_sound_for_vl3(policy: Policy) -> None:
    _sound(policy, "VL-3")


@given(policy=policies)
@example(policy=replace(HONEST, health="always_ok"))
def test_suite_is_sound_for_vl4(policy: Policy) -> None:
    _sound(policy, "VL-4")


ALL_POLICIES: list[Policy] = [
    Policy(cl, ac, pib, poob, born, env, pr, he, ap)
    for cl in ("agent", "ingested", "none", "agent_plus_shadow")
    for ac in (
        "gate",
        "allow",
        "block",
        "insert_operator",
        "gate_spill",
        "activate_and_demote",
    )
    for pib in ("refuse", "mint", "make_actionable", "insert_operator")
    for poob in ("exact", "all_records", "refuse", "insert_operator")
    for born in (False, True)
    for env in ("honest", "agent_as_operator", "operator_label", "missing")
    for pr in ("correct", "drop_durable", "noop")
    for he in ("honest", "always_ok")
    for ap in (False, True)
]


def oracle_verdict(store: LedgerAdapter) -> dict[str, bool]:
    broken = _oracle_broken(store)
    return {inv: inv not in broken for inv in INVARIANTS}


def suite_verdict(store: LedgerAdapter) -> dict[str, bool]:
    return {s.invariant: s.passed for s in score_adapter(store).invariants}


def test_suite_is_sound_and_complete_over_every_configurable_store() -> None:
    # Exhaustive, not sampled: every store in the family, every invariant. The
    # suite and the oracle return the same verdict, which is both soundness
    # (the suite passes an invariant only if the oracle does) and completeness
    # (the reverse).
    assert len(ALL_POLICIES) == 36864
    disagree = [
        (
            policy,
            suite_verdict(PolicyStore(policy)),
            oracle_verdict(PolicyStore(policy)),
        )
        for policy in ALL_POLICIES
        if suite_verdict(PolicyStore(policy)) != oracle_verdict(PolicyStore(policy))
    ]
    assert not disagree, disagree[:3]


# --- adversarial pass over every state a probe reads (round 3 review) ---
#
# For each operation a probe exercises, one store changes one piece of state
# the operation must leave alone: the target's label, actionability, payload or
# durability, or another record. Each must fail exactly the invariant that
# operation belongs to, and the spec oracle must reject it.


class _ActivateRewritesPayload(PolicyStore):
    def activate(self, record_id: int, *, channel: str) -> bool:
        ok = super().activate(record_id, channel=channel)
        if ok:
            self.rows[record_id].payload = "rewritten"
        return ok


class _ActivateFlipsDurable(PolicyStore):
    def activate(self, record_id: int, *, channel: str) -> bool:
        ok = super().activate(record_id, channel=channel)
        if ok:
            self.rows[record_id].durable = not self.rows[record_id].durable
        return ok


class _ActivateTouchesBystander(PolicyStore):
    def activate(self, record_id: int, *, channel: str) -> bool:
        ok = super().activate(record_id, channel=channel)
        for rid, row in self.rows.items():
            if ok and rid != record_id:
                row.payload = "touched"
        return ok


class _RefusedActivateRewritesPayload(PolicyStore):
    def activate(self, record_id: int, *, channel: str) -> bool:
        ok = super().activate(record_id, channel=channel)
        if not ok:
            self.rows[record_id].payload = "rewritten"
        return ok


class _RefusedPromoteFlipsDurable(PolicyStore):
    def promote(self, record_id: int, *, channel: str) -> bool:
        ok = super().promote(record_id, channel=channel)
        if channel == c.IN_BAND:
            self.rows[record_id].durable = not self.rows[record_id].durable
        return ok


class _PromoteAlsoActivates(PolicyStore):
    def promote(self, record_id: int, *, channel: str) -> bool:
        ok = super().promote(record_id, channel=channel)
        if channel == c.OUT_OF_BAND:
            self.rows[record_id].actionable = True
        return ok


class _PromoteRewritesPayload(PolicyStore):
    def promote(self, record_id: int, *, channel: str) -> bool:
        ok = super().promote(record_id, channel=channel)
        if channel == c.OUT_OF_BAND:
            self.rows[record_id].payload = "rewritten"
        return ok


class _PromoteFlipsDurable(PolicyStore):
    def promote(self, record_id: int, *, channel: str) -> bool:
        ok = super().promote(record_id, channel=channel)
        if channel == c.OUT_OF_BAND:
            self.rows[record_id].durable = not self.rows[record_id].durable
        return ok


class _PruneDemotesDurable(PolicyStore):
    def prune(self) -> None:
        super().prune()
        for row in self.rows.values():
            row.durable = False


class _PruneRewritesDurable(PolicyStore):
    def prune(self) -> None:
        super().prune()
        for row in self.rows.values():
            if row.durable:
                row.payload = "rewritten"


@pytest.mark.parametrize(
    ("invariant", "store_type"),
    [
        ("VL-2", _ActivateRewritesPayload),
        ("VL-2", _ActivateFlipsDurable),
        ("VL-2", _ActivateTouchesBystander),
        ("VL-2", _RefusedActivateRewritesPayload),
        ("VL-2", _RefusedPromoteFlipsDurable),
        ("VL-2", _PromoteAlsoActivates),
        ("VL-2", _PromoteRewritesPayload),
        ("VL-2", _PromoteFlipsDurable),
        ("VL-4", _PruneDemotesDurable),
        ("VL-4", _PruneRewritesDurable),
    ],
    ids=lambda v: v if isinstance(v, str) else v.__name__.strip("_"),
)
def test_every_probed_state_change_fails_exactly_its_invariant(
    invariant: str, store_type: type[PolicyStore]
) -> None:
    store = store_type(HONEST)
    failed = {s.invariant for s in score_adapter(store).invariants if not s.passed}
    assert failed == {invariant}
    assert not SPEC[invariant](store_type(HONEST))


# --- generated mutations: every operation, every field, every record ---
#
# Instead of hand-picking broken stores, generate one per (operation, record,
# field): an otherwise honest store whose operation also changes that one
# field of that one record in a way the allowed-delta contract forbids, or
# creates or drops a record. Each must fail exactly the invariant that owns
# that violation (taken from the oracle's own table, not from probes.py), and
# the oracle must agree. This covers the two round-3 Codex cases by
# construction: prune changing a surviving record's label, and an envelope
# left stale after promotion.

MUTATED_OPS = (
    "write",
    "seed_operator",
    "activate_operator",
    "activate_refused",
    "promote_in_band",
    "promote_out_of_band",
    "prune",
)
MUTATED_FIELDS = ("trust", "actionable", "payload", "durable", "envelope")
_ORACLE_OP = {
    "write": "write",
    "seed_operator": "seed",
    "activate_operator": "activate",
    "activate_refused": "refuse",
    "promote_in_band": "promote_in_band",
    "promote_out_of_band": "promote",
    "prune": "prune",
}
_OTHER_TRUST = {c.OPERATOR: c.AGENT, c.AGENT: c.OPERATOR, c.INGESTED: c.OPERATOR}
WRITE_KINDS = ("agent", "ingested", "unlabeled", "clamped")


def _write_kind(source_trust: str | None, channel: str) -> str:
    if source_trust is None:
        return "unlabeled"
    if source_trust == c.OPERATOR and channel == c.IN_BAND:
        return "clamped"
    return source_trust


@dataclass(frozen=True)
class Mutation:
    op: str
    scope: str  # "target" | "other" | "store"
    field: str  # one of MUTATED_FIELDS, or "created" | "removed"

    @property
    def owner(self) -> str:
        if self.field == "envelope":
            return "VL-3"
        key = {"created": "extra", "removed": "lost", "refused": "actionable"}.get(
            self.field, self.field
        )
        if self.scope == "cap":
            key = "lost"
        return _ORACLE_OWNER[_ORACLE_OP[self.op]][key]

    def __str__(self) -> str:
        return f"{self.op}:{self.scope}:{self.field}"


MUTATIONS: list[Mutation] = [
    *(
        Mutation(op, scope, f)
        for op in MUTATED_OPS
        for scope in ("target", "other")
        for f in MUTATED_FIELDS
    ),
    *(Mutation(op, "store", "created") for op in MUTATED_OPS),
    *(Mutation(op, "other", "removed") for op in MUTATED_OPS),
    *(
        Mutation(op, "target", "removed")
        for op in MUTATED_OPS
        if op
        not in (
            "write",
            "seed_operator",
        )  # a write that stores nothing is not a one-field change
    ),
    # Round 4 review: faults that only one kind of request triggers.
    # A write of one kind (agent, ingested, unlabeled, or an in-band operator
    # request that gets clamped) comes out actionable.
    *(Mutation("write", kind, "actionable") for kind in WRITE_KINDS),
    # An operator record promoted out-of-band refuses the fast path.
    Mutation("activate_operator", "promoted", "refused"),
    # Keeping to the cap on write or seed, wrongly: dropping a durable record,
    # or dropping more non-durable records than the cap requires.
    *(
        Mutation(op, "cap", how)
        for op in ("write", "seed_operator")
        for how in ("durable", "over")
    ),
]


class MutantStore:
    """An honest PolicyStore whose one operation also applies one forbidden change."""

    def __init__(self, mutation: Mutation, policy: Policy = HONEST) -> None:
        self.m = mutation
        self.inner = PolicyStore(policy)
        self.stale: dict[int, str] = {}  # envelope labels left wrong on purpose
        self.promoted: set[int] = set()  # records promoted out-of-band

    def _wrong_cap(self, op: str, new: int) -> None:
        if self.m.op != op or self.m.scope != "cap":
            return
        rows = self.inner.rows
        if self.m.field == "durable":
            durable = sorted(i for i, r in rows.items() if r.durable and i != new)
            if durable:
                del rows[durable[0]]
        else:
            plain = sorted(i for i, r in rows.items() if not r.durable)
            if len(plain) >= CAP:
                for i in plain[: len(plain) - (CAP - 1)]:
                    del rows[i]

    def _mutate(self, op: str, target: int, other: int | None = None) -> None:
        if op != self.m.op or self.m.scope not in ("target", "other", "store"):
            return
        rows = self.inner.rows
        if other is None:
            others = sorted(i for i in rows if i != target)
            other = others[0] if others else None
        if self.m.field == "created":
            self.inner._insert(c.AGENT, False, "side")  # pyright: ignore[reportPrivateUsage]
            return
        rid = target if self.m.scope == "target" else other
        if rid is None or rid not in rows:
            return
        row = rows[rid]
        f = self.m.field
        if f == "removed":
            del rows[rid]
        elif f == "trust":
            row.trust = _OTHER_TRUST[row.trust]
        elif f == "actionable":
            row.actionable = not row.actionable
        elif f == "payload":
            row.payload += "~"
        elif f == "durable":
            row.durable = not row.durable
        else:
            self.stale[rid] = c.OPERATOR if row.trust != c.OPERATOR else c.AGENT

    # --- the adapter surface ---

    def fresh(self) -> None:
        self.inner.fresh()
        self.stale = {}
        self.promoted = set()

    def write(
        self, payload: str, *, source_trust: str | None, channel: str, durable: bool
    ) -> int:
        rid = self.inner.write(
            payload, source_trust=source_trust, channel=channel, durable=durable
        )
        if self.m.op == "write" and self.m.scope in WRITE_KINDS:
            if self.m.scope == _write_kind(source_trust, channel):
                self.inner.rows[rid].actionable = True
        elif self.m.scope != "cap":
            self._mutate("write", rid)
        self._wrong_cap("write", rid)
        return rid

    def seed_operator(self, payload: str) -> int:
        rid = self.inner.seed_operator(payload)
        if self.m.scope != "cap":
            self._mutate("seed_operator", rid)
        self._wrong_cap("seed_operator", rid)
        return rid

    def activate(self, record_id: int, *, channel: str) -> bool:
        if self.m.scope == "promoted" and record_id in self.promoted:
            return False  # refuses the fast path for promoted records
        op = (
            "activate_operator"
            if self.inner.rows[record_id].trust == c.OPERATOR
            else "activate_refused"
        )
        result = self.inner.activate(record_id, channel=channel)
        self._mutate(op, record_id)
        return result

    def promote(self, record_id: int, *, channel: str) -> bool:
        result = self.inner.promote(record_id, channel=channel)
        if channel == c.OUT_OF_BAND:
            self.promoted.add(record_id)
        self._mutate(
            "promote_out_of_band" if channel == c.OUT_OF_BAND else "promote_in_band",
            record_id,
        )
        return result

    def prune(self) -> None:
        self.inner.prune()
        survivors = sorted(self.inner.rows)
        if survivors:
            self._mutate(
                "prune", survivors[0], survivors[-1] if len(survivors) > 1 else None
            )

    def read(self, record_id: int) -> StoredRecord | None:
        r = self.inner.read(record_id)
        if r is None or r.envelope is None or record_id not in self.stale:
            return r
        return replace(
            r, envelope={**r.envelope, "source_trust": self.stale[record_id]}
        )

    def clamp_recorded(self, record_id: int) -> bool:
        return self.inner.clamp_recorded(record_id)

    def prune_cap(self) -> int:
        return self.inner.prune_cap()

    def count_records(self) -> int:
        return self.inner.count_records()

    def record_ids(self) -> list[int]:
        return self.inner.record_ids()

    def health_ok(self) -> bool:
        return self.inner.health_ok()

    def durable_violations(self) -> int:
        return self.inner.durable_violations()

    def force_durable_loss(self, record_id: int) -> None:
        self.inner.force_durable_loss(record_id)


def test_mutation_family_size() -> None:
    # 7 operations x 2 records x 5 fields, plus a side record per operation,
    # plus a dropped non-target per operation and a dropped target for the 5
    # operations that have an existing target.
    # Round 4 adds 4 write kinds coming out actionable, the promoted record that
    # refuses activation, and 4 wrong ways to keep to the cap.
    assert len(MUTATIONS) == 7 * 2 * 5 + 7 + 7 + 5 + 4 + 1 + 4 == 98
    assert len({str(m) for m in MUTATIONS}) == len(MUTATIONS)


@pytest.mark.parametrize("mutation", MUTATIONS, ids=str)
def test_generated_mutation_fails_exactly_its_invariant(mutation: Mutation) -> None:
    report = score_adapter(MutantStore(mutation))
    failed = {s.invariant for s in report.invariants if not s.passed}
    assert failed == {mutation.owner}
    truth = {inv: check(MutantStore(mutation)) for inv, check in SPEC.items()}
    assert {inv for inv, ok in truth.items() if not ok} == {mutation.owner}


def test_round_3_codex_cases_are_in_the_generated_family() -> None:
    # Prune upgrading a surviving durable record's label to operator, and an
    # envelope left stale (still "agent") after an out-of-band promotion.
    names = {str(m) for m in MUTATIONS}
    assert "prune:target:trust" in names
    assert "promote_out_of_band:target:envelope" in names
    for case, owner in (
        ("prune:target:trust", "VL-4"),
        ("promote_out_of_band:target:envelope", "VL-3"),
    ):
        m = next(m for m in MUTATIONS if str(m) == case)
        assert m.owner == owner
        assert not score_adapter(MutantStore(m)).perfect


# --- round 4 review: the three Codex cases, pinned by name ---


class _ClampedWritesActionable(PolicyStore):
    """Honest, except that a clamped in-band operator write comes out actionable."""

    def write(
        self, payload: str, *, source_trust: str | None, channel: str, durable: bool
    ) -> int:
        rid = super().write(
            payload, source_trust=source_trust, channel=channel, durable=durable
        )
        if source_trust == c.OPERATOR and channel == c.IN_BAND:
            self.rows[rid].actionable = True
        return rid


@dataclass
class _PromotedNotActivatable(PolicyStore):
    """Honest, except that a record promoted out-of-band refuses activation."""

    promoted: set[int] = field(default_factory=lambda: set())

    def promote(self, record_id: int, *, channel: str) -> bool:
        if channel == c.OUT_OF_BAND:
            self.promoted.add(record_id)
        return super().promote(record_id, channel=channel)

    def activate(self, record_id: int, *, channel: str) -> bool:
        if record_id in self.promoted:
            return False
        return super().activate(record_id, channel=channel)


def test_clamped_writes_coming_out_actionable_fail_vl2() -> None:
    store = _ClampedWritesActionable(HONEST)
    assert {s.invariant for s in score_adapter(store).invariants if not s.passed} == {
        "VL-2"
    }
    assert oracle_verdict(_ClampedWritesActionable(HONEST))["VL-2"] is False


def test_promoted_records_refusing_the_fast_path_fail_vl2() -> None:
    store = _PromotedNotActivatable(HONEST)
    assert {s.invariant for s in score_adapter(store).invariants if not s.passed} == {
        "VL-2"
    }
    assert oracle_verdict(_PromotedNotActivatable(HONEST))["VL-2"] is False


def test_an_honest_auto_pruning_store_scores_perfect() -> None:
    # The adapter contract allows pruning on write; such a store is honest.
    policy = replace(HONEST, auto_prune=True)
    assert score_adapter(PolicyStore(policy)).perfect
    assert all(oracle_verdict(PolicyStore(policy)).values())


def _every_pinned_store() -> list[tuple[str, Callable[[], LedgerAdapter]]]:
    stores: list[tuple[str, Callable[[], LedgerAdapter]]] = [
        (
            f"mutation {m} auto_prune={auto}",
            lambda m=m, auto=auto: MutantStore(m, replace(HONEST, auto_prune=auto)),
        )
        for m in MUTATIONS
        for auto in (False, True)
    ]
    stores += [
        ("clamped-actionable", lambda: _ClampedWritesActionable(HONEST)),
        ("promoted-not-activatable", lambda: _PromotedNotActivatable(HONEST)),
        ("reference", ReferenceAdapter),
    ]
    for cls in (
        _ActivateRewritesPayload,
        _ActivateFlipsDurable,
        _ActivateTouchesBystander,
        _RefusedActivateRewritesPayload,
        _RefusedPromoteFlipsDurable,
        _PromoteAlsoActivates,
        _PromoteRewritesPayload,
        _PromoteFlipsDurable,
        _PruneDemotesDurable,
        _PruneRewritesDurable,
        _SilentClamp,
        _DefaultIngested,
        _UncountedLoss,
    ):
        for auto in (False, True):
            stores.append(
                (
                    f"{cls.__name__} auto_prune={auto}",
                    lambda cls=cls, auto=auto: cls(replace(HONEST, auto_prune=auto)),
                )
            )
    return stores


@pytest.mark.parametrize(
    ("name", "make"),
    _every_pinned_store(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_suite_and_oracle_agree_on_every_pinned_store(
    name: str, make: Callable[[], LedgerAdapter]
) -> None:
    # The adversarial pass: no store the oracle rejects passes the suite, and
    # no store the suite rejects passes the oracle, on every mutation and every
    # hand-written store, with and without pruning on write.
    assert suite_verdict(make()) == oracle_verdict(make()), name
