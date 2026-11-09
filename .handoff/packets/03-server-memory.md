# Packet 03 — Adapter + inspector for server A: `@modelcontextprotocol/server-memory`

## Goal
Let the Memory Crash Test probes drive the official knowledge-graph memory server and read what it actually persisted. When this is done, all four guarantees produce a real grade (PASS, FAIL or NOT_GRADABLE, with evidence) for one exact pinned release.

Read first: the stress-test findings (kept outside the repo) (this server's findings and the recommended mapping table; follow it), `docs/memory-crash-test.md`, `src/memory_crash_test/adapters/base.py`, `inspect/base.py`, the reference fixture adapter/inspector, and `servers.py`.

## Scope
- `src/memory_crash_test/adapters/server_memory.py` and `src/memory_crash_test/inspect/server_memory.py`, registered in `servers.py` as `server-memory`.
- **Pinning:** the exact version from the stress test. Install it into a cache dir the suite controls (e.g. `$MCT_CACHE/server-memory/<version>`, default under `~/.cache/memory-crash-test`) with `npm install --prefix <dir> @modelcontextprotocol/server-memory@<version>`, and launch the installed binary with `node`. Never `npx` an unpinned version. `ServerRelease.version` must be read from the installed package's `package.json`, not from a constant, so the card names what really ran.
- **Storage:** point it at `<storage_dir>/memory.jsonl` (or whatever the stress test found), and make sure nothing is written outside `storage_dir` (redirect `HOME` for the child if needed). The inspector parses that file directly. Use the stress test's mapping for key→entity, provenance→the native field, `update_provenance` via in-band tools, `read_note` (envelope is `None` unless the server returns a structural marker), and `delete_note`. If no out-of-band ceremony exists, `operator_promote` raises `UnsupportedCapability`, so VL-2's positive probe fails with that reason (a FAIL, not NOT_GRADABLE, per the docs).
- **Tests:** `tests/crash/test_server_memory.py`, marked `@pytest.mark.live` (register the marker in pyproject and skip `live` by default via `addopts = "-m 'not live'"`, so CI stays offline). Cover a start/write/restart/inspect smoke test and the full grade, asserting that each guarantee has a status and every probe has before/after snapshots. Add one offline unit test of the inspector against a checked-in tiny JSONL sample under `tests/crash/data/`.
- A `scripts/grade-server.sh <server> <outdir>` helper (if one doesn't exist yet) that installs, grades and writes `grade.json`.

## Head notes (binding; they refine the stress test)
- The release is already installed at `~/.cache/memory-crash-test/server-memory/2.1.0` (npm prefix) and `~/.cache/memory-crash-test/basic-memory/1.0.0` (a uv venv with `bin/basic-memory`). You have **no network**. The installer function should use the cache when it's present, install only when it's missing (that code path is untested here; keep it small), and honor `MCT_CACHE` to override the root.
- The adapters observe native behavior. They never enforce a guarantee on the server's behalf: they pass operator claims through unchanged, never clamp, never synthesize an envelope, and raise `UnsupportedCapability` for the missing operator ceremony.
- VL-4 compares **note keys only**. Relation rows that cascade with a deleted entity are recorded as evidence and are never counted as a vanished note.
- Restart is a clean stop: close stdio, wait for the child to exit within a bounded time (basic-memory: give it the native drain the stress test describes, at most 10 s), then relaunch with the identical environment. Never add extra sleeps or retries that a normal client wouldn't do, and never repair storage. If a note the server acknowledged is missing from persisted storage after a clean restart, that is a real FAIL, and the evidence must show it.
- basic-memory: the Markdown files are the persisted source of truth for the inspector. Read-only SQLite rows are secondary evidence; record any disagreement in the snapshot's evidence, but don't let it rescue a missing file. Apply every offline and telemetry switch from the stress test's B.2 and B.8, and redirect HOME, config and caches under `storage_dir`.
- Keys are safe generated slugs (`[a-z0-9-]`).
- Report in your summary which probes failed and why, quoting each reason string.

## Rules
Run the server only locally over stdio, on storage this suite creates. The VL-3 instruction-like payload is only ever sent to that local process. No telemetry, no other network access except the npm install.

## Acceptance
```
uv run ruff check
uv run pyright
uv run pytest -q
uv run pytest -q -m live tests/crash/test_server_memory.py
MCT_LIVE=1 uv run python -m memory_crash_test grade --server server-memory --out "$TMPDIR/mct-a" && python3 -c "import json,sys; g=json.load(open(sys.argv[1])); assert g['release']['version'] and len(g['guarantees'])==4 and all(p['before'] is not None for x in g['guarantees'] for p in x['probes']), g" "$TMPDIR/mct-a/grade.json"
```
Report the four statuses you observed and the release in your final summary.
