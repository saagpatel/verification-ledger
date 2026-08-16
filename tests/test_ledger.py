"""VL-1 provenance typing (with the in-band operator clamp) and VL-3 envelope,
exercised against the reference ledger.
"""

from __future__ import annotations

from verification_ledger.ledger import Ledger
from verification_ledger.model import Channel, Trust


def test_default_write_is_agent() -> None:
    with Ledger() as led:
        result = led.write("note")
        assert result.source_trust is Trust.AGENT
        assert result.clamped is False
        env = led.read(result.record_id)
        assert env is not None
        assert env.record.source_trust is Trust.AGENT


def test_in_band_operator_is_clamped() -> None:
    with Ledger() as led:
        # An in-band writer (the default channel) cannot mint operator trust.
        result = led.write("privileged", source_trust=Trust.OPERATOR)
        assert result.clamped is True
        assert result.source_trust is Trust.AGENT
        env = led.read(result.record_id)
        assert env is not None
        assert env.record.source_trust is Trust.AGENT


def test_out_of_band_operator_is_stored() -> None:
    with Ledger() as led:
        # The out-of-band channel — the operator's terminal — may store operator.
        result = led.write(
            "directive", source_trust=Trust.OPERATOR, channel=Channel.OUT_OF_BAND
        )
        assert result.clamped is False
        assert result.source_trust is Trust.OPERATOR
        env = led.read(result.record_id)
        assert env is not None
        assert env.record.source_trust is Trust.OPERATOR


def test_ingested_is_stored_as_ingested() -> None:
    with Ledger() as led:
        result = led.write("scraped", source_trust=Trust.INGESTED)
        assert result.clamped is False
        assert result.source_trust is Trust.INGESTED


def test_every_read_carries_envelope() -> None:
    with Ledger() as led:
        rid = led.write("a").record_id
        env = led.read(rid)
        assert env is not None
        assert env.envelope["kind"] == "stored_data_not_instructions"
        assert env.envelope["source_trust"] == env.record.source_trust
        led.write("b")
        all_reads = led.read_all()
        assert len(all_reads) == 2
        assert all(
            r.envelope["source_trust"] == r.record.source_trust for r in all_reads
        )


def test_read_missing_returns_none() -> None:
    with Ledger() as led:
        assert led.read(999) is None
