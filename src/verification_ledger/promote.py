"""The out-of-band operator promotion ceremony. See SPEC.md § VL-2.

Run by an operator at a terminal — a process a model cannot invoke. This is the
only surface that mints operator trust. The in-band MCP surface never exposes
promotion and always passes ``Channel.IN_BAND``, so promotion is structurally
unavailable to a model; running this CLI is the out-of-band act.

    verification-ledger promote <db_path> <record_id>
"""

from __future__ import annotations

import argparse

from verification_ledger.ledger import Ledger
from verification_ledger.model import Channel


def promote(db_path: str, record_id: int) -> int:
    """Promote one record to operator trust. Returns a process exit code."""
    with Ledger(db_path) as led:
        if led.read(record_id) is None:
            print(f"no record with id {record_id}")
            return 1
        result = led.promote(record_id, channel=Channel.OUT_OF_BAND)
        print(
            f"record {record_id}: {result.reason} "
            f"(source_trust now {result.source_trust})"
        )
        return 0 if result.promoted else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verification-ledger promote",
        description="Out-of-band operator promotion: mint operator trust on one record.",
    )
    parser.add_argument("db_path", help="path to the ledger SQLite file")
    parser.add_argument("record_id", type=int, help="id of the record to promote")
    args = parser.parse_args(argv)
    return promote(str(args.db_path), int(args.record_id))


if __name__ == "__main__":
    raise SystemExit(main())
