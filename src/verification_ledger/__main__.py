"""CLI entry point for verification-ledger.

Subcommands:
  --version                        print the package version
  promote <db_path> <record_id>    the out-of-band operator promotion ceremony
  serve [db_path]                  run the in-band MCP stdio server (needs the mcp extra)
  demo                             run the scripted replay on synthetic data

The conformance runner is ``python -m verification_ledger.conformance``.
"""

from __future__ import annotations

import sys

_USAGE = (
    "verification-ledger — subcommands:\n"
    "  --version                      print the package version\n"
    "  promote <db_path> <record_id>  out-of-band operator promotion (SPEC.md § VL-2)\n"
    "  serve [db_path]                in-band MCP stdio server (needs the mcp extra)\n"
    "  demo                           scripted replay on synthetic data\n"
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
    if args and args[0] == "serve":
        from verification_ledger.server import main as serve_main

        try:
            return serve_main(args[1:])
        except ImportError:
            print(
                "the MCP server requires the 'mcp' extra: "
                "pip install 'verification-ledger[mcp]'"
            )
            return 1
    if args and args[0] == "demo":
        from verification_ledger.demo.replay import main as demo_main

        return demo_main()
    print(_USAGE, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
