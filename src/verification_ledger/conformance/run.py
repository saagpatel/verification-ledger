"""Conformance CLI: grade an adapter and print the report.

    python -m verification_ledger.conformance [reference | module:callable] [--json]

Default adapter is ``reference`` (the bundled store, which must score 1.0). Any
other store is graded by passing ``module:callable`` where ``callable()`` returns
a LedgerAdapter. Exit code is 0 only on a perfect score.
"""

from __future__ import annotations

import argparse
import importlib
import json

from verification_ledger.conformance.contract import LedgerAdapter
from verification_ledger.conformance.score import ConformanceReport, score_adapter


def load_adapter(spec: str) -> LedgerAdapter:
    if spec == "reference":
        from verification_ledger.conformance.adapters.reference import ReferenceAdapter

        return ReferenceAdapter()
    if ":" not in spec:
        raise SystemExit(
            f"unknown adapter {spec!r}; use 'reference' or 'module:callable'"
        )
    module_name, _, attr = spec.partition(":")
    module = importlib.import_module(module_name)
    factory = getattr(module, attr)
    return factory()


def report_to_dict(report: ConformanceReport) -> dict[str, object]:
    return {
        "score": report.score,
        "passed": report.passed_count,
        "total": report.total_invariants,
        "invariants": [
            {
                "invariant": s.invariant,
                "passed": s.passed,
                "positive": f"{s.positive_passed}/{s.positive_total}",
                "adversarial": f"{s.adversarial_passed}/{s.adversarial_total}",
            }
            for s in report.invariants
        ],
    }


def print_report(report: ConformanceReport) -> None:
    print("Ledger Conformance")
    print("=" * 46)
    for s in report.invariants:
        mark = "PASS" if s.passed else "FAIL"
        print(
            f"  {s.invariant}  {mark}   "
            f"positive {s.positive_passed}/{s.positive_total}   "
            f"adversarial {s.adversarial_passed}/{s.adversarial_total}"
        )
    print("-" * 46)
    print(
        f"  Score {report.score:.2f}  "
        f"({report.passed_count}/{report.total_invariants} invariants)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verification-ledger conformance",
        description="Grade a ledger store against the four-invariant contract.",
    )
    parser.add_argument(
        "adapter",
        nargs="?",
        default="reference",
        help="'reference' (default) or 'module:callable' returning a LedgerAdapter",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)
    report = score_adapter(load_adapter(str(args.adapter)))
    if bool(args.json):
        print(json.dumps(report_to_dict(report), indent=2))
    else:
        print_report(report)
    return 0 if report.perfect else 1


if __name__ == "__main__":
    raise SystemExit(main())
