"""CLI entry point for verification-ledger.

Phase 0 provides ``--version`` and a pointer to the contract. The promotion
ceremony (VL-2) and the conformance runner land in later phases.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "--version":
        from verification_ledger import __version__

        print(__version__)
        return 0
    print(
        "verification-ledger — see SPEC.md for the ledger contract. "
        "The promotion CLI and conformance runner land in later phases."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
