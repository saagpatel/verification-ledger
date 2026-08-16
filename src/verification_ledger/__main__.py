"""CLI entry point for verification-ledger.

Subcommands:
  --version                        print the package version
  promote <db_path> <record_id>    the out-of-band operator promotion ceremony

The conformance runner (``python -m verification_ledger.conformance``) lands in
a later phase.
"""

from __future__ import annotations

import sys

_USAGE = (
    "verification-ledger — subcommands:\n"
    "  --version                      print the package version\n"
    "  promote <db_path> <record_id>  out-of-band operator promotion (SPEC.md § VL-2)\n"
)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "--version":
        from verification_ledger import __version__

        print(__version__)
        return 0
    if args and args[0] == "promote":
        from verification_ledger.promote import main as promote_main

        return promote_main(args[1:])
    print(_USAGE, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
