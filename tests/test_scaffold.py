"""Phase 0 scaffold sanity: the package imports and the contract ships."""

from __future__ import annotations

from pathlib import Path

import verification_ledger

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_version() -> None:
    assert verification_ledger.__version__ == "0.1.0"


def test_spec_present_and_defines_all_invariants() -> None:
    spec = REPO_ROOT / "SPEC.md"
    assert spec.is_file(), "SPEC.md (the contract) must ship at the repo root"
    body = spec.read_text(encoding="utf-8")
    for invariant in ("VL-1", "VL-2", "VL-3", "VL-4"):
        assert invariant in body, f"{invariant} missing from SPEC.md"
