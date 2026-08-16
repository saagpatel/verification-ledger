"""VL-4 retention: pure selection plus the ledger's prune/health behavior."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from verification_ledger.invariants import FailurePolicy, InvariantViolation
from verification_ledger.ledger import Ledger
from verification_ledger.retention import RetentionCandidate, select_prunable

# --- pure selection ---


def _cands(*specs: tuple[int, bool]) -> list[RetentionCandidate]:
    # newest first
    return [RetentionCandidate(rid, durable) for rid, durable in specs]


def test_durable_never_selected_even_when_old() -> None:
    # newest first: two fresh non-durable, one old durable, one old non-durable
    cands = _cands((4, False), (3, False), (2, True), (1, False))
    selected = select_prunable(cands, keep=1)
    assert 2 not in selected  # the durable id is exempt
    assert selected == [3, 1]  # non-durable past the newest 1, durable skipped


def test_durable_does_not_count_against_the_cap() -> None:
    # three durable + two non-durable; keep=1 non-durable
    cands = _cands((5, True), (4, True), (3, True), (2, False), (1, False))
    assert select_prunable(cands, keep=1) == [1]  # keeps newest non-durable (2)


def test_keep_zero_prunes_all_non_durable() -> None:
    cands = _cands((3, True), (2, False), (1, False))
    assert select_prunable(cands, keep=0) == [2, 1]


def test_keep_beyond_count_prunes_none() -> None:
    cands = _cands((2, False), (1, False))
    assert select_prunable(cands, keep=5) == []


def test_negative_keep_raises() -> None:
    with pytest.raises(ValueError):
        select_prunable([], keep=-1)


# --- ledger integration ---


def test_prune_keeps_newest_and_all_durable() -> None:
    with Ledger() as led:
        durable_id = led.write("shipped", durable=True).record_id
        ids = [led.write(f"n{i}").record_id for i in range(5)]
        result = led.prune(keep=2)
        assert result.pruned_count == 3
        remaining = {e.record.id for e in led.read_all()}
        assert durable_id in remaining  # durable survives regardless of the cap
        assert ids[-2:] == sorted(i for i in ids if i in remaining)  # newest 2 kept


def test_prune_never_touches_a_durable_record() -> None:
    with Ledger() as led:
        d1 = led.write("a", durable=True).record_id
        d2 = led.write("b", durable=True).record_id
        for i in range(4):
            led.write(f"n{i}")
        led.prune(keep=0)  # prune every non-durable record
        remaining = {e.record.id for e in led.read_all()}
        assert remaining == {d1, d2}
        assert led.health().ok is True  # no orphan created by pruning


def test_health_on_clean_store() -> None:
    with Ledger() as led:
        led.write("d", durable=True)
        led.write("n")
        report = led.health()
        assert report.ok is True
        assert report.total == 2
        assert report.durable_protected == 1
        assert report.prunable == 1
        assert report.durable_orphans == 0
        assert report.violations == ()


def _delete_row_out_of_band(db: Path, record_id: int) -> None:
    # Simulate external tampering / a buggy foreign prune deleting a durable row.
    raw = sqlite3.connect(str(db))
    raw.execute("DELETE FROM records WHERE id = ?", (record_id,))
    raw.commit()
    raw.close()


def test_forced_durable_loss_is_reported_under_report_policy(tmp_path: Path) -> None:
    db = tmp_path / "led.db"
    with Ledger(db, policy=FailurePolicy.REPORT) as led:
        rid = led.write("shipped", durable=True).record_id
        _delete_row_out_of_band(db, rid)
        report = led.health()
        assert report.ok is False
        assert report.durable_orphans == 1
        assert report.violations  # non-empty: the loss is surfaced, not silent


def test_forced_durable_loss_raises_under_raise_policy(tmp_path: Path) -> None:
    db = tmp_path / "led.db"
    with Ledger(db, policy=FailurePolicy.RAISE) as led:
        rid = led.write("shipped", durable=True).record_id
        _delete_row_out_of_band(db, rid)
        with pytest.raises(InvariantViolation):
            led.health()
