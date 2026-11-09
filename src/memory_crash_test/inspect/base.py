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


class GraphSnapshot(dict[str, StoredNote]):
    """Note identities with separately recorded native graph edges."""

    def __init__(
        self,
        notes: dict[str, StoredNote],
        relations: list[dict[str, str]],
        *,
        evidence: dict[str, object] | None = None,
    ) -> None:
        super().__init__(notes)
        self.relations = relations
        self.evidence = evidence


type StorageSnapshot = dict[str, StoredNote]


class EvidenceSnapshot(dict[str, StoredNote]):
    """Optional secondary evidence; dict equality still compares only notes."""

    def __init__(self, *, evidence: dict[str, object]) -> None:
        super().__init__()
        self.evidence = evidence


class StorageInspector(Protocol):
    def snapshot(self, storage_dir: Path) -> StorageSnapshot | Unreadable: ...
