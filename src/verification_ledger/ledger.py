"""The reference implementation: a SQLite-backed governed ledger (Phases 1, 3).

Implements VL-1 (provenance typing, in-band operator clamp) and VL-4 (retention
invariant) over a minimal single-table store. Kept small on purpose: the point
is a store readable in one sitting, not feature parity with any specific fleet.
"""
