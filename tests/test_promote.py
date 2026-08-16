"""The out-of-band promotion ceremony (CLI), end to end on a file-backed store."""

from __future__ import annotations

from pathlib import Path

from verification_ledger import promote as promote_cli
from verification_ledger.ledger import Ledger
from verification_ledger.model import Channel, Trust


def test_cli_promotes_then_record_can_activate(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    with Ledger(db) as led:
        rid = led.write("needs review", source_trust=Trust.AGENT).record_id
        assert led.activate(rid).allowed is False  # in-band: cannot activate

    # The operator runs the out-of-band ceremony — a separate process.
    assert promote_cli.main([str(db), str(rid)]) == 0

    with Ledger(db) as led:
        env = led.read(rid)
        assert env is not None
        assert env.record.source_trust is Trust.OPERATOR
        result = led.activate(rid, channel=Channel.IN_BAND)
        assert result.allowed is True  # legitimate fast path, one call
        assert result.actionable is True


def test_cli_missing_record_returns_error(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    with Ledger(db):
        pass
    assert promote_cli.main([str(db), "999"]) == 1
