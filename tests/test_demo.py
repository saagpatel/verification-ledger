"""The synthetic seed and the scripted replay."""

from __future__ import annotations

from verification_ledger.demo.replay import main, run
from verification_ledger.demo.seed import seed
from verification_ledger.ledger import Ledger
from verification_ledger.model import Trust


def test_seed_writes_only_synthetic_non_operator_records() -> None:
    with Ledger() as led:
        count = seed(led)
        records = led.read_all()
        assert count == len(records)
        # Nothing seeded is operator-trust; it is all untrusted fleet output.
        assert all(e.record.source_trust is not Trust.OPERATOR for e in records)
        assert any(e.record.durable for e in records)  # includes a durable record


def test_replay_tells_the_full_story() -> None:
    with Ledger() as led:
        outcome = run(led, verbose=False)
    assert outcome.laundering_blocked is True
    assert outcome.actionable_after_promotion is True
    assert outcome.durable_survived_prune is True
    assert outcome.health_ok is True
    assert outcome.ok is True


def test_replay_main_exits_zero() -> None:
    assert main() == 0
