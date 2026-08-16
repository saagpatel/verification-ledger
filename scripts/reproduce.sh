#!/usr/bin/env bash
# One-command local gate: install, lint, type-check, test.
# Later phases append the conformance run and the replay demo.
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync
uv run ruff check
uv run pyright
uv run pytest
echo "OK: lint + type-check + tests green."
