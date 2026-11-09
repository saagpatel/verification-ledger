"""Small atomic JSON store. Every operation reloads out-of-band changes."""

import json
import os
import tempfile
from pathlib import Path

from memory_crash_test._json import mapping

FILENAME = "notes.json"


def load(storage_dir: Path) -> dict[str, object]:
    path = storage_dir / FILENAME
    if not path.exists():
        return {}
    value: object = json.loads(path.read_text(encoding="utf-8"))
    return mapping(value)


def save(storage_dir: Path, notes: dict[str, object]) -> None:
    storage_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=storage_dir, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(notes, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, storage_dir / FILENAME)
    finally:
        temporary.unlink(missing_ok=True)
