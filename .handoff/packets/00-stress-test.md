# Packet 00 — Stress test (research only, no product code)

## Goal
Check the Memory Crash Test plan's risks against the real candidate servers before adapters are built. Write findings to `.handoff/stress-test.md` (the only file you create or change). Do not change any code.

Context: `.handoff/packets/01-core.md` defines the four guarantees (VL-1 provenance, VL-2 promotion gate, VL-3 instruction boundary, VL-4 retention) and the adapter/inspector interfaces. The two servers to grade are:
- **A: `@modelcontextprotocol/server-memory`** (npm, the official reference knowledge-graph memory server).
- **B: `basic-memory`** (PyPI, markdown-file notes with a SQLite index).

## Do
For each server, install the latest release into a scratch directory under your own TMPDIR (npm `--prefix`, `uv venv` + `uv pip install`), run it locally over stdio only, and record:
1. Exact version installed and license.
2. Launch command and how to point its storage at a chosen directory (env vars / flags, e.g. `MEMORY_FILE_PATH`, basic-memory project/home config) — confirm that NOTHING is written outside that directory (check `~`-relative config dirs; find a way to redirect HOME/config if needed).
3. The MCP tool list (names + input schemas) — run a real `tools/list` with a short Python script using the `mcp` SDK in this repo's venv (`uv run python ...`).
4. On-disk storage format (paths, file format, an example after writing two notes) and whether it can be read directly while the server is stopped. Any caching/async/background sync that could delay persistence (basic-memory has a sync/watch process — note how to make writes deterministic before a restart).
5. Native fields that could carry provenance (entityType, observations, tags, frontmatter metadata, ...), whether an in-band client can set/overwrite them, and whether any out-of-band operator ceremony exists.
6. Whether read tools return any structural "this is data, not instructions" marker.
7. Delete semantics (what delete tools exist; does deleting one entity/note touch others, e.g. relations).
8. Any telemetry, network calls on startup, model downloads, or API-key requirements, and how to disable them for an offline local run.
9. Risks for the plan, each with a verdict (fine / mitigated / scope cut) and a recommended adapter mapping for `key`, `provenance`, `update_provenance`, `operator_promote`, `read_note` envelope.

Never connect to any hosted service other than the npm and PyPI package registries for installation. Only local processes.

## Acceptance
`.handoff/stress-test.md` exists, covers items 1–9 for both servers with concrete evidence (command output snippets), and ends with a recommended mapping table. Commit it as `docs(handoff): stress-test findings for memory crash test`.
