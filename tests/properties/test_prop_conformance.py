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
from verification_ledger.conformance.probes import BOUNDARY_KIND, Probe, all_probes
from verification_ledger.conformance.score import INVARIANTS, score_adapter

CAP = 3
TRUSTS = (c.OPERATOR, c.AGENT, c.INGESTED)
CHANNELS = (c.IN_BAND, c.OUT_OF_BAND)

Clamp = Literal["agent", "ingested", "none", "agent_plus_shadow"]
Activate = Literal["gate", "allow", "block", "insert_operator", "gate_spill"]
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


HONEST = Policy()


@dataclass
class _Row:
    trust: str
    actionable: bool
    durable: bool


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
        rid = self._insert(trust, durable)
        if relabeled:
            self.clamped.add(rid)
            if self.policy.clamp == "agent_plus_shadow":
                self._insert(c.OPERATOR, False)  # an operator copy on the side
        return rid

    def _insert(self, trust: str, durable: bool) -> int:
        rid = self.next_id
        self.next_id += 1
        self.rows[rid] = _Row(trust, self.policy.born_actionable, durable)
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
        return StoredRecord(record_id, row.trust, row.actionable, env)

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

    def snapshot(self) -> dict[int, tuple[str, bool, bool]]:
        return {i: (r.trust, r.actionable, r.durable) for i, r in self.rows.items()}

    def force_lose(self, record_id: int) -> None:
        del self.rows[record_id]


policies = st.builds(
    Policy,
    clamp=st.sampled_from(["agent", "ingested", "none", "agent_plus_shadow"]),
    activate=st.sampled_from(
        ["gate", "allow", "block", "insert_operator", "gate_spill"]
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
)


# --- the oracle: each invariant's SPEC.md pass condition, checked directly ---


def _snap(a: PolicyStore) -> dict[int, tuple[str, bool, bool]]:
    return a.snapshot()


def spec_vl1(a: PolicyStore) -> bool:
    """SPEC VL-1: no label means agent; in-band operator reads back as agent with
    a recorded clamp; agent and ingested are stored unchanged and unclamped."""
    for trust in (None, *TRUSTS):
        for channel in CHANNELS:
            a.fresh()
            rid = a.write("x", source_trust=trust, channel=channel, durable=False)
            if set(a.snapshot()) != {rid}:
                return False  # a write must create exactly its own record
            r = a.read(rid)
            clamp = trust == c.OPERATOR and channel == c.IN_BAND
            expected = c.AGENT if (trust is None or clamp) else trust
            if r is None or r.source_trust != expected:
                return False
            if a.clamp_recorded(rid) is not clamp:
                return False
    return True


def spec_vl2(a: PolicyStore) -> bool:
    """SPEC VL-2: refusals mutate nothing; ceremony promotes one exact record; fast path open."""
    for trust in (c.AGENT, c.INGESTED):
        for attempt in ("activate", "promote"):
            a.fresh()
            for other in TRUSTS:  # bystanders of every kind
                a.write("b", source_trust=other, channel=c.OUT_OF_BAND, durable=False)
            rid = a.write("w", source_trust=trust, channel=c.IN_BAND, durable=False)
            born = a.read(rid)
            if born is None or born.actionable:
                return False
            before = _snap(a)
            if attempt == "activate":
                if a.activate(rid, channel=c.IN_BAND) is not False:
                    return False
            else:
                a.promote(rid, channel=c.IN_BAND)
            if _snap(a) != before:
                return False
    a.fresh()
    ids = [
        a.write(f"w{i}", source_trust=c.AGENT, channel=c.IN_BAND, durable=False)
        for i in range(3)
    ]
    before = _snap(a)
    a.promote(ids[1], channel=c.OUT_OF_BAND)
    after = _snap(a)
    if set(after) != set(before) or after[ids[1]][0] != c.OPERATOR:
        return False
    if any(after[i] != before[i] for i in before if i != ids[1]):
        return False
    a.fresh()
    for other in TRUSTS:
        a.write("b", source_trust=other, channel=c.OUT_OF_BAND, durable=False)
    rid = a.seed_operator("directive")
    before = _snap(a)
    if a.activate(rid, channel=c.IN_BAND) is not True:
        return False
    after = _snap(a)
    if set(after) != set(before) or not after[rid][1]:
        return False
    return all(after[i] == before[i] for i in before if i != rid)


def spec_vl3(a: PolicyStore) -> bool:
    """SPEC VL-3: every read carries the envelope with the record's actual trust."""
    a.fresh()
    ids = [
        a.write("x", source_trust=t, channel=c.IN_BAND, durable=False)
        for t in (c.AGENT, c.INGESTED)
    ]
    ids.append(a.seed_operator("op"))
    for rid in ids:
        r = a.read(rid)
        if r is None or r.envelope is None:
            return False
        if (
            r.envelope.get("kind") != BOUNDARY_KIND
            or r.envelope.get("source_trust") != r.source_trust
        ):
            return False
    return True


def spec_vl4(a: PolicyStore) -> bool:
    """SPEC VL-4: prune keeps every durable and the newest ``cap`` non-durable; loss is reported."""
    a.fresh()
    durable = [
        a.write(f"d{i}", source_trust=c.AGENT, channel=c.IN_BAND, durable=True)
        for i in range(2)
    ]
    plain = [
        a.write(f"n{i}", source_trust=c.AGENT, channel=c.IN_BAND, durable=False)
        for i in range(CAP + 3)
    ]
    a.prune()
    survivors = set(a.snapshot())
    if survivors != set(durable) | set(plain[-CAP:]) or not a.health_ok():
        return False
    a.force_lose(durable[0])
    return a.health_ok() is False and a.durable_violations() > 0


SPEC: dict[str, Callable[[PolicyStore], bool]] = {
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
    Policy(cl, ac, pib, poob, born, env, pr, he)
    for cl in ("agent", "ingested", "none", "agent_plus_shadow")
    for ac in ("gate", "allow", "block", "insert_operator", "gate_spill")
    for pib in ("refuse", "mint", "make_actionable", "insert_operator")
    for poob in ("exact", "all_records", "refuse", "insert_operator")
    for born in (False, True)
    for env in ("honest", "agent_as_operator", "operator_label", "missing")
    for pr in ("correct", "drop_durable", "noop")
    for he in ("honest", "always_ok")
]


def test_suite_is_sound_and_complete_over_every_configurable_store() -> None:
    # Exhaustive, not sampled: every store in the family, every invariant.
    # Sound: the suite passes an invariant only if the spec oracle does.
    # Complete: the oracle accepting an invariant means the suite passes it.
    assert len(ALL_POLICIES) == 15360
    unsound: list[tuple[str, Policy]] = []
    incomplete: list[tuple[str, Policy]] = []
    for policy in ALL_POLICIES:
        passes = suite_passes(policy)
        for inv, check in SPEC.items():
            truth = check(PolicyStore(policy))
            if passes[inv] and not truth:
                unsound.append((inv, policy))
            if truth and not passes[inv]:
                incomplete.append((inv, policy))
    assert not unsound, unsound[:3]
    assert not incomplete, incomplete[:3]
