"""Adapters translate identities and tool responses, never storage evidence."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from memory_crash_test.inspect.base import Provenance as Provenance

EVIDENCE_LIMIT = 1000


class UnsupportedCapability(Exception):
    """The server has no declared ceremony for the requested operation."""


@dataclass(frozen=True)
class ServerRelease:
    name: str
    version: str
    source: str


@dataclass(frozen=True)
class CallRecord:
    tool_name: str
    args: str  # JSON text, truncated to EVIDENCE_LIMIT characters


@dataclass(frozen=True)
class ReadResult:
    text: str
    envelope: dict[str, object] | None
    raw_response: str  # truncated to EVIDENCE_LIMIT characters


class MemoryServerAdapter(Protocol):
    release: ServerRelease

    def start(self, storage_dir: Path) -> None: ...
    def stop(self) -> None: ...
    def restart(self) -> None: ...
    def write_note(self, key: str, text: str, *, provenance: Provenance) -> bool: ...
    def update_provenance(self, key: str, provenance: Provenance) -> bool: ...
    def operator_promote(self, key: str) -> bool: ...
    def read_note(self, key: str) -> ReadResult | None: ...
    def list_keys(self) -> list[str]: ...
    def delete_note(self, key: str) -> bool: ...
    def calls(self) -> list[CallRecord]: ...
