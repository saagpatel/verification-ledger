"""Property tests for the ledger's remaining claims, one claim per test.

The stateful machine in ``test_prop_ledger_stateful.py`` covers the four
invariants under random interleavings. This file covers what an interleaving
cannot reach on its own: the pure retention selection, the failure-policy
monitor, the out-of-band promotion CLI against a file store, the shape of the
in-band MCP surface, and the README quickstart. Store-level attacks on durable
records are in ``test_prop_durable_integrity.py``.
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
import tempfile
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from verification_ledger.envelope import BOUNDARY_KIND, BOUNDARY_WARNING, boundary
from verification_ledger.invariants import (
    FailurePolicy,
    InvariantMonitor,
    InvariantViolation,
)
from verification_ledger.ledger import Ledger
from verification_ledger.model import Channel, Trust
from verification_ledger.promote import promote as promote_cli
from verification_ledger.retention import RetentionCandidate, select_prunable
from verification_ledger.server import IN_BAND_TOOL_NAMES, in_band_tools

TRUSTS = st.sampled_from(list(Trust))
POLICIES = st.sampled_from(list(FailurePolicy))

# --- VL-4: the pure retention selection (retention.py) ---


@st.composite
def candidate_lists(draw: st.DrawFn) -> list[RetentionCandidate]:
    """Newest-first candidates with unique descending ids and random durability."""
    ids = draw(st.lists(st.integers(1, 10_000), unique=True, max_size=40))
    flags = draw(st.lists(st.booleans(), min_size=len(ids), max_size=len(ids)))
    return [
        RetentionCandidate(rid, d)
        for rid, d in zip(sorted(ids, reverse=True), flags, strict=True)
    ]


@given(cands=candidate_lists(), keep=st.integers(0, 50))
def test_select_prunable_never_selects_durable_and_keeps_newest_non_durable(
    cands: list[RetentionCandidate], keep: int
) -> None:
    selected = select_prunable(cands, keep=keep)
    durable = {c.record_id for c in cands if c.durable}
    non_durable = [c.record_id for c in cands if not c.durable]
    assert not durable & set(selected)  # the exemption is absolute
    # Durable records are not counted against the cap: exactly the newest
    # ``keep`` non-durable records survive, however many durable ones exist.
    assert selected == non_durable[keep:]
    assert set(non_durable) - set(selected) == set(non_durable[:keep])


@given(cands=candidate_lists(), keep=st.integers(max_value=-1))
def test_select_prunable_rejects_negative_keep(
    cands: list[RetentionCandidate], keep: int
) -> None:
    with pytest.raises(ValueError):
        select_prunable(cands, keep=keep)


# --- VL-4: the failure-policy monitor (invariants.py) ---


class _Errors(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@given(
    policy=POLICIES,
    checks=st.lists(st.tuples(st.booleans(), st.text(max_size=10)), max_size=20),
)
def test_monitor_never_passes_a_violation_unnoticed(
    policy: FailurePolicy, checks: list[tuple[bool, str]]
) -> None:
    errors = _Errors()
    log = logging.getLogger("verification_ledger.invariants")
    log.addHandler(errors)
    try:
        monitor = InvariantMonitor(policy)
        expected: list[str] = []
        for condition, message in checks:
            if condition:
                assert monitor.always(True, message) is True
                continue
            expected.append(message)
            if policy is FailurePolicy.RAISE:
                with pytest.raises(InvariantViolation):
                    monitor.always(False, message)
            else:
                assert monitor.always(False, message) is False
        # Every violation is logged, under either policy, and recorded.
        assert errors.messages == [f"invariant violated: {m}" for m in expected]
        assert monitor.violations == tuple(expected)
    finally:
        log.removeHandler(errors)


# --- VL-1: labels at the storage layer, and the clamp record ---


@given(label=st.text(max_size=12).filter(lambda s: s not in {str(t) for t in Trust}))
@example(label="OPERATOR")
@example(label="operator ")
def test_storage_rejects_any_label_outside_the_trust_set(label: str) -> None:
    led = Ledger()
    conn: sqlite3.Connection = led._conn  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO records (payload, source_trust, created_at) VALUES ('x', ?, 't')",
            (label,),
        )
    with pytest.raises(ValueError):
        in_band_tools(led)["vl_write"](payload="x", source_trust=label)
    assert led.read_all() == []
    led.close()


# --- VL-2: the out-of-band promotion CLI on a file store ---


@given(
    trusts=st.lists(TRUSTS, min_size=1, max_size=6),
    pick=st.integers(0, 100),
    missing=st.integers(1, 5),
)
def test_promotion_cli_mints_operator_on_exactly_one_record(
    trusts: list[Trust], pick: int, missing: int
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "fleet.db")
        with Ledger(db) as led:
            ids = [
                led.write("p", source_trust=t, channel=Channel.IN_BAND).record_id
                for t in trusts
            ]
            before = {e.record.id: e.record for e in led.read_all()}
        target = ids[pick % len(ids)]
        # A missing id is refused with exit 1 and mutates nothing.
        assert promote_cli(db, max(ids) + missing) == 1
        assert promote_cli(db, target) == 0
        with Ledger(db) as led:
            after = {e.record.id: e.record for e in led.read_all()}
        assert after[target].source_trust is Trust.OPERATOR
        assert after[target].actionable is before[target].actionable
        for rid, rec in before.items():
            if rid != target:
                assert after[rid] == rec


# --- VL-1 / VL-2: the in-band MCP surface ---


def test_in_band_surface_has_no_promotion_and_no_channel_parameter() -> None:
    tools = in_band_tools(Ledger())
    assert tuple(tools) == IN_BAND_TOOL_NAMES
    assert not any("promote" in name for name in tools)
    for name, fn in tools.items():
        params = inspect.signature(fn).parameters
        assert "channel" not in params, f"{name} lets a model choose its channel"


@given(requested=TRUSTS, durable=st.booleans(), payload=st.text(max_size=20))
def test_in_band_surface_can_never_mint_or_activate_non_operator(
    requested: Trust, durable: bool, payload: str
) -> None:
    led = Ledger()
    tools = in_band_tools(led)
    out = tools["vl_write"](
        payload=payload, source_trust=str(requested), durable=durable
    )
    assert out["source_trust"] != str(Trust.OPERATOR)
    assert out["clamped"] is (requested is Trust.OPERATOR)
    act = tools["vl_activate"](record_id=out["record_id"])
    assert act["allowed"] is False and act["actionable"] is False
    led.close()


@given(requested=TRUSTS, payload=st.text(max_size=20))
def test_library_channel_is_caller_asserted(requested: Trust, payload: str) -> None:
    # Documents, rather than proves, the SPEC.md claim that an in-band writer can
    # never mint operator trust. SPEC.md calls library calls in-band, but
    # Ledger.write() and Ledger.promote() take ``channel`` as a plain argument,
    # so any library caller that passes OUT_OF_BAND mints operator trust. The
    # code cannot tell who is calling; the guarantee holds only on surfaces
    # that pin the channel (the MCP tools, the default arguments). This test
    # pins the current behavior so a change to it is noticed.
    led = Ledger()
    w = led.write(payload, source_trust=requested, channel=Channel.OUT_OF_BAND)
    assert w.source_trust is requested and w.clamped is False
    p = led.promote(w.record_id, channel=Channel.OUT_OF_BAND)
    assert p.promoted is True
    led.close()


# --- VL-3: the envelope value ---


@given(trust=TRUSTS)
def test_boundary_is_exact_for_every_trust(trust: Trust) -> None:
    assert dict(boundary(trust)) == {
        "kind": BOUNDARY_KIND,
        "source_trust": str(trust),
        "warning": BOUNDARY_WARNING,
    }


# --- README quickstart ---


@given(payload=st.text(max_size=30))
def test_readme_quickstart_holds_for_any_payload(payload: str) -> None:
    led = Ledger()
    r = led.write(payload, source_trust=Trust.OPERATOR)
    assert r.clamped and r.source_trust is Trust.AGENT
    assert led.activate(r.record_id).allowed is False
    led.promote(r.record_id, channel=Channel.OUT_OF_BAND)
    assert led.activate(r.record_id).allowed is True
    env = led.read(r.record_id)
    assert env is not None and env.envelope["kind"] == BOUNDARY_KIND
    led.close()
