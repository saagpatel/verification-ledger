"""The conformance suite: the reference impl scores 1.0, and two degenerate stores
fail the specific invariants they violate — proving the score is bidirectional and
granular, not a checklist a block-everything or allow-everything store can game.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from verification_ledger.conformance import contract as c
from verification_ledger.conformance.adapters.reference import ReferenceAdapter
from verification_ledger.conformance.contract import StoredRecord
from verification_ledger.conformance.run import main
from verification_ledger.conformance.score import score_adapter
from verification_ledger.model import Channel, Trust


@dataclass
class _Row:
    trust: str
    actionable: bool
    durable: bool


def _envelope(trust: str) -> dict[str, str]:
    return {
        "kind": "stored_data_not_instructions",
        "source_trust": trust,
        "warning": "w",
    }


# The test doubles all use one small cap; the suite reads it via prune_cap().
_DOUBLE_CAP = 3


def _correct_prune(rows: dict[int, _Row], keep: int) -> None:
    non_durable = [i for i in sorted(rows, reverse=True) if not rows[i].durable]
    for i in non_durable[keep:]:
        del rows[i]


class _AllowEverythingAdapter:
    """No clamp; activation and promotion always succeed. Fails VL-1 and VL-2."""

    def __init__(self) -> None:
        self._rows: dict[int, _Row] = {}
        self._next = 1

    def fresh(self) -> None:
        self._rows = {}
        self._next = 1

    def write(
        self, payload: str, *, source_trust: str, channel: str, durable: bool
    ) -> int:
        rid = self._next
        self._next += 1
        self._rows[rid] = _Row(source_trust, False, durable)  # no clamp
        return rid

    def read(self, record_id: int) -> StoredRecord | None:
        row = self._rows.get(record_id)
        if row is None:
            return None
        return StoredRecord(record_id, row.trust, row.actionable, _envelope(row.trust))

    def activate(self, record_id: int, *, channel: str) -> bool:
        self._rows[record_id].actionable = True
        return True

    def promote(self, record_id: int, *, channel: str) -> bool:
        self._rows[record_id].trust = "operator"  # even in-band
        return True

    def seed_operator(self, payload: str) -> int:
        return self.write(
            payload, source_trust=c.OPERATOR, channel=c.OUT_OF_BAND, durable=False
        )

    def prune(self) -> None:
        _correct_prune(self._rows, _DOUBLE_CAP)

    def prune_cap(self) -> int:
        return _DOUBLE_CAP

    def count_records(self) -> int:
        return len(self._rows)

    def health_ok(self) -> bool:
        return True


class _BlockEverythingAdapter:
    """Correct clamp and storage, but nothing ever activates or promotes. Fails VL-2."""

    def __init__(self) -> None:
        self._rows: dict[int, _Row] = {}
        self._next = 1

    def fresh(self) -> None:
        self._rows = {}
        self._next = 1

    def write(
        self, payload: str, *, source_trust: str, channel: str, durable: bool
    ) -> int:
        trust = source_trust
        if source_trust == c.OPERATOR and channel == c.IN_BAND:
            trust = c.AGENT  # correct clamp
        rid = self._next
        self._next += 1
        self._rows[rid] = _Row(trust, False, durable)
        return rid

    def read(self, record_id: int) -> StoredRecord | None:
        row = self._rows.get(record_id)
        if row is None:
            return None
        return StoredRecord(record_id, row.trust, row.actionable, _envelope(row.trust))

    def activate(self, record_id: int, *, channel: str) -> bool:
        return False  # blocks the legitimate fast path too

    def promote(self, record_id: int, *, channel: str) -> bool:
        return False

    def seed_operator(self, payload: str) -> int:
        return self.write(
            payload, source_trust=c.OPERATOR, channel=c.OUT_OF_BAND, durable=False
        )

    def prune(self) -> None:
        _correct_prune(self._rows, _DOUBLE_CAP)

    def prune_cap(self) -> int:
        return _DOUBLE_CAP

    def count_records(self) -> int:
        return len(self._rows)

    def health_ok(self) -> bool:
        return True


class _BornActionableAdapter:
    """Honest VL-1/VL-3/VL-4, but every record is born actionable while activate()
    merely reports ``trust == operator``. This is the laundering store the red-team
    built: a downstream consumer reading ``StoredRecord.actionable`` acts on
    untrusted agent content while the gate politely reports "refused". It must fail
    VL-2 now that the probes assert persisted state.
    """

    def __init__(self) -> None:
        self._rows: dict[int, _Row] = {}
        self._next = 1

    def fresh(self) -> None:
        self._rows = {}
        self._next = 1

    def write(
        self, payload: str, *, source_trust: str, channel: str, durable: bool
    ) -> int:
        trust = source_trust
        if source_trust == c.OPERATOR and channel == c.IN_BAND:
            trust = c.AGENT  # honest clamp
        rid = self._next
        self._next += 1
        self._rows[rid] = _Row(trust, True, durable)  # BORN ACTIONABLE
        return rid

    def read(self, record_id: int) -> StoredRecord | None:
        row = self._rows.get(record_id)
        if row is None:
            return None
        return StoredRecord(record_id, row.trust, row.actionable, _envelope(row.trust))

    def activate(self, record_id: int, *, channel: str) -> bool:
        return self._rows[record_id].trust == c.OPERATOR  # reports refused for agent

    def promote(self, record_id: int, *, channel: str) -> bool:
        if channel == c.OUT_OF_BAND:
            self._rows[record_id].trust = c.OPERATOR
            return True
        return False

    def seed_operator(self, payload: str) -> int:
        return self.write(
            payload, source_trust=c.OPERATOR, channel=c.OUT_OF_BAND, durable=False
        )

    def prune(self) -> None:
        _correct_prune(self._rows, _DOUBLE_CAP)

    def prune_cap(self) -> int:
        return _DOUBLE_CAP

    def count_records(self) -> int:
        return len(self._rows)

    def health_ok(self) -> bool:
        return True


def test_reference_scores_perfect() -> None:
    report = score_adapter(ReferenceAdapter())
    assert report.perfect is True
    assert report.score == 1.0
    assert report.passed_count == 4
    for s in report.invariants:
        assert s.passed, f"{s.invariant} failed on the reference implementation"


def test_allow_everything_fails_vl1_and_vl2() -> None:
    report = score_adapter(_AllowEverythingAdapter())
    failed = {s.invariant for s in report.invariants if not s.passed}
    assert failed == {"VL-1", "VL-2"}  # laundering not blocked
    assert report.score == 0.5


def test_block_everything_fails_only_vl2() -> None:
    report = score_adapter(_BlockEverythingAdapter())
    failed = {s.invariant for s in report.invariants if not s.passed}
    assert failed == {"VL-2"}  # legitimate fast path broken
    assert report.score == 0.75


def test_born_actionable_laundering_store_is_caught() -> None:
    # Regression for the red-team break: this store scored a perfect 1.00 before
    # the VL-2 probes asserted persisted read().actionable. It must now fail VL-2.
    report = score_adapter(_BornActionableAdapter())
    failed = {s.invariant for s in report.invariants if not s.passed}
    assert failed == {"VL-2"}
    assert report.score == 0.75


def test_contract_strings_match_reference_enums() -> None:
    assert c.OPERATOR == Trust.OPERATOR
    assert c.AGENT == Trust.AGENT
    assert c.INGESTED == Trust.INGESTED
    assert c.IN_BAND == Channel.IN_BAND
    assert c.OUT_OF_BAND == Channel.OUT_OF_BAND


def test_cli_reference_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["reference"]) == 0
    assert "Score 1.00" in capsys.readouterr().out


def test_cli_json_report(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["reference", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["score"] == 1.0
    assert data["passed"] == 4
