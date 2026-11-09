"""Direct JSON inspection, independent of a running fixture or MCP client."""

from pathlib import Path

from memory_crash_test._json import mapping, string
from memory_crash_test.fixtures.reference_server.storage import load
from memory_crash_test.inspect.base import Provenance, StorageSnapshot, StoredNote, Unreadable


class ReferenceInspector:
    def snapshot(self, storage_dir: Path) -> StorageSnapshot | Unreadable:
        try:
            notes: StorageSnapshot = {}
            for key, value in load(storage_dir).items():
                note = mapping(value)
                raw_provenance = note.get("provenance")
                provenance: Provenance | None | Unreadable
                if raw_provenance is None:
                    provenance = None
                elif raw_provenance in ("operator", "agent", "ingested"):
                    # Narrow literals individually rather than trusting decoded JSON.
                    if raw_provenance == "operator":
                        provenance = "operator"
                    elif raw_provenance == "agent":
                        provenance = "agent"
                    else:
                        provenance = "ingested"
                else:
                    provenance = Unreadable("unrecognized stored provenance value")
                if note.get("key") != key:
                    return Unreadable("stored identity disagrees with its JSON key")
                notes[key] = StoredNote(key, string(note["text"]), provenance)
            return notes
        except (OSError, ValueError, KeyError) as exc:
            return Unreadable(f"cannot inspect notes.json: {exc}")
