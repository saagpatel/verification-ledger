"""Inspectors read files or databases directly, including while a server is stopped."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

Provenance = Literal["operator", "agent", "ingested"]


@dataclass(frozen=True)
class Unreadable:
    """A storage fact the inspector cannot assess; never use for an absent field."""

    reason: str


@dataclass(frozen=True)
class StoredNote:
    key: str
    text: str
    provenance: Provenance | None | Unreadable


type StorageSnapshot = dict[str, StoredNote]


class EvidenceSnapshot(dict[str, StoredNote]):
    """Optional secondary evidence; dict equality still compares only notes."""

    def __init__(self, *, evidence: dict[str, object]) -> None:
        super().__init__()
        self.evidence = evidence


class StorageInspector(Protocol):
    def snapshot(self, storage_dir: Path) -> StorageSnapshot | Unreadable: ...
