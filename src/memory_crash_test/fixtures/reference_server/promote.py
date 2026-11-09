"""Separate operator-only process, never exposed as an MCP tool."""

import sys
from pathlib import Path

from memory_crash_test._json import mapping
from memory_crash_test.fixtures.reference_server.storage import load, save


def main() -> int:
    storage_dir, key = Path(sys.argv[1]), sys.argv[2]
    notes = load(storage_dir)
    if key not in notes:
        return 1
    note = mapping(notes[key])
    note["provenance"] = "operator"
    notes[key] = note
    save(storage_dir, notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
