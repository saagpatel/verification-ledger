# Packet 06 — Cheating-store regression

## Goal
Prove the Memory Crash Test suite catches a liar. Build a deliberately dishonest stdio MCP memory server that reports success for everything while not storing correctly. Then add regression tests asserting the suite grades it **FAIL on VL-4 ("saved notes never quietly vanish")**, with evidence showing the missing notes.

Read first: `docs/memory-crash-test.md`, `src/memory_crash_test/adapters/base.py`, `src/memory_crash_test/inspect/base.py`, `src/memory_crash_test/fixtures/reference_server/` (copy its shape), and `src/memory_crash_test/servers.py` (registry).

## Scope
- `src/memory_crash_test/fixtures/cheating_store/`: a server built like the reference fixture (same tool names, same JSON-file storage format, so it can share the reference inspector), with these lies, each switchable by an env var `MCT_CHEAT_MODES` (comma-separated, default `all`):
  - `drop_on_restart`: every second note written is kept only in process memory, never flushed to disk, but the write returns success. While the process is up, it shows up in reads and lists. After a restart it is gone.
  - `phantom_read`: read/list also return a cached copy of a note even after it has been deleted from disk, so the server's answers disagree with storage.
  - `collateral_delete`: deleting one note also silently removes the oldest other non-deleted note from disk.
  Otherwise behave like the reference fixture (provenance clamp, envelope, out-of-band promote), so only VL-4 is meant to fail.
- An adapter and inspector for it (the inspector may reuse the reference one), registered in `servers.py` as `cheating-store` with `ServerRelease(name="mct-cheating-store", version="fixture", source="fixture")`.
- `tests/crash/test_cheating_store.py`:
  - With all modes on: VL-4 is `FAIL`, and VL-1, VL-2 and VL-3 are `PASS`. That shows the failure is located correctly rather than everything failing at once.
  - For each mode run alone: VL-4 is `FAIL`, and the failing probe's evidence (`before`/`after` snapshots + reason) names at least one note key that the server reported as saved but storage lacks, or the mismatch between the read and storage.
  - The reference fixture still grades all-PASS in the same test module, for contrast.
  - The grade never comes from return values: assert that the cheating store's write calls all returned success in the call log.

## Out of scope
Do not change the probes, grader, or reference fixture unless the cheating store exposes a real gap (a lie the probes miss). If it does, fix the probe minimally, add a test, and explain the change in the commit message. Do not touch `src/verification_ledger/`.

## Acceptance
```
uv run ruff check
uv run pyright
uv run pytest -q
uv run python -m memory_crash_test grade --server cheating-store --out "$TMPDIR/mct-cheat" && python3 -c "import json,sys; g={x['guarantee']:x['status'] for x in json.load(open(sys.argv[1]))['guarantees']}; assert g['VL-4']=='FAIL' and g['VL-1']==g['VL-2']==g['VL-3']=='PASS', g" "$TMPDIR/mct-cheat/grade.json"
```
