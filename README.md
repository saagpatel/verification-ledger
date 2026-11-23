# verification-ledger

A governed coordination ledger for local multi-agent fleets, and an executable
conformance suite that tests whether a store enforces its guarantees.

Agent fleets can share state through a plain file or an ungoverned store. That
lets one agent's output silently become the next agent's "operator intent"
(laundering), and lets ordinary housekeeping silently drop a durable record
(loss). A README asking everyone to be careful is a word-level guarantee. This
project makes the guarantees *mechanisms* enforced at the in-band MCP surface.
It ships a test suite that fails every store in a documented family of broken
stores on exactly the guarantee it breaks and passes the reference store.
No finite suite proves an arbitrary store sound; see
SPEC.md, "What the suite establishes".

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
- **VL-4 Retention invariant** — pruning skips durable records. The health check
  detects a missing or demoted durable record, a change to its payload or
  creation time, and a removed receipt. Receipts are hash-chained. A
  consistent rewrite of the whole chain inside the store needs an outside anchor
  to catch.

## Install

Run these commands from the clone root in a Python 3.12+ virtual environment
with pip.

```bash
pip install .           # zero runtime dependencies
pip install '.[mcp]'    # + the optional MCP stdio server
pip install '.[crash]'  # optional Memory Crash Test dependencies
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

# This trusted Python example also plays the operator's role.
led.promote(r.record_id, channel=Channel.OUT_OF_BAND)
assert led.activate(r.record_id).allowed is True

# Every read is wrapped in the instruction-boundary envelope.
led.read(r.record_id).envelope  # {"kind": "stored_data_not_instructions", ...}
```

In a fleet, give agents only the in-band MCP tools. The Python API trusts its
caller to select the channel. Keep direct database access and the promotion
CLI under operator control.

```bash
verification-ledger promote fleet.db 1     # mints operator trust on one record
verification-ledger --version              # prints the package version
verification-ledger demo                   # watch the guarantees on synthetic data
verification-ledger serve fleet.db         # in-band MCP stdio server (mcp extra)
```

## The conformance suite

The guarantees ship with a test anyone can run. The suite grades a store
through a small adapter, per invariant, with **positive probes** (the legitimate
path works) and **adversarial probes** (the attack is blocked). An invariant
passes only if both are perfect, so the score is bidirectional — a
block-everything store fails the positives, an allow-everything store fails the
adversarials.

```bash
python -m verification_ledger.conformance          # reference store (stdout shown below)
# Ledger Conformance
# ==============================================
#   VL-1  PASS   positive 3/3   adversarial 3/3
#   VL-2  PASS   positive 3/3   adversarial 3/3
#   VL-3  PASS   positive 3/3   adversarial 3/3
#   VL-4  PASS   positive 3/3   adversarial 3/3
# ----------------------------------------------
#   Score 1.00  (4/4 invariants)
```

Grade your own store: `python -m verification_ledger.conformance yourmodule:make`,
replacing `yourmodule:make` with an importable factory that returns a fresh
`LedgerAdapter`. Add `--json` for a machine-readable report. The methodology
essay describes the store that passed the original probes while breaking VL-2.

## Develop

The development checks use uv, Bash and Python 3.12.

```bash
uv sync
bash scripts/reproduce.sh   # lint + type-check + tests + conformance (all green)
```

## License

MIT. This is a clean-room, general-purpose distillation of coordination-ledger
patterns. The reference ledger uses SQLite and the demo runs on synthetic data.
