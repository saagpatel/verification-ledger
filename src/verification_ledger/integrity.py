"""VL-4 durable integrity: content digests and the receipt hash chain. See SPEC.md § VL-4.

Every durable record gets a receipt carrying a SHA-256 digest of the record's
canonical content (id, payload, created_at) and a link hash over the previous
receipt's link hash, so the receipts form a chain in record-id order. The
ledger's metadata keeps the chain's length and head. ``verify`` re-walks the
chain against the live records and counts four kinds of violation separately:

- ``digest_mismatches``: a durable record whose content no longer matches its
  receipt (an overwrite);
- ``missing_records``: a receipt whose durable record is gone or no longer
  durable (a loss or a demotion);
- ``missing_receipts``: a durable record with no receipt;
- ``chain_breaks``: a receipt whose links do not verify, or a chain whose
  length or head disagrees with the metadata (a receipt removed from the middle
  or the end of the chain).

This module is pure (no I/O) so the checks are exhaustively testable; the ledger
owns the tables and applies it.

What it cannot catch: everything here lives in the same database, and the hash
has no secret key. A writer who rewrites records, receipts and metadata
consistently (recomputing every digest and link after the change) produces a
store that verifies. Detecting that needs an anchor outside the database, such
as the head hash recorded somewhere the writer cannot reach.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

GENESIS = "0" * 64
# Digest recorded by the migration for a receipt whose record was already gone
# when the store was upgraded: there is no content left to hash, and the loss
# is still reported as a missing record.
LOST_BEFORE_MIGRATION = "lost-before-migration"


def content_digest(record_id: int, payload: str, created_at: str) -> str:
    """SHA-256 of a durable record's canonical content.

    Covers what must never change for a durable record. ``source_trust`` and
    ``actionable`` are deliberately excluded: out-of-band promotion and
    activation legitimately change them.
    """
    canonical = json.dumps(
        ["vl-durable-v1", record_id, payload, created_at],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def link_hash(prev_hash: str, record_id: int, digest: str) -> str:
    """The chain link for one receipt."""
    return hashlib.sha256(f"{prev_hash}:{record_id}:{digest}".encode()).hexdigest()


@dataclass(frozen=True)
class Receipt:
    record_id: int
    digest: str | None
    prev_hash: str | None
    receipt_hash: str | None


@dataclass(frozen=True)
class LiveRecord:
    payload: str
    created_at: str
    durable: bool


@dataclass(frozen=True)
class Meta:
    """The chain's length and head as the store recorded them (None if absent)."""

    count: int | None
    head: str | None


@dataclass(frozen=True)
class IntegrityReport:
    digest_mismatches: int
    missing_records: int
    missing_receipts: int
    chain_breaks: int

    @property
    def total(self) -> int:
        return (
            self.digest_mismatches
            + self.missing_records
            + self.missing_receipts
            + self.chain_breaks
        )


def verify(
    receipts: Sequence[Receipt],
    records: Mapping[int, LiveRecord],
    meta: Meta,
) -> IntegrityReport:
    """Re-verify every receipt against the live records, the chain, and the metadata.

    ``receipts`` must be in record-id order (the chain order).
    """
    digest_mismatches = missing_records = chain_breaks = 0
    expected_prev = GENESIS
    for r in receipts:
        rec = records.get(r.record_id)
        if rec is None or not rec.durable:
            missing_records += 1
        elif r.digest != content_digest(r.record_id, rec.payload, rec.created_at):
            digest_mismatches += 1
        if (
            r.digest is None
            or r.prev_hash != expected_prev
            or r.receipt_hash != link_hash(expected_prev, r.record_id, r.digest)
        ):
            chain_breaks += 1
        # Continue from what the receipt claims, so one break is counted once
        # rather than cascading down the rest of the chain.
        expected_prev = r.receipt_hash if r.receipt_hash is not None else expected_prev
    head = receipts[-1].receipt_hash if receipts else GENESIS
    if meta.count != len(receipts) or meta.head != head:
        chain_breaks += 1  # truncated, extended, or metadata missing
    receipted = {r.record_id for r in receipts}
    missing_receipts = sum(
        1 for rid, rec in records.items() if rec.durable and rid not in receipted
    )
    return IntegrityReport(
        digest_mismatches=digest_mismatches,
        missing_records=missing_records,
        missing_receipts=missing_receipts,
        chain_breaks=chain_breaks,
    )


def relink(
    receipts: Sequence[Receipt], records: Mapping[int, LiveRecord]
) -> list[Receipt]:
    """Fill digests and links for receipts that have none (the migration step).

    Receipts that already carry a link are kept exactly as they are, so running
    this twice changes nothing and an existing chain is never recomputed.
    """
    out: list[Receipt] = []
    prev = GENESIS
    for r in receipts:
        if r.receipt_hash is not None and r.digest is not None:
            out.append(r)
            prev = r.receipt_hash
            continue
        digest = r.digest
        if digest is None:
            rec = records.get(r.record_id)
            digest = (
                LOST_BEFORE_MIGRATION
                if rec is None
                else content_digest(r.record_id, rec.payload, rec.created_at)
            )
        link = link_hash(prev, r.record_id, digest)
        out.append(Receipt(r.record_id, digest, prev, link))
        prev = link
    return out
