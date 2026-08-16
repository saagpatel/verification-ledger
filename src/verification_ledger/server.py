"""Thin MCP stdio wrapper over the ledger (Phase 5, opt-in).

Exposes write/read/attempt-promotion/prune to an MCP-speaking fleet. Imported
only when the ``mcp`` optional dependency is installed; the core library and the
conformance suite never depend on MCP.
"""
