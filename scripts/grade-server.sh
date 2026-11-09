#!/bin/sh
# The registered adapter reuses or installs its exact pinned distribution.
set -eu
if [ "$#" -ne 2 ]; then
  echo "usage: $0 <server> <outdir>" >&2
  exit 2
fi
MCT_LIVE=1 uv run --extra crash python -m memory_crash_test grade --server "$1" --out "$2"
