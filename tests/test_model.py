"""The VL trust model types."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from verification_ledger.model import DEFAULT_TRUST, Channel, Record, Trust


def test_trust_values() -> None:
    assert Trust.OPERATOR == "operator"
    assert Trust.AGENT == "agent"
    assert Trust.INGESTED == "ingested"
    assert str(Trust.AGENT) == "agent"


def test_default_trust_is_agent() -> None:
    assert DEFAULT_TRUST is Trust.AGENT


def test_channels() -> None:
    assert Channel.IN_BAND == "in_band"
    assert Channel.OUT_OF_BAND == "out_of_band"


def test_record_is_frozen() -> None:
    record = Record(
        id=1,
        payload="x",
        source_trust=Trust.AGENT,
        durable=False,
        actionable=False,
        created_at="2026-01-01T00:00:00.000000Z",
    )
    with pytest.raises(FrozenInstanceError):
        setattr(record, "payload", "y")  # noqa: B010 — dynamic set probes immutability
