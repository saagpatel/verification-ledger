"""Core types: the trust model and the record shape.

See SPEC.md § "The trust model".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Trust(StrEnum):
    """Provenance label on an instruction-bearing record.

    Only ``operator`` may drive privileged action; ``agent`` is the conservative
    default; ``ingested`` (external, untrusted input) is the strictest label.
    """

    OPERATOR = "operator"
    AGENT = "agent"
    INGESTED = "ingested"


class Channel(StrEnum):
    """The surface a write or promotion arrives on.

    ``IN_BAND`` is anything a model can reach (library calls, the MCP tool
    surface). ``OUT_OF_BAND`` is a surface a model cannot reach — an operator at
    a terminal. Only the out-of-band channel may mint operator trust; that gap is
    VL-2's whole mechanism.
    """

    IN_BAND = "in_band"
    OUT_OF_BAND = "out_of_band"


DEFAULT_TRUST: Trust = Trust.AGENT


@dataclass(frozen=True)
class Record:
    """A stored coordination record — the read shape.

    ``actionable`` is the generic analogue of a handoff crossing pending → active:
    a record promoted to actionable operator intent. VL-2 governs that transition
    (Phase 2); at write time every record is non-actionable.
    """

    id: int
    payload: str
    source_trust: Trust
    durable: bool
    actionable: bool
    created_at: str
