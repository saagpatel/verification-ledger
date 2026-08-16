"""Scoring: per-invariant pass plus the top-line Ledger Conformance Score (Phase 4).

An invariant passes only if its positive-probe pass rate and adversarial-probe
block rate are both 1.0. The top-line score is the fraction of the four
invariants fully passed. See SPEC.md § "Conformance scoring".
"""
