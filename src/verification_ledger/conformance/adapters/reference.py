"""Adapter binding the bundled reference implementation to the LedgerAdapter protocol.

Required to score a perfect 1.0 — the reference store is the existence proof that
the contract is satisfiable. ``read`` reports the store's actual persisted state.
"""

from __future__ import annotations

from verification_ledger.conformance.contract import StoredRecord
from verification_ledger.invariants import FailurePolicy
from verification_ledger.ledger import Ledger
from verification_ledger.model import Channel, Trust


class ReferenceAdapter:
    """Grades ``verification_ledger.ledger.Ledger`` against the conformance suite."""

    def __init__(self) -> None:
        # REPORT policy: a VL-4 violation degrades health_ok() rather than raising,
        # so the suite reads a boolean instead of catching an exception.
        self._led = Ledger(":memory:", policy=FailurePolicy.REPORT)

    def fresh(self) -> None:
        self._led.close()
        self._led = Ledger(":memory:", policy=FailurePolicy.REPORT)

    def write(
        self, payload: str, *, source_trust: str, channel: str, durable: bool
    ) -> int:
        return self._led.write(
            payload,
            source_trust=Trust(source_trust),
            channel=Channel(channel),
            durable=durable,
        ).record_id

    def read(self, record_id: int) -> StoredRecord | None:
        enveloped = self._led.read(record_id)
        if enveloped is None:
            return None
        env = enveloped.envelope
        return StoredRecord(
            record_id=enveloped.record.id,
            source_trust=str(enveloped.record.source_trust),
            actionable=enveloped.record.actionable,
            envelope={
                "kind": env["kind"],
                "source_trust": env["source_trust"],
                "warning": env["warning"],
            },
        )

    def activate(self, record_id: int, *, channel: str) -> bool:
        self._led.activate(record_id, channel=Channel(channel))
        record = self.read(record_id)
        return record is not None and record.actionable

    def promote(self, record_id: int, *, channel: str) -> bool:
        self._led.promote(record_id, channel=Channel(channel))
        record = self.read(record_id)
        return record is not None and record.source_trust == str(Trust.OPERATOR)

    def prune(self, *, keep: int) -> None:
        self._led.prune(keep=keep)

    def count_records(self) -> int:
        return len(self._led.read_all())

    def health_ok(self) -> bool:
        return self._led.health().ok
