"""Inspect the server's persisted JSONL graph directly."""
import json
from pathlib import Path

from memory_crash_test._json import mapping, sequence, string
from memory_crash_test.inspect.base import GraphSnapshot, Provenance, StorageSnapshot, StoredNote, Unreadable


class ServerMemoryInspector:
    def snapshot(self, storage_dir: Path) -> StorageSnapshot | Unreadable:
        path = storage_dir / "memory.jsonl"
        try:
            if not path.exists():
                return {}
            notes: StorageSnapshot = {}
            relations: list[dict[str, str]] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                record: object = json.loads(line)
                row = mapping(record)
                if row.get("type") == "relation":
                    relations.append({field: string(row[field]) for field in ("from", "to", "relationType")})
                    continue
                if row.get("type") != "entity":
                    raise ValueError("unknown graph record type")
                key = string(row["name"])
                if key in notes:
                    raise ValueError("duplicate entity name")
                raw = row.get("entityType")
                provenance: Provenance | None | Unreadable
                if raw is None:
                    provenance = None
                elif raw == "operator":
                    provenance = "operator"
                elif raw == "agent":
                    provenance = "agent"
                elif raw == "ingested":
                    provenance = "ingested"
                else:
                    provenance = Unreadable("unrecognized stored provenance value")
                text = "\n".join(string(item) for item in sequence(row["observations"]))
                notes[key] = StoredNote(key, text, provenance)
            return GraphSnapshot(notes, relations)
        except (OSError, ValueError, KeyError) as exc:
            return Unreadable(f"cannot inspect memory.jsonl: {exc}")
