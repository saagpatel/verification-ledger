"""Thin MCP stdio wrapper over the ledger (opt-in: requires the ``mcp`` extra).

This is the IN-BAND surface — everything a model can reach. Every write hardcodes
``Channel.IN_BAND``, and the surface deliberately does NOT expose a promotion
tool: minting operator trust is out-of-band only (the ``verification-ledger
promote`` CLI). So a model driving this server can never mint operator trust or
make a non-operator record actionable — VL-1 and VL-2 hold structurally at this
boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from verification_ledger.ledger import EnvelopedRecord, Ledger
from verification_ledger.model import Channel, Trust

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

# The in-band tools this server exposes. A promotion tool is intentionally absent.
IN_BAND_TOOL_NAMES = (
    "vl_write",
    "vl_read",
    "vl_read_all",
    "vl_activate",
    "vl_prune",
    "vl_health",
)


def _record_dict(env: EnvelopedRecord) -> dict[str, Any]:
    rec = env.record
    return {
        "id": rec.id,
        "payload": rec.payload,
        "source_trust": str(rec.source_trust),
        "durable": rec.durable,
        "actionable": rec.actionable,
        "created_at": rec.created_at,
        "instruction_boundary": {
            "kind": env.envelope["kind"],
            "source_trust": env.envelope["source_trust"],
            "warning": env.envelope["warning"],
        },
    }


def in_band_tools(ledger: Ledger) -> dict[str, Callable[..., Any]]:
    """The in-band tool callables bound to ``ledger``. No promotion tool exists here.

    Exposed separately from MCP registration so the surface is testable without
    the ``mcp`` extra: the absence of any promotion tool is the structural
    guarantee, and every write is pinned to the in-band channel.
    """

    def vl_write(
        payload: str, source_trust: str = "agent", durable: bool = False
    ) -> dict[str, Any]:
        """Store a record on the in-band channel; a request for operator trust is clamped to agent."""
        r = ledger.write(
            payload,
            source_trust=Trust(source_trust),
            channel=Channel.IN_BAND,
            durable=durable,
        )
        return {
            "record_id": r.record_id,
            "source_trust": str(r.source_trust),
            "clamped": r.clamped,
        }

    def vl_read(record_id: int) -> dict[str, Any] | None:
        """Read a record wrapped in its instruction-boundary envelope."""
        env = ledger.read(record_id)
        return None if env is None else _record_dict(env)

    def vl_read_all() -> list[dict[str, Any]]:
        """Every record, each wrapped in its envelope, oldest first."""
        return [_record_dict(e) for e in ledger.read_all()]

    def vl_activate(record_id: int) -> dict[str, Any]:
        """Attempt the actionable transition on the in-band channel (operator-trust only)."""
        r = ledger.activate(record_id, channel=Channel.IN_BAND)
        return {
            "record_id": r.record_id,
            "allowed": r.allowed,
            "actionable": r.actionable,
            "reason": r.reason,
        }

    def vl_prune(keep: int) -> dict[str, Any]:
        """Prune non-durable records to the newest ``keep``; durable records are exempt."""
        r = ledger.prune(keep=keep)
        return {"pruned_ids": list(r.pruned_ids), "pruned_count": r.pruned_count}

    def vl_health() -> dict[str, Any]:
        """Retention health, including any durable-loss violation."""
        h = ledger.health()
        return {
            "ok": h.ok,
            "total": h.total,
            "durable_protected": h.durable_protected,
            "prunable": h.prunable,
            "durable_orphans": h.durable_orphans,
            "violations": list(h.violations),
        }

    return {
        "vl_write": vl_write,
        "vl_read": vl_read,
        "vl_read_all": vl_read_all,
        "vl_activate": vl_activate,
        "vl_prune": vl_prune,
        "vl_health": vl_health,
    }


def build_server(ledger: Ledger, name: str = "verification-ledger") -> FastMCP:
    """Register the in-band tools on a FastMCP server bound to ``ledger``.

    ``mcp`` is imported lazily so the core library never depends on it.
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(name)
    for fn in in_band_tools(ledger).values():
        mcp.tool()(fn)
    return mcp


def main(argv: list[str] | None = None) -> int:
    """Run the stdio MCP server over a ledger at ``argv[0]`` (default ``:memory:``)."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    db_path = args[0] if args else ":memory:"
    build_server(Ledger(db_path)).run()
    return 0
