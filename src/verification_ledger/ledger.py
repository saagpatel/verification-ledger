"""The reference implementation: a SQLite-backed governed ledger.

Implements VL-1 (provenance typing with the in-band operator clamp) and the VL-3
read envelope. The VL-2 promotion gate and VL-4 retention build on this store in
later phases. Deliberately small — stdlib ``sqlite3``, one table, zero external
dependencies — a store readable in one sitting.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from verification_ledger.envelope import Envelope, boundary
from verification_ledger.model import DEFAULT_TRUST, Channel, Record, Trust

logger = logging.getLogger("verification_ledger.ledger")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    payload      TEXT NOT NULL,
    source_trust TEXT NOT NULL CHECK (source_trust IN ('operator', 'agent', 'ingested')),
    durable      INTEGER NOT NULL DEFAULT 0 CHECK (durable IN (0, 1)),
    actionable   INTEGER NOT NULL DEFAULT 0 CHECK (actionable IN (0, 1)),
    created_at   TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a write. ``clamped`` records a refused in-band operator mint."""

    record_id: int
    source_trust: Trust
    clamped: bool


@dataclass(frozen=True)
class EnvelopedRecord:
    """A read result: the record plus its VL-3 instruction-boundary envelope."""

    record: Record
    envelope: Envelope


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Ledger:
    """A governed coordination store. Open with a path, or ``:memory:`` (default)."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def write(
        self,
        payload: str,
        *,
        source_trust: Trust = DEFAULT_TRUST,
        channel: Channel = Channel.IN_BAND,
        durable: bool = False,
    ) -> WriteResult:
        """VL-1: store a record; clamp an in-band request for operator trust.

        The clamp is the mechanism: nothing a model can reach may mint operator
        trust. Only the out-of-band channel — the operator's terminal — may store
        ``operator``. A clamp is never silent: it is logged and returned on the
        WriteResult.
        """
        clamped = False
        if source_trust is Trust.OPERATOR and channel is Channel.IN_BAND:
            source_trust = Trust.AGENT
            clamped = True
            logger.warning(
                "clamped in-band operator write to agent (payload len=%d)", len(payload)
            )
        cursor = self._conn.execute(
            "INSERT INTO records (payload, source_trust, durable, actionable, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (payload, str(source_trust), 1 if durable else 0, _utc_now()),
        )
        self._conn.commit()
        rowid = cursor.lastrowid
        if rowid is None:  # pragma: no cover — an INSERT always yields a rowid
            raise RuntimeError("insert did not produce a row id")
        return WriteResult(
            record_id=int(rowid), source_trust=source_trust, clamped=clamped
        )

    def read(self, record_id: int) -> EnvelopedRecord | None:
        """VL-3: return a record wrapped in the instruction-boundary envelope."""
        row = self._conn.execute(
            "SELECT id, payload, source_trust, durable, actionable, created_at "
            "FROM records WHERE id = ?",
            (record_id,),
        ).fetchone()
        return None if row is None else self._envelop(row)

    def read_all(self) -> list[EnvelopedRecord]:
        """VL-3: every stored record, each wrapped in its envelope, oldest first."""
        rows = self._conn.execute(
            "SELECT id, payload, source_trust, durable, actionable, created_at "
            "FROM records ORDER BY id"
        ).fetchall()
        return [self._envelop(row) for row in rows]

    def _envelop(self, row: sqlite3.Row) -> EnvelopedRecord:
        trust = Trust(row["source_trust"])
        record = Record(
            id=int(row["id"]),
            payload=str(row["payload"]),
            source_trust=trust,
            durable=bool(row["durable"]),
            actionable=bool(row["actionable"]),
            created_at=str(row["created_at"]),
        )
        return EnvelopedRecord(record=record, envelope=boundary(trust))
