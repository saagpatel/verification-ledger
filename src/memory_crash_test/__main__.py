"""CLI: python -m memory_crash_test grade --server reference --out DIR."""

import argparse
from pathlib import Path

from memory_crash_test.grade.grader import grade_server
from memory_crash_test.servers import SERVERS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory_crash_test")
    commands = parser.add_subparsers(dest="command", required=True)
    grade = commands.add_parser("grade", help="grade a registered local fixture/server")
    grade.add_argument("--server", required=True, choices=sorted(SERVERS))
    grade.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    binding = SERVERS[args.server]
    report = grade_server(binding.adapter_factory, binding.inspector_factory())
    destination = Path(args.out)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "grade.json").write_text(report.to_json(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
