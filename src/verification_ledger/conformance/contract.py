"""The ``LedgerAdapter`` protocol a store implements to be graded (Phase 4).

Roughly five methods — write, read, attempt-in-band-promotion, prune, health —
so any third-party store plugs into the suite the same way the bundled reference
implementation does. See SPEC.md § "The adapter contract".
"""
