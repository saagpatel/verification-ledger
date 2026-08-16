"""VL-3 instruction-boundary envelope."""

from __future__ import annotations

from verification_ledger.envelope import BOUNDARY_KIND, boundary
from verification_ledger.model import Trust


def test_envelope_shape() -> None:
    env = boundary(Trust.AGENT)
    assert env["kind"] == BOUNDARY_KIND == "stored_data_not_instructions"
    assert env["source_trust"] == "agent"
    warning = env["warning"].lower()
    assert "not" in warning and "instruction" in warning


def test_envelope_reflects_trust() -> None:
    for trust in Trust:
        assert boundary(trust)["source_trust"] == str(trust)
