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

    def seed_operator(self, payload: str) -> int:
        """Seed one record at ``operator`` trust via the store's OUT-OF-BAND ceremony
        and return its id.

        This is the store's own legitimate operator-write path, and the suite is
        deliberately agnostic about which one it is: an out-of-band write for a
        store that supports one, or an in-band ``agent`` write followed by the
        out-of-band promotion ceremony for a store (such as bridge-db) that mints
        ``operator`` only through promotion. It must NOT take any in-band
        shortcut — the VL-2 adversarial probes independently verify that the
        in-band path to ``operator`` stays closed, so declaring the seed here
        cannot be used to game the score.
        """
        ...

    def prune(self) -> None:
        """Prune non-durable records to this store's own cap (``prune_cap``);
        durable records are exempt. A store that prunes automatically on write may
        implement this as a no-op."""
        ...

    def prune_cap(self) -> int:
        """The store's non-durable retention cap: the number of non-durable records
        it keeps after pruning. The suite sizes its VL-4 workload from this so a
        store with a fixed cap (bridge-db keeps 50 per source) and one with a
        configurable cap are graded the same way."""
        ...

    def count_records(self) -> int:
        """Total records currently stored (for VL-4 cap checks)."""
        ...

    def health_ok(self) -> bool:
        """Whether the store reports itself healthy (VL-4)."""
        ...
