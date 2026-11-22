"""VL-4 durable integrity: store-level attacks on durable records, and the migration.

Each durable record has a receipt carrying a SHA-256 digest of its content and a
link over the previous receipt; the ledger's metadata keeps the chain's length
and head (``integrity.py``). These properties attack a store directly through
SQL, the way housekeeping or a careless writer would, and require ``health()``
to report each attack with its own counter under both failure policies:

- overwrite of a durable payload: ``digest_mismatches``;
- deletion of a durable row: ``durable_orphans`` (missing records);
- deletion of a durable row together with its receipt, from anywhere in the
  chain including the newest: ``chain_breaks`` (plus the missing record when
  only the receipt goes);
- deletion of a receipt alone: ``missing_receipts`` and ``chain_breaks``.

Two cases are pinned from the earlier strict xfails, now passing. One case is
pinned as a documented limit: a writer who rewrites records, receipts and
metadata consistently inside the database is not detectable without an anchor
outside it.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from verification_ledger.integrity import (
    GENESIS,
    LOST_BEFORE_MIGRATION,
    content_digest,
    link_hash,
)
from verification_ledger.invariants import FailurePolicy, InvariantViolation
from verification_ledger.ledger import HealthReport, Ledger

POLICIES = st.sampled_from(list(FailurePolicy))
PAYLOADS = st.text(max_size=20)


def _conn(led: Ledger) -> sqlite3.Connection:
    return led._conn  # pyright: ignore[reportPrivateUsage]


@st.composite
def stores(draw: st.DrawFn) -> list[tuple[str, bool]]:
    """A write sequence with at least one durable record."""
    writes = draw(st.lists(st.tuples(PAYLOADS, st.booleans()), max_size=10))
    at = draw(st.integers(0, len(writes)))
    writes.insert(at, (draw(PAYLOADS), True))
    return writes


def _build(writes: list[tuple[str, bool]]) -> tuple[Ledger, list[int]]:
    led = Ledger(policy=FailurePolicy.REPORT)
    durable: list[int] = []
    for payload, d in writes:
        rid = led.write(payload, durable=d).record_id
        if d:
            durable.append(rid)
    assert led.health().ok
    return led, durable


def _health(led: Ledger, policy: FailurePolicy) -> HealthReport | None:
    """health() under ``policy``; None if it raised (RAISE detected a violation)."""
    led._policy = policy  # pyright: ignore[reportPrivateUsage]
    try:
        return led.health()
    except InvariantViolation:
        return None


# --- the two pinned counterexamples, now detected ---


@given(
    policy=POLICIES,
    forged=st.text(max_size=20),
    writes=stores(),
    pick=st.integers(0, 50),
)
@example(
    policy=FailurePolicy.REPORT,
    forged="forged",
    writes=[("shipped artifact", True)],
    pick=0,
)
def test_rewrite_of_a_durable_record_is_detected(
    policy: FailurePolicy, forged: str, writes: list[tuple[str, bool]], pick: int
) -> None:
    led, durable = _build(writes)
    rid = durable[pick % len(durable)]
    original = led.read(rid)
    assert original is not None
    conn = _conn(led)
    conn.execute("UPDATE records SET payload = ? WHERE id = ?", (forged, rid))
    conn.commit()
    h = _health(led, policy)
    if forged == original.record.payload:
        assert h is not None and h.ok  # no change, nothing to report
    elif policy is FailurePolicy.RAISE:
        assert h is None
    else:
        assert h is not None and h.ok is False
        assert h.digest_mismatches == 1
        assert h.violation_count == 1
    led.close()


@given(policy=POLICIES, writes=stores(), pick=st.integers(0, 50))
@example(policy=FailurePolicy.REPORT, writes=[("shipped artifact", True)], pick=0)
def test_durable_loss_with_its_receipt_is_detected(
    policy: FailurePolicy, writes: list[tuple[str, bool]], pick: int
) -> None:
    # Anywhere in the chain: a middle receipt breaks the next link; the newest
    # leaves every link valid and is caught by the chain length and head.
    led, durable = _build(writes)
    rid = durable[pick % len(durable)]
    conn = _conn(led)
    conn.execute("DELETE FROM records WHERE id = ?", (rid,))
    conn.execute("DELETE FROM durable_receipts WHERE record_id = ?", (rid,))
    conn.commit()
    h = _health(led, policy)
    if policy is FailurePolicy.RAISE:
        assert h is None
    else:
        assert h is not None and h.ok is False
        assert h.chain_breaks >= 1
        assert h.durable_orphans == 0 and h.digest_mismatches == 0
    led.close()


# --- the other single-step attacks ---


@given(policy=POLICIES, writes=stores(), pick=st.integers(0, 50))
def test_durable_row_deletion_is_a_missing_record(
    policy: FailurePolicy, writes: list[tuple[str, bool]], pick: int
) -> None:
    led, durable = _build(writes)
    rid = durable[pick % len(durable)]
    conn = _conn(led)
    conn.execute("DELETE FROM records WHERE id = ?", (rid,))
    conn.commit()
    h = _health(led, policy)
    if policy is FailurePolicy.RAISE:
        assert h is None
    else:
        assert h is not None and h.ok is False
        assert (
            h.durable_orphans,
            h.digest_mismatches,
            h.missing_receipts,
            h.chain_breaks,
        ) == (1, 0, 0, 0)
    led.close()


@given(policy=POLICIES, writes=stores(), pick=st.integers(0, 50))
def test_receipt_deletion_is_a_missing_receipt_and_a_chain_break(
    policy: FailurePolicy, writes: list[tuple[str, bool]], pick: int
) -> None:
    led, durable = _build(writes)
    rid = durable[pick % len(durable)]
    conn = _conn(led)
    conn.execute("DELETE FROM durable_receipts WHERE record_id = ?", (rid,))
    conn.commit()
    h = _health(led, policy)
    if policy is FailurePolicy.RAISE:
        assert h is None
    else:
        assert h is not None and h.ok is False
        assert h.missing_receipts == 1 and h.chain_breaks >= 1
    led.close()


@given(
    writes=stores(),
    pick=st.integers(0, 50),
    field=st.sampled_from(["digest", "prev_hash", "receipt_hash"]),
)
def test_altered_receipt_link_is_a_chain_break(
    writes: list[tuple[str, bool]], pick: int, field: str
) -> None:
    led, durable = _build(writes)
    rid = durable[pick % len(durable)]
    conn = _conn(led)
    conn.execute(
        f"UPDATE durable_receipts SET {field} = ? WHERE record_id = ?", ("f" * 64, rid)
    )
    conn.commit()
    h = led.health()
    assert h.ok is False and h.chain_breaks >= 1
    led.close()


def test_missing_chain_metadata_is_reported_not_recreated() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "fleet.db")
        with Ledger(db, policy=FailurePolicy.REPORT) as led:
            led.write("a", durable=True)
            _conn(led).execute("DELETE FROM ledger_meta")
            _conn(led).commit()
            assert led.health().chain_breaks == 1
        # Reopening must not quietly rebuild the metadata from the receipts.
        with Ledger(db, policy=FailurePolicy.REPORT) as led:
            assert led.health().chain_breaks == 1


def test_non_durable_rewrite_is_out_of_scope() -> None:
    # Non-durable records have no receipt and may be pruned at any time; VL-4
    # makes no claim about them.
    led = Ledger(policy=FailurePolicy.REPORT)
    rid = led.write("scratch").record_id
    _conn(led).execute("UPDATE records SET payload = 'other' WHERE id = ?", (rid,))
    assert led.health().ok is True


# --- the documented limit ---


def test_consistent_whole_chain_rewrite_is_not_detectable_in_store() -> None:
    # Moved to "can't be tested as stated": a writer with write access who
    # rewrites the record, recomputes its digest and every later link, and
    # updates the metadata leaves a store that verifies. The hash has no key and
    # the chain has no anchor outside the database. This pins the limit so a
    # change to it (for example an external head anchor) is noticed.
    led, durable = _build([("a", True), ("b", True), ("c", False), ("d", True)])
    conn = _conn(led)
    target = durable[0]
    conn.execute("UPDATE records SET payload = 'forged' WHERE id = ?", (target,))
    prev = GENESIS
    rows = conn.execute(
        "SELECT r.id, r.payload, r.created_at FROM records r "
        "JOIN durable_receipts d ON d.record_id = r.id ORDER BY r.id"
    ).fetchall()
    for rid, payload, created_at in rows:
        digest = content_digest(int(rid), str(payload), str(created_at))
        link = link_hash(prev, int(rid), digest)
        conn.execute(
            "UPDATE durable_receipts SET digest = ?, prev_hash = ?, receipt_hash = ? "
            "WHERE record_id = ?",
            (digest, prev, link, rid),
        )
        prev = link
    conn.execute("UPDATE ledger_meta SET value = ? WHERE key = 'durable_head'", (prev,))
    conn.commit()
    assert led.health().ok is True
    led.close()


# --- migration of stores written by the previous version ---

_LEGACY_SCHEMA = """
CREATE TABLE records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    payload      TEXT NOT NULL,
    source_trust TEXT NOT NULL CHECK (source_trust IN ('operator', 'agent', 'ingested')),
    durable      INTEGER NOT NULL DEFAULT 0 CHECK (durable IN (0, 1)),
    actionable   INTEGER NOT NULL DEFAULT 0 CHECK (actionable IN (0, 1)),
    created_at   TEXT NOT NULL
);
CREATE TABLE durable_receipts (
    record_id  INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""


def _legacy_store(db: str, writes: list[tuple[str, bool]], lost: set[int]) -> list[int]:
    """Write a store the way the previous version did; drop the rows in ``lost``."""
    raw = sqlite3.connect(db)
    raw.executescript(_LEGACY_SCHEMA)
    durable: list[int] = []
    for i, (payload, d) in enumerate(writes):
        cur = raw.execute(
            "INSERT INTO records (payload, source_trust, durable, created_at) "
            "VALUES (?, 'agent', ?, ?)",
            (payload, 1 if d else 0, f"2026-01-01T00:00:{i:02d}.000000Z"),
        )
        rid = int(cur.lastrowid or 0)
        if d:
            raw.execute(
                "INSERT INTO durable_receipts (record_id, created_at) VALUES (?, 't')",
                (rid,),
            )
            durable.append(rid)
    for i in lost:
        if durable:
            raw.execute(
                "DELETE FROM records WHERE id = ?", (durable[i % len(durable)],)
            )
    raw.commit()
    raw.close()
    return durable


def _receipts_only(led: Ledger) -> list[tuple[object, ...]]:
    return [
        tuple(r)
        for r in _conn(led).execute("SELECT * FROM durable_receipts ORDER BY record_id")
    ]


def _receipt_rows(led: Ledger) -> list[tuple[object, ...]]:
    return _receipts_only(led) + [
        tuple(r) for r in _conn(led).execute("SELECT * FROM ledger_meta ORDER BY key")
    ]


@given(
    writes=stores(),
    lost=st.sets(st.integers(0, 20), max_size=2),
    forged=st.text(min_size=1, max_size=10),
)
def test_migration_chains_a_legacy_store_idempotently(
    writes: list[tuple[str, bool]], lost: set[int], forged: str
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "legacy.db")
        durable = _legacy_store(db, writes, lost)
        gone = {durable[i % len(durable)] for i in lost}
        with Ledger(db, policy=FailurePolicy.REPORT) as led:
            h = led.health()
            # Losses from before the upgrade are still reported, nothing else is.
            assert (
                h.durable_orphans,
                h.digest_mismatches,
                h.missing_receipts,
                h.chain_breaks,
            ) == (len(gone), 0, 0, 0)
            sentinels = {
                int(r[0])
                for r in _conn(led).execute(
                    "SELECT record_id FROM durable_receipts WHERE digest = ?",
                    (LOST_BEFORE_MIGRATION,),
                )
            }
            assert sentinels == gone
            first = _receipts_only(led)
            # New writes extend the migrated chain.
            led.write("after upgrade", durable=True)
            assert led.health().violation_count == len(gone)
            after_write = _receipt_rows(led)
        with Ledger(db, policy=FailurePolicy.REPORT) as led:
            assert _receipt_rows(led) == after_write  # reopening changes nothing
            # The migrated receipts are an unchanged prefix of the extended chain.
            assert after_write[: len(first)] == first
            survivors = [r for r in durable if r not in gone]
            if survivors:
                _conn(led).execute(
                    "UPDATE records SET payload = payload || ? WHERE id = ?",
                    (forged, survivors[0]),
                )
                _conn(led).commit()
                assert led.health().digest_mismatches == 1


def test_migration_of_an_empty_legacy_store_starts_a_valid_chain() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "legacy.db")
        _legacy_store(db, [], set())
        with Ledger(db, policy=FailurePolicy.REPORT) as led:
            assert led.health().ok
            led.write("first durable", durable=True)
            assert led.health().ok


@pytest.mark.parametrize("policy", list(FailurePolicy))
def test_raise_policy_names_the_violation_kind(policy: FailurePolicy) -> None:
    led, durable = _build([("a", True), ("b", True)])
    _conn(led).execute("UPDATE records SET payload = 'x' WHERE id = ?", (durable[0],))
    led._policy = policy  # pyright: ignore[reportPrivateUsage]
    if policy is FailurePolicy.RAISE:
        with pytest.raises(InvariantViolation, match="overwritten"):
            led.health()
    else:
        assert led.health().violations == (
            "VL-4: 1 durable record(s) overwritten: content no longer matches its "
            "receipt digest",
        )


# --- reopening must never repair evidence (round 2 review) ---
#
# The migration runs only on a real schema transition (a pre-chain
# durable_receipts table without integrity columns). In a store already on the
# chain schema, NULL integrity fields and missing metadata are evidence; neither
# reopening nor a later write may recompute or recreate them.


def _reopen_health(db: str) -> HealthReport:
    with Ledger(db, policy=FailurePolicy.REPORT) as led:
        return led.health()


def _evidence(db: str) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    raw = sqlite3.connect(db)
    try:
        receipts = [
            tuple(r)
            for r in raw.execute("SELECT * FROM durable_receipts ORDER BY record_id")
        ]
        meta = [tuple(r) for r in raw.execute("SELECT * FROM ledger_meta ORDER BY key")]
    finally:
        raw.close()
    return receipts, meta


@given(
    writes=stores(),
    pick=st.integers(0, 50),
    nulled=st.sets(
        st.sampled_from(["digest", "prev_hash", "receipt_hash"]), min_size=1
    ),
    forged=st.text(min_size=1, max_size=10),
    overwrite=st.booleans(),
)
@example(
    writes=[("shipped artifact", True)],
    pick=0,
    nulled={"digest"},
    forged="forged",
    overwrite=True,
)
def test_reopening_never_backfills_nulled_receipt_fields(
    writes: list[tuple[str, bool]],
    pick: int,
    nulled: set[str],
    forged: str,
    overwrite: bool,
) -> None:
    # The review's counterexample: overwrite a durable payload and NULL its
    # receipt digest. Before the fix, reopening treated the NULL as legacy data
    # and recomputed digest, link and metadata, reporting healthy.
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "fleet.db")
        with Ledger(db, policy=FailurePolicy.REPORT) as led:
            durable: list[int] = []
            for payload, d in writes:
                rid = led.write(payload, durable=d).record_id
                if d:
                    durable.append(rid)
            rid = durable[pick % len(durable)]
            conn = _conn(led)
            if overwrite:
                conn.execute(
                    "UPDATE records SET payload = payload || ? WHERE id = ?",
                    (forged, rid),
                )
            for col in sorted(nulled):
                conn.execute(
                    f"UPDATE durable_receipts SET {col} = NULL WHERE record_id = ?",
                    (rid,),
                )
            conn.commit()
            before = led.health()
        assert before.ok is False and before.chain_breaks >= 1
        if overwrite:
            assert before.digest_mismatches == 1
        evidence = _evidence(db)
        for _ in range(2):
            after = _reopen_health(db)
            assert after == before  # identical report, every counter
            assert _evidence(db) == evidence  # reopening rewrote nothing


@given(
    writes=stores(),
    drop=st.sampled_from(["rows", "version", "count", "head", "table"]),
    erase_all=st.booleans(),
    then_write=st.booleans(),
)
def test_missing_metadata_is_never_recreated(
    writes: list[tuple[str, bool]], drop: str, erase_all: bool, then_write: bool
) -> None:
    # Deleting the metadata (all of it, one key, or the whole table), even
    # together with every durable row and receipt, stays reported across
    # reopening and across later durable writes.
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "fleet.db")
        with Ledger(db, policy=FailurePolicy.REPORT) as led:
            for payload, d in writes:
                led.write(payload, durable=d)
            conn = _conn(led)
            if erase_all:
                conn.execute("DELETE FROM records WHERE durable = 1")
                conn.execute("DELETE FROM durable_receipts")
            if drop == "table":
                conn.execute("DROP TABLE ledger_meta")
            elif drop == "rows":
                conn.execute("DELETE FROM ledger_meta")
            else:
                key = {
                    "version": "schema_version",
                    "count": "durable_count",
                    "head": "durable_head",
                }[drop]
                conn.execute("DELETE FROM ledger_meta WHERE key = ?", (key,))
            conn.commit()
        assert _reopen_health(db).chain_breaks >= 1
        if then_write:
            with Ledger(db, policy=FailurePolicy.REPORT) as led:
                led.write("later", durable=True)
                led.write("later, plain")
        h = _reopen_health(db)
        assert h.ok is False and h.chain_breaks >= 1


def test_migration_runs_only_on_the_pre_chain_schema() -> None:
    # A store already on the chain schema is never migrated again, whatever its
    # receipts look like; a pre-chain store is migrated exactly once.
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "legacy.db")
        _legacy_store(db, [("a", True), ("b", True)], set())
        with Ledger(db, policy=FailurePolicy.REPORT) as led:
            assert led.health().ok
            version = (
                _conn(led)
                .execute("SELECT value FROM ledger_meta WHERE key = 'schema_version'")
                .fetchone()
            )
            assert version is not None and version[0] == "2"
            _conn(led).execute(
                "UPDATE durable_receipts SET digest = NULL, prev_hash = NULL, receipt_hash = NULL"
            )
            _conn(led).commit()
        evidence = _evidence(db)
        assert _reopen_health(db).chain_breaks >= 1
        assert _evidence(db) == evidence


def test_rebuilding_the_pre_chain_schema_is_the_documented_limit() -> None:
    # Out of scope, like the consistent whole-chain rewrite: a writer who drops
    # the integrity columns and the metadata so the store looks pre-chain gets
    # a fresh migration on the next open. Pinned so a change is noticed.
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "fleet.db")
        with Ledger(db, policy=FailurePolicy.REPORT) as led:
            rid = led.write("shipped", durable=True).record_id
            conn = _conn(led)
            conn.execute("UPDATE records SET payload = 'forged' WHERE id = ?", (rid,))
            for col in ("digest", "prev_hash", "receipt_hash"):
                conn.execute(f"ALTER TABLE durable_receipts DROP COLUMN {col}")
            conn.execute("DROP TABLE ledger_meta")
            conn.commit()
        assert _reopen_health(db).ok is True
