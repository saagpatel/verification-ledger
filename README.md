# verification-ledger

A governed coordination ledger for local multi-agent fleets — and an executable
conformance suite that proves a store enforces its guarantees.

Most agent fleets share state through a plain file or an ungoverned store. That
lets one agent's output silently become the next agent's "operator intent"
(laundering), and lets ordinary housekeeping silently drop a durable record
(loss). A README asking everyone to be careful is a word-level guarantee. This
project makes the guarantees *mechanisms* — properties of the store that hold
regardless of what any agent does — and ships a test suite that mechanically
proves a given store enforces them.

## The four guarantees

- **VL-1 Provenance typing** — every record carries `source_trust`
  (`operator | agent | ingested`, default `agent`); an in-band writer can never
  mint `operator`.
- **VL-2 Promotion gate** — a non-operator record cannot become actionable
  intent in-band; only an out-of-band operator ceremony promotes it. The strict
  sink is refuse-until-promoted.
- **VL-3 Instruction-boundary envelope** — every read is wrapped so a consuming
  model is told, structurally, that this is stored data, not an instruction.
- **VL-4 Retention invariant** — durable-tagged records are never pruned; a lost
  durable record or orphaned receipt is a detectable violation, never silent.

The precise contract is in [SPEC.md](SPEC.md). The conformance suite scores any
store, bidirectionally, so neither a block-everything nor an allow-everything
store passes.

## Status

Phase 0 — scaffold and contract. The contract (`SPEC.md`), toolchain, and the
sanitization guard are in place; the reference implementation and conformance
suite land in the phases that follow.

## Develop

```bash
uv sync                 # install into .venv
uv run ruff check       # lint
uv run pyright          # type check (strict)
uv run pytest           # tests
bash scripts/reproduce.sh   # full local gate (once the suite lands)
```

## License

MIT. This is a clean-room, general-purpose distillation of coordination-ledger
patterns; it contains no private data and depends on no external store.
