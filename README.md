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

The argument, in full: [docs/methodology.md](docs/methodology.md). The precise
contract: [SPEC.md](SPEC.md).

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
  durable record is a detectable violation, never silent.

## Install

```bash
pip install verification-ledger          # zero runtime dependencies
pip install 'verification-ledger[mcp]'   # + the optional MCP stdio server
```

## Quickstart

```python
from verification_ledger.ledger import Ledger
from verification_ledger.model import Channel, Trust

led = Ledger("fleet.db")

# An in-band agent writes untrusted content. It cannot mint operator trust…
r = led.write("proposed action", source_trust=Trust.OPERATOR)  # in-band
assert r.clamped and r.source_trust is Trust.AGENT             # …clamped to agent

# …and it cannot become actionable intent in-band.
assert led.activate(r.record_id).allowed is False

# Only the out-of-band operator ceremony can promote it (a model can't reach this).
led.promote(r.record_id, channel=Channel.OUT_OF_BAND)
assert led.activate(r.record_id).allowed is True

# Every read is wrapped in the instruction-boundary envelope.
led.read(r.record_id).envelope  # {"kind": "stored_data_not_instructions", ...}
```

The out-of-band promotion is a separate operator-only process:

```bash
verification-ledger promote fleet.db 1     # mints operator trust on one record
verification-ledger demo                   # watch the guarantees on synthetic data
verification-ledger serve fleet.db         # in-band MCP stdio server (mcp extra)
```

## The conformance suite

The guarantees ship with a test anyone can run. Any store implements a small
adapter and is graded, per invariant, with **positive probes** (the legitimate
path works) and **adversarial probes** (the attack is blocked). An invariant
passes only if both are perfect, so the score is bidirectional — a
block-everything store fails the positives, an allow-everything store fails the
adversarials.

```bash
python -m verification_ledger.conformance          # grade the reference store
# Ledger Conformance
#   VL-1 PASS   VL-2 PASS   VL-3 PASS   VL-4 PASS
#   Score 1.00  (4/4 invariants)
```

Grade your own store: `python -m verification_ledger.conformance yourmodule:make`,
where `make()` returns a `LedgerAdapter`. The suite was adversarially broken and
hardened before v1 — see the methodology essay.

## Develop

```bash
uv sync
bash scripts/reproduce.sh   # lint + type-check + tests + conformance (all green)
```

## License

MIT. This is a clean-room, general-purpose distillation of coordination-ledger
patterns; it contains no private data and depends on no external store.
