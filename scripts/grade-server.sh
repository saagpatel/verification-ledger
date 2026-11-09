#!/bin/sh
set -eu
if [ "$#" -ne 2 ]; then
  echo "usage: $0 <server> <outdir>" >&2
  exit 2
fi
exec uv run python -m memory_crash_test grade --server "$1" --out "$2"
