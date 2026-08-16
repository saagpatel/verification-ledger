# Adopters: grading a real store

The conformance suite grades any store that implements the `LedgerAdapter`
protocol (`verification_ledger.conformance.contract`). This note walks one real,
independent store — [bridge-db](https://github.com/saagpatel/bridge-db), a
SQLite coordination bridge for a multi-agent fleet — through it, because
bridge-db is *stricter* than the bundled reference and so shows exactly what the
two implementation-agnostic hooks (`seed_operator`, `prune_cap`) are for.

## Why an adapter is more than a rename

The reference store and bridge-db enforce the same four invariants through
different machinery:

| Invariant | Reference mechanism | bridge-db mechanism |
|---|---|---|
| VL-1 provenance typing | `write()` clamps an in-band `operator` request to `agent` | `create_handoff` clamps an in-band `operator` request to `agent` and records the clamp |
| VL-2 promotion gate | non-operator record refuses activation in-band; `promote()` mints `operator` **only** out-of-band | `pick_up_handoff` refuses a non-operator handoff; operator trust is minted **only** by the out-of-band promotion ceremony (there is no in-band operator write at all) |
| VL-3 envelope | every `read()` wraps content in the instruction-boundary envelope | `get_pending_handoffs` returns each row under an `instruction_boundary` envelope |
| VL-4 retention | durable-tagged rows are prune-exempt; loss is health-detectable | `LEDGER`/`SHIPPED`-tagged rows are prune-exempt; untagged rows are capped at 50 per source |

Two differences are exactly the ones the protocol is built to absorb.

### `seed_operator` — the store's own operator ceremony

The reference supports a direct out-of-band operator write, so its
`seed_operator` is one call:

```python
def seed_operator(self, payload: str) -> int:
    return self._led.write(
        payload, source_trust=Trust.OPERATOR, channel=Channel.OUT_OF_BAND
    ).record_id
```

bridge-db is stricter: **there is no operator write, in-band or out.** Operator
trust exists only as the result of the out-of-band promotion ceremony applied to
an existing record. Its `seed_operator` therefore writes an `agent` record and
then runs that ceremony:

```python
def seed_operator(self, payload: str) -> int:
    rid = self._create_handoff(payload)          # stored as agent (untrusted)
    self._promote_out_of_band(rid)               # the operator-only ceremony
    return rid
```

Both are legitimate out-of-band paths to an operator record. Because the VL-2
*adversarial* probes independently prove the in-band path to `operator` stays
closed, letting each adapter declare its own seed here does not weaken the score
— it only stops the *positive* probe from hard-coding the reference's mechanics.

### `prune_cap` — the store's own retention cap

The reference takes an arbitrary cap per `prune()` call; the adapter picks a
small one and declares it. bridge-db's cap is fixed at 50 per source and pruning
happens automatically on every write, so its adapter declares 50 and makes
`prune()` a no-op:

```python
def prune_cap(self) -> int:
    return 50          # bridge-db keeps the newest 50 untagged rows per source

def prune(self) -> None:
    pass               # bridge-db auto-prunes on write; nothing to trigger
```

The VL-4 probes read `prune_cap()`, overflow it, and expect exactly `cap`
non-durable records to survive alongside the durable ones — so a fixed-cap
auto-pruning store and a configurable-cap explicit-prune store are graded the
same way.

## Result

Driven in-process against a throwaway database (the live store and its logs are
never touched), bridge-db passes all four invariants: the in-band `operator`
clamp holds, non-operator handoffs cannot be claimed in-band while the
out-of-band promotion opens the legitimate path, every read carries the
boundary envelope, and a `LEDGER`-tagged row survives an aggressive prune that
caps untagged rows at 50 per source.

The adapter that drives bridge-db is not shipped in this repository: it imports
bridge-db's package to reach those tools, and this project depends on no external
store by design. The point of this note is the *shape* — an adapter is a thin,
honest translation from these seven-or-so calls to a store's real surface, and a
store that enforces the invariants through stricter machinery than the reference
still scores 1.00.
