"""CLI: grade a local server, or publish static grade cards."""

import argparse
import sys
from pathlib import Path

from memory_crash_test.grade.grader import grade_server
from memory_crash_test.publish.render import load_grades, write_cards
from memory_crash_test.servers import SERVERS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory_crash_test")
    commands = parser.add_subparsers(dest="command", required=True)

    grade = commands.add_parser("grade", help="grade a registered local fixture/server")
    grade.add_argument("--server", required=True, choices=sorted(SERVERS))
    grade.add_argument("--out", required=True, type=Path)

    publish = commands.add_parser("publish", help="write static HTML grade cards")
    publish.add_argument(
        "--grades", required=True, nargs="+", type=Path,
        help="grade.json files or directories that contain them",
    )
    publish.add_argument("--out", required=True, type=Path, help="directory for the index and cards")

    args = parser.parse_args(argv)
    if args.command == "grade":
        _write_grade(args.server, args.out)
    elif args.command == "publish":
        try:
            _publish(args.grades, args.out)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
    else:
        parser.error("unknown command")
    return 0


def _write_grade(server: str, out: Path) -> None:
    binding = SERVERS[server]
    report = grade_server(binding.adapter_factory, binding.inspector_factory())
    out.mkdir(parents=True, exist_ok=True)
    (out / "grade.json").write_text(report.to_json(), encoding="utf-8")


def _publish(grades: list[Path], out: Path) -> None:
    write_cards(load_grades(grades), out)


if __name__ == "__main__":
    raise SystemExit(main())
