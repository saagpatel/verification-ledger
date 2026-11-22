"""The reference implementation: a SQLite-backed governed ledger.

Implements VL-1 (provenance typing with the in-band operator clamp), the VL-3
read envelope, the VL-2 promotion gate (activation + out-of-band promotion), and
VL-4 retention (the durable exemption plus a health check that detects durable
loss). Deliberately small — stdlib ``sqlite3``, zero external dependencies — a
store readable in one sitting.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from verification_ledger.envelope import Envelope, boundary
from verification_ledger.gate import evaluate_activation, evaluate_promotion
from verification_ledger.integrity import (
    GENESIS,
    LiveRecord,
    Meta,
    Receipt,
    content_digest,
    link_hash,
    relink,
    verify,
)
from verification_ledger.invariants import FailurePolicy, InvariantMonitor
from verification_ledger.model import DEFAULT_TRUST, Channel, Record, Trust
from verification_ledger.retention import RetentionCandidate, select_prunable

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
-- A durable receipt is written for every durable record. VL-4's health check
-- cross-checks it against the records table: a receipt with no live durable
-- record means a durable record was lost, the detectable violation. Each
-- receipt also carries a digest of its record's content and a link over the
-- previous receipt, so an overwrite or a removed receipt is detectable too.
CREATE TABLE IF NOT EXISTS durable_receipts (
    record_id    INTEGER PRIMARY KEY,
    created_at   TEXT NOT NULL,
    digest       TEXT,
    prev_hash    TEXT,
    receipt_hash TEXT
);
-- The receipt chain's length and head, so removing the newest receipt (which
-- leaves every remaining link valid) is detectable as well.
CREATE TABLE IF NOT EXISTS ledger_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_RECEIPT_COLUMNS = ("digest", "prev_hash", "receipt_hash")


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


@dataclass(frozen=True)
class ActivationResult:
    """Outcome of an activation attempt (the VL-2 gated transition)."""

    record_id: int
    allowed: bool
    reason: str
    actionable: bool


@dataclass(frozen=True)
class PromotionResult:
    """Outcome of a promotion attempt (the out-of-band trust mint)."""

    record_id: int
    promoted: bool
    reason: str
    source_trust: Trust


@dataclass(frozen=True)
class PruneResult:
    """Outcome of a prune: which non-durable records were removed."""

    pruned_ids: tuple[int, ...]
    pruned_count: int


@dataclass(frozen=True)
class HealthReport:
    """VL-4 health: counts plus the durable-integrity violation surface.

    ``durable_orphans`` counts receipts whose durable record is gone or demoted
    (the missing-record counter). The other three counters come from the
    receipt chain: content that no longer matches its digest, durable records
    with no receipt, and chain links or metadata that do not verify.
    """

    ok: bool
    total: int
    durable_protected: int
    prunable: int
    durable_orphans: int
    violations: tuple[str, ...]
    digest_mismatches: int = 0
    missing_receipts: int = 0
    chain_breaks: int = 0

    @property
    def violation_count(self) -> int:
        """Every VL-4 violation the health check found, across all counters."""
        return (
            self.durable_orphans
            + self.digest_mismatches
            + self.missing_receipts
            + self.chain_breaks
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Ledger:
    """A governed coordination store. Open with a path, or ``:memory:`` (default)."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        policy: FailurePolicy = FailurePolicy.RAISE,
    ) -> None:
        self._policy = policy
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        existed = self._has_table("records")
        self._conn.executescript(_SCHEMA)
        self._migrate(existed)
        self._conn.commit()

    def _has_table(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def _migrate(self, existed: bool) -> None:
        """Bring a store written by an earlier version up to the receipt chain.

        Idempotent: adds the receipt columns if missing, fills digests and links
        only for receipts that have none (an existing link is never recomputed),
        and writes the chain metadata only when this store is new or is being
        upgraded. Metadata that has gone missing from an already-chained store is
        not recreated; health reports it.
        """
        cols = {
            str(r["name"])
            for r in self._conn.execute("PRAGMA table_info(durable_receipts)")
        }
        for col in _RECEIPT_COLUMNS:
            if col not in cols:
                self._conn.execute(
                    f"ALTER TABLE durable_receipts ADD COLUMN {col} TEXT"
                )
        receipts = self._receipts()
        legacy = [r for r in receipts if r.receipt_hash is None or r.digest is None]
        if legacy:
            for r in relink(receipts, self._live_records()):
                if r in receipts:
                    continue
                self._conn.execute(
                    "UPDATE durable_receipts SET digest = ?, prev_hash = ?, "
                    "receipt_hash = ? WHERE record_id = ?",
                    (r.digest, r.prev_hash, r.receipt_hash, r.record_id),
                )
            logger.info("migrated %d durable receipts to the hash chain", len(legacy))
        meta = self._meta()
        if not existed or legacy or (meta.count is None and not receipts):
            chained = self._receipts()
            head = chained[-1].receipt_hash if chained else GENESIS
            self._set_meta(len(chained), head or GENESIS)

    def _receipts(self) -> list[Receipt]:
        rows = self._conn.execute(
            "SELECT record_id, digest, prev_hash, receipt_hash FROM durable_receipts "
            "ORDER BY record_id"
        ).fetchall()
        return [
            Receipt(int(r["record_id"]), r["digest"], r["prev_hash"], r["receipt_hash"])
            for r in rows
        ]

    def _live_records(self) -> dict[int, LiveRecord]:
        rows = self._conn.execute(
            "SELECT id, payload, created_at, durable FROM records"
        ).fetchall()
        return {
            int(r["id"]): LiveRecord(
                str(r["payload"]), str(r["created_at"]), bool(r["durable"])
            )
            for r in rows
        }

    def _meta(self) -> Meta:
        values = {
            str(r["key"]): str(r["value"])
            for r in self._conn.execute("SELECT key, value FROM ledger_meta")
        }
        count = values.get("durable_count")
        return Meta(
            count=int(count) if count is not None and count.isdigit() else None,
            head=values.get("durable_head"),
        )

    def _set_meta(self, count: int, head: str) -> None:
        self._conn.executemany(
            "INSERT INTO ledger_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [("durable_count", str(count)), ("durable_head", head)],
        )

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
        created_at = _utc_now()
        cursor = self._conn.execute(
            "INSERT INTO records (payload, source_trust, durable, actionable, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (payload, str(source_trust), 1 if durable else 0, created_at),
        )
        rowid = cursor.lastrowid
        if rowid is None:  # pragma: no cover — an INSERT always yields a rowid
            raise RuntimeError("insert did not produce a row id")
        if durable:
            # VL-4: the durable receipt is the tally the health check verifies
            # against. It carries a digest of the content and a link over the
            # previous receipt; the metadata keeps the chain's length and head.
            # All of it is in the same transaction as the row, and the INSERT
            # above already holds the write lock, so the head read here is the
            # latest one.
            rid = int(rowid)
            digest = content_digest(rid, payload, created_at)
            meta = self._meta()
            prev = meta.head if meta.head is not None else GENESIS
            link = link_hash(prev, rid, digest)
            self._conn.execute(
                "INSERT INTO durable_receipts "
                "(record_id, created_at, digest, prev_hash, receipt_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (rid, created_at, digest, prev, link),
            )
            self._set_meta((meta.count or 0) + 1, link)
        self._conn.commit()
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

    def activate(
        self, record_id: int, *, channel: Channel = Channel.IN_BAND
    ) -> ActivationResult:
        """VL-2: attempt to make a record actionable intent.

        Trust-gated: an operator record activates in one call; every non-operator
        record is refused and nothing is mutated. Refusal is reported, not raised,
        so a caller can distinguish a governed refusal from a missing record
        (which raises ``KeyError``).
        """
        row = self._conn.execute(
            "SELECT source_trust, actionable FROM records WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no record with id {record_id}")
        trust = Trust(row["source_trust"])
        decision = evaluate_activation(trust)
        if not decision.allowed:
            logger.info(
                "activation refused: record=%d trust=%s channel=%s",
                record_id,
                trust,
                channel,
            )
            return ActivationResult(
                record_id, False, decision.reason, bool(row["actionable"])
            )
        # Guarded write: re-assert operator trust in the UPDATE so a non-operator
        # record can never be activated even if trust changed after the read.
        cursor = self._conn.execute(
            "UPDATE records SET actionable = 1 WHERE id = ? AND source_trust = 'operator'",
            (record_id,),
        )
        self._conn.commit()
        # Report the guarded write's real outcome, not the pre-write decision: if
        # the guard no-oped (trust changed after the SELECT), actionable is
        # unchanged and we say so rather than over-reporting success.
        activated = cursor.rowcount == 1
        logger.info(
            "activation %s: record=%d trust=%s channel=%s",
            "allowed" if activated else "guard-refused",
            record_id,
            trust,
            channel,
        )
        reason = (
            decision.reason
            if activated
            else "activation guard refused: record was not operator-trust at write time"
        )
        return ActivationResult(record_id, activated, reason, activated)

    def promote(
        self, record_id: int, *, channel: Channel = Channel.IN_BAND
    ) -> PromotionResult:
        """VL-2: mint operator trust on one exact record.

        Available only on the out-of-band channel (the operator's terminal). An
        in-band promotion attempt is refused and mutates nothing — a model cannot
        promote. Refusal is reported, not raised; a missing record raises
        ``KeyError``.

        The ``channel`` default is the safe, refused ``IN_BAND`` value: the
        keystone fails closed, so an integrator who forgets to thread the channel
        is refused rather than silently minting operator trust. The only
        legitimate caller — the out-of-band promotion CLI — passes
        ``Channel.OUT_OF_BAND`` explicitly.
        """
        row = self._conn.execute(
            "SELECT source_trust FROM records WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no record with id {record_id}")
        current = Trust(row["source_trust"])
        decision = evaluate_promotion(channel)
        if not decision.allowed:
            logger.warning(
                "promotion refused: record=%d channel=%s", record_id, channel
            )
            return PromotionResult(record_id, False, decision.reason, current)
        # No SQL-level trust backstop here (unlike activate()'s guarded UPDATE):
        # the security predicate is the channel — a call parameter, not stored row
        # state — so there is no TOCTOU to guard against. evaluate_promotion(channel)
        # above is the single line of defense by construction.
        self._conn.execute(
            "UPDATE records SET source_trust = 'operator' WHERE id = ?", (record_id,)
        )
        self._conn.commit()
        logger.info(
            "promotion: record=%d %s -> operator (out-of-band)", record_id, current
        )
        return PromotionResult(record_id, True, "promoted to operator", Trust.OPERATOR)

    def prune(self, *, keep: int) -> PruneResult:
        """VL-4: prune non-durable records to the newest ``keep``; never a durable one.

        Durable records are exempt and uncounted against the cap. The delete has a
        SQL-level ``durable = 0`` backstop in addition to the pure selection, so a
        durable record cannot be removed even if the selection were wrong; a
        mismatch trips the invariant monitor.
        """
        rows = self._conn.execute(
            "SELECT id, durable FROM records ORDER BY id DESC"
        ).fetchall()
        candidates = [
            RetentionCandidate(int(r["id"]), bool(r["durable"])) for r in rows
        ]
        to_delete = select_prunable(candidates, keep=keep)
        if not to_delete:
            return PruneResult(pruned_ids=(), pruned_count=0)
        placeholders = ",".join("?" for _ in to_delete)
        cursor = self._conn.execute(
            f"DELETE FROM records WHERE id IN ({placeholders}) AND durable = 0",
            to_delete,
        )
        self._conn.commit()
        deleted = cursor.rowcount
        InvariantMonitor(self._policy).always(
            deleted == len(to_delete),
            f"VL-4: prune removed {deleted} of {len(to_delete)} targeted rows; "
            "a durable row must never be targeted",
        )
        logger.info("pruned %d non-durable records", deleted)
        return PruneResult(pruned_ids=tuple(to_delete), pruned_count=deleted)

    def health(self) -> HealthReport:
        """VL-4: report retention health and detect any durable-integrity violation.

        Re-verifies the whole receipt chain against the live records: a receipt
        whose durable record is gone or demoted (``durable_orphans``), content
        that no longer matches its digest, a durable record without a receipt,
        and links or chain metadata that do not verify are each counted. Under
        ``RAISE`` this call raises on the first kind found; under ``REPORT`` it
        returns a report with ``ok=False`` and every counter.
        """
        total = self._count("SELECT COUNT(*) FROM records")
        durable = self._count("SELECT COUNT(*) FROM records WHERE durable = 1")
        prunable = self._count("SELECT COUNT(*) FROM records WHERE durable = 0")
        report = verify(self._receipts(), self._live_records(), self._meta())
        orphans = report.missing_records
        monitor = InvariantMonitor(self._policy)
        checks = [
            monitor.always(
                orphans == 0,
                f"VL-4: {orphans} durable record(s) lost or demoted — retention must "
                "never drop a durable record",
            ),
            monitor.always(
                report.digest_mismatches == 0,
                f"VL-4: {report.digest_mismatches} durable record(s) overwritten: "
                "content no longer matches its receipt digest",
            ),
            monitor.always(
                report.missing_receipts == 0,
                f"VL-4: {report.missing_receipts} durable record(s) without a receipt",
            ),
            monitor.always(
                report.chain_breaks == 0,
                f"VL-4: {report.chain_breaks} receipt chain break(s): a receipt "
                "was removed or altered",
            ),
        ]
        return HealthReport(
            ok=all(checks),
            total=total,
            durable_protected=durable,
            prunable=prunable,
            durable_orphans=orphans,
            violations=monitor.violations,
            digest_mismatches=report.digest_mismatches,
            missing_receipts=report.missing_receipts,
            chain_breaks=report.chain_breaks,
        )

    def _count(self, sql: str) -> int:
        row = self._conn.execute(sql).fetchone()
        return int(row[0]) if row is not None else 0

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
