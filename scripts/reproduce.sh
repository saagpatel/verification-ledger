#!/usr/bin/env bash
# One-command local gate: install, lint, type-check, test, and grade the
# reference store against the four-invariant contract (must score 1.00).
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync
uv run ruff check
uv run pyright
uv run pytest
uv run python -m verification_ledger.conformance
echo "OK: lint + type-check + tests + conformance green."
