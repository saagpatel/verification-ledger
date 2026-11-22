"""Multi-statement operations against a store shared by two connections.

Each test interleaves a second connection deterministically, at the exact
point between two reads (or a read and a write) of the operation under test,
by wrapping one internal step. Two properties are checked:

- ``health()`` reads every input in one snapshot, so a legitimate durable
  write committed mid-check is not a false alarm (REPORT stays ok; RAISE does
  not throw);
- ``prune()``, ``activate()``, ``promote()`` and the migration hold the write
  lock across their read and write, so no other writer can interleave.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from verification_ledger import ledger as ledger_mod
from verification_ledger.gate import (
    GateDecision,
    evaluate_activation,
    evaluate_promotion,
)
from verification_ledger.integrity import LiveRecord, Receipt, relink
from verification_ledger.invariants import FailurePolicy
from verification_ledger.ledger import Ledger, PromotionResult
from verification_ledger.model import Channel, Trust
from verification_ledger.retention import RetentionCandidate, select_prunable


def _try_raw_write(db: Path) -> bool:
    """True if another connection could write right now (no lock held)."""
    raw = sqlite3.connect(str(db), timeout=0)
    try:
        raw.execute("CREATE TABLE IF NOT EXISTS probe_lock (x INTEGER)")
        raw.commit()
        return True
    except sqlite3.OperationalError as exc:
        assert "locked" in str(exc)
        return False
    finally:
        raw.close()


@pytest.mark.parametrize("policy", list(FailurePolicy))
def test_health_reads_one_snapshot_under_a_concurrent_durable_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: FailurePolicy
) -> None:
    db = tmp_path / "fleet.db"
    checker = Ledger(db, policy=policy)
    writer = Ledger(db)
    checker.write("before", durable=True)
    original = Ledger._receipts  # pyright: ignore[reportPrivateUsage]
    fired: list[int] = []

    def receipts_then_concurrent_write(self: Ledger) -> list[Receipt]:
        out = original(self)
        if self is checker and not fired:
            # Commit a legitimate durable write between health's receipt read
            # and its record and metadata reads.
            fired.append(writer.write("concurrent", durable=True).record_id)
        return out

    monkeypatch.setattr(Ledger, "_receipts", receipts_then_concurrent_write)
    report = checker.health()  # RAISE would throw on a false alarm
    assert fired, "the interleaving did not happen"
    assert report.ok is True
    assert report.violation_count == 0
    assert report.total == 1  # the snapshot predates the concurrent write
    monkeypatch.undo()
    later = checker.health()
    assert later.ok is True and later.total == 2
    checker.close()
    writer.close()


def test_prune_holds_the_write_lock_between_select_and_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "fleet.db"
    led = Ledger(db, policy=FailurePolicy.RAISE)
    led.write("kept", durable=True)
    for i in range(4):
        led.write(f"n{i}")
    locked: list[bool] = []

    def selecting(candidates: Sequence[RetentionCandidate], *, keep: int) -> list[int]:
        locked.append(not _try_raw_write(db))
        return select_prunable(candidates, keep=keep)

    monkeypatch.setattr(ledger_mod, "select_prunable", selecting)
    result = led.prune(keep=1)  # RAISE: a false mismatch would throw here
    assert locked == [True]
    assert result.pruned_count == 3
    assert led.health().ok
    led.close()


@pytest.mark.parametrize("op", ["activate", "promote"])
def test_gate_operations_hold_the_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    db = tmp_path / "fleet.db"
    led = Ledger(db)
    rid = led.write(
        "op", source_trust=Trust.OPERATOR, channel=Channel.OUT_OF_BAND
    ).record_id
    locked: list[bool] = []
    if op == "activate":

        def deciding_a(trust: Trust) -> GateDecision:
            locked.append(not _try_raw_write(db))
            return evaluate_activation(trust)

        monkeypatch.setattr(ledger_mod, "evaluate_activation", deciding_a)
        assert led.activate(rid).allowed is True
    else:

        def deciding_p(channel: Channel) -> GateDecision:
            locked.append(not _try_raw_write(db))
            return evaluate_promotion(channel)

        monkeypatch.setattr(ledger_mod, "evaluate_promotion", deciding_p)
        assert led.promote(rid, channel=Channel.OUT_OF_BAND).promoted is True
    assert locked == [True]
    led.close()


def test_migration_holds_the_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(db))
    raw.executescript(
        """
        CREATE TABLE records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL,
            source_trust TEXT NOT NULL, durable INTEGER NOT NULL DEFAULT 0,
            actionable INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE durable_receipts (record_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL);
        INSERT INTO records (payload, source_trust, durable, created_at) VALUES ('a', 'agent', 1, 't');
        INSERT INTO durable_receipts (record_id, created_at) VALUES (1, 't');
        """
    )
    raw.close()
    locked: list[bool] = []
    real: Callable[[Sequence[Receipt], dict[int, LiveRecord]], list[Receipt]] = relink

    def relinking(
        receipts: Sequence[Receipt], records: dict[int, LiveRecord]
    ) -> list[Receipt]:
        locked.append(not _try_raw_write(db))
        return real(receipts, records)

    monkeypatch.setattr(ledger_mod, "relink", relinking)
    with Ledger(db, policy=FailurePolicy.REPORT) as led:
        assert led.health().ok
    assert locked == [True]


def test_promotion_cli_reports_a_record_pruned_by_another_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The CLI used to read the record, then promote it in a second call; a
    # prune by another process in between crashed it with a KeyError.
    from verification_ledger.promote import promote as promote_cli

    db = tmp_path / "fleet.db"
    with Ledger(db) as led:
        rid = led.write("gone soon").record_id
    original = Ledger.promote

    def prune_then_promote(
        self: Ledger, record_id: int, *, channel: Channel = Channel.IN_BAND
    ) -> PromotionResult:
        # Another process prunes the record just before the promotion runs.
        with Ledger(db) as other:
            other.prune(keep=0)
        return original(self, record_id, channel=channel)

    monkeypatch.setattr(Ledger, "promote", prune_then_promote)
    assert promote_cli(str(db), rid) == 1
    assert "no record" in capsys.readouterr().out
