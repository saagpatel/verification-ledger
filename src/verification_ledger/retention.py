"""VL-4 retention invariant (Phase 3).

Durable-tagged records are permanently prune-exempt; all others prune to a cap.
A lost durable record or an orphaned receipt is a detectable violation surfaced
by the store's health check — never silent. See SPEC.md § VL-4.
"""
