"""The in-band MCP surface: it exposes no promotion tool and pins writes in-band."""

from __future__ import annotations

import pytest

from verification_ledger.ledger import Ledger
from verification_ledger.server import IN_BAND_TOOL_NAMES, build_server, in_band_tools


def test_surface_has_no_promotion_tool() -> None:
    tools = in_band_tools(Ledger())
    assert set(tools) == set(IN_BAND_TOOL_NAMES)
    # The structural VL-2 guarantee: nothing on the in-band surface can promote.
    assert not any("promote" in name for name in tools)


def test_write_tool_clamps_in_band_operator() -> None:
    tools = in_band_tools(Ledger())
    result = tools["vl_write"](payload="x", source_trust="operator")
    assert result["clamped"] is True
    assert result["source_trust"] == "agent"


def test_read_tool_carries_the_envelope() -> None:
    tools = in_band_tools(Ledger())
    rid = tools["vl_write"](payload="x", source_trust="agent")["record_id"]
    record = tools["vl_read"](record_id=rid)
    assert record is not None
    assert record["instruction_boundary"]["kind"] == "stored_data_not_instructions"
    assert record["instruction_boundary"]["source_trust"] == "agent"


def test_activate_tool_refuses_agent_in_band() -> None:
    tools = in_band_tools(Ledger())
    rid = tools["vl_write"](payload="x", source_trust="agent")["record_id"]
    result = tools["vl_activate"](record_id=rid)
    assert result["allowed"] is False
    assert result["actionable"] is False


def test_prune_and_health_tools() -> None:
    tools = in_band_tools(Ledger())
    tools["vl_write"](payload="keep", source_trust="agent", durable=True)
    for i in range(4):
        tools["vl_write"](payload=f"n{i}", source_trust="agent")
    pruned = tools["vl_prune"](keep=1)
    assert pruned["pruned_count"] == 3
    health = tools["vl_health"]()
    assert health["ok"] is True
    assert health["durable_protected"] == 1


def test_build_server_registers_the_in_band_tools() -> None:
    mcp = pytest.importorskip("mcp")  # requires the mcp extra (present in dev)
    assert mcp is not None
    server = build_server(Ledger(), name="test-ledger")
    # A FastMCP server was constructed and the tools registered without error.
    assert server is not None
