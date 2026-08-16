"""The ``LedgerAdapter`` protocol a store implements to be graded. See SPEC.md § "The adapter contract".

The suite speaks in plain strings (``"operator"``/``"agent"``/``"ingested"`` and
``"in_band"``/``"out_of_band"``) so a third-party store need not import this
package's enums. An adapter is a thin translation from these calls to the store,
and ``read`` reports the store's actual persisted state so a store that lies to
itself cannot pass by lying to the suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# Trust labels and channels, as the suite passes them. These MUST equal the
# reference Trust/Channel enum values (a test asserts it).
OPERATOR = "operator"
AGENT = "agent"
INGESTED = "ingested"
IN_BAND = "in_band"
OUT_OF_BAND = "out_of_band"


@dataclass(frozen=True)
class StoredRecord:
    """What a read exposes about one record: its persisted state, as the store sees it."""

    record_id: int
    source_trust: str
    actionable: bool
    envelope: dict[str, str] | None


class LedgerAdapter(Protocol):
    """The surface a store binds to be graded by the conformance suite."""

    def fresh(self) -> None:
        """Reset to an empty store; each probe runs on a clean slate."""
        ...

    def write(
        self, payload: str, *, source_trust: str, channel: str, durable: bool
    ) -> int:
        """Store a record and return its id. Must honor the VL-1 in-band operator clamp."""
        ...

    def read(self, record_id: int) -> StoredRecord | None:
        """Return the record's persisted trust, actionable flag, and VL-3 envelope."""
        ...

    def activate(self, record_id: int, *, channel: str) -> bool:
        """Attempt the actionable transition; return whether the record is now actionable."""
        ...

    def promote(self, record_id: int, *, channel: str) -> bool:
        """Attempt to mint operator trust; return whether the record is now operator."""
        ...

    def prune(self, *, keep: int) -> None:
        """Prune non-durable records to the newest ``keep``; durable records are exempt."""
        ...

    def count_records(self) -> int:
        """Total records currently stored (for VL-4 cap checks)."""
        ...

    def health_ok(self) -> bool:
        """Whether the store reports itself healthy (VL-4)."""
        ...
