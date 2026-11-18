# TLA+ model of the Verification Ledger

`VerificationLedger.tla` models the reference store in `src/verification_ledger`
as a state machine: records with provenance labels, the promotion gate, the
retention pass and its health check, several concurrent in-band agents, one
out-of-band operator, and an adversary with store-level access. TLC checks
VL-1, VL-2 and VL-4 exhaustively on a bounded model. VL-3 (the read envelope) is
a pure function of a record's label and is covered by the property tests, not
here.

The model follows the **code**, not SPEC.md. Where the two differ, the code
won; every difference is listed below.

## Run it

```bash
TLA2TOOLS=/path/to/tla2tools.jar specs/tla/run.sh            # every configuration
TLA2TOOLS=/path/to/tla2tools.jar specs/tla/run.sh VerificationLedger
```

`JAVA` overrides the java binary and `TLC_WORKERS` the worker count. Full TLC
logs land in `specs/tla/out/` (ignored by git). The script exits non-zero if
any configuration's result differs from its expectation.

## Bounds

Every configuration uses the same bounds: **3 record slots, 3 in-band agents
(symmetric), keep in {0, 1}**, plus the operator and the store-level adversary.
Each agent may hold one pending second statement (activation's guarded UPDATE
or prune's DELETE) while every other actor moves, so the interleavings between
the two statements of `activate()` and `prune()` are all explored.

## Configurations

| Config | Mechanisms | Expected |
|---|---|---|
| `VerificationLedger` | all real mechanisms; adversary may delete or demote a durable row (receipt kept) | no violation |
| `Control_GateDecisionOnly` | `evaluate_activation` allows everything; the SQL guard stays | no violation (the guard is a real second line) |
| `Control_GateSkipsCheck` | no trust check and no SQL guard on activation | `VL2_ActionableIsOperator` violated |
| `Control_NoClamp` | in-band operator write stored as operator | `VL1_Clamp` violated |
| `Control_PromotionIgnoresChannel` | in-band promotion allowed | `VL1_OperatorOnlyOutOfBand` violated |
| `Control_PruneSelectionOnly` | `select_prunable` targets durable rows; the `durable = 0` DELETE backstop stays | no violation (the backstop is a real second line) |
| `Control_PruneIgnoresDurable` | selection and DELETE both ignore `durable` | `VL4_DurableKept` violated |
| `Finding_ReceiptErased` | adversary deletes a durable row **and** its receipt | `VL4_LossDetected` violated (a real gap, see below) |
| `Witness_FastPath` | all real mechanisms; checks `NoRecordEverActionable` | violated, which exhibits an operator record becoming actionable |

## Invariants and properties

| Name | Kind | Claim |
|---|---|---|
| `VL1_Typed` | invariant | every live record's label is in {operator, agent, ingested} |
| `VL1_Clamp` | invariant | no label means agent; in-band operator is stored as agent and reported clamped; agent and ingested are stored as-is |
| `VL1_OperatorOnlyOutOfBand` | invariant | operator trust exists only where the out-of-band channel minted it |
| `VL2_ActionableIsOperator` | invariant | an actionable record is operator trust minted out-of-band |
| `VL2_NotBornActionable` | invariant | a record is never actionable at write time |
| `VL2_RefusalMutatesNothing` | action property | a refused activation or promotion leaves every table unchanged |
| `VL2_PromotionIsExact` | action property | the out-of-band ceremony changes one exact record's label and nothing else |
| `VL2_FastPathOpen` | action property | the guarded UPDATE on a live operator record sets actionable |
| `VL4_DurableKept` | invariant | no API action removes or demotes a durable record |
| `VL4_LossDetected` | invariant | any durable record that is gone or demoted makes `health()` report it |

## Where the code and SPEC.md differ

1. **The channel is a caller-supplied argument.** SPEC.md calls library calls
   in-band, but `Ledger.write()` and `Ledger.promote()` accept
   `channel=Channel.OUT_OF_BAND` from any caller. The model gives out-of-band
   actions only to the operator actor, which matches the MCP surface (no
   channel parameter, no promotion tool) and the library defaults, not every
   possible library caller.
2. **Activation is trust-only.** `activate()` ignores its `channel` argument
   except in a log line, so a non-operator record is refused on either channel.
   SPEC.md only says it is refused in-band. The model's activation has no
   channel.
3. **Two statements, not one transition.** `activate()` is a SELECT plus a
   guarded UPDATE and `prune()` a SELECT plus a guarded DELETE; another process
   on the same file store (the promotion CLI, a second server) can interleave.
   Modeled as two steps per agent. `promote()` is a SELECT plus an unguarded
   UPDATE; because trust only ever rises it is modeled as one step. One
   reporting-only consequence: if the row is pruned between the two statements,
   `promote()` still returns `promoted=True` for a row that no longer exists.
4. **Two ways to mint operator out-of-band.** SPEC.md VL-2 says promotion to
   operator happens only through the ceremony; the code also accepts a direct
   out-of-band operator write (the reference adapter's `seed_operator`). Both
   are modeled as out-of-band mints.
5. **The clamp is recorded only in the return value and a log line**, never on
   the stored record. The model records it in the ghost `last.clamped`.
6. **The retention cap is per call.** SPEC.md speaks of a configured cap; the
   code takes `keep` on every `prune()` call, and the in-band `vl_prune` tool
   lets any agent pick it, including 0. Modeled as agents choosing keep.
7. **`ingested` is not treated more strictly than `agent`.** SPEC.md calls it
   the strictest label; in the code the two are handled identically (both
   refused, both promotable out-of-band). The model reflects the code.
8. **Loss detection is receipt-based, inside the same store.** A durable row
   removed together with its receipt leaves no orphan, so `health()` stays ok.
   `Finding_ReceiptErased` shows TLC finding exactly that trace.

## Results

TLC 2.x, 4 workers, breadth-first, on the bounds above. "Generated" and
"distinct" are TLC's state counts and "depth" is the search depth TLC reports
(multi-worker BFS reports 21 or 22 for the full model across runs). For a
violation, "trace" is the number of states in TLC's shortest counterexample.

| Config | Result | Generated | Distinct | Depth | Trace |
|---|---|---:|---:|---:|---:|
| `VerificationLedger` | no violation | 60,295,796 | 4,576,089 | 22 | |
| `Control_GateDecisionOnly` | no violation | 97,330,588 | 7,482,311 | 21 | |
| `Control_PruneSelectionOnly` | no violation | 86,873,406 | 6,959,459 | 22 | |
| `Control_GateSkipsCheck` | `VL2_ActionableIsOperator` violated | 2,096 | 443 | 5 | 4 |
| `Control_NoClamp` | `VL1_Clamp` violated | 2 | 2 | 2 | 2 |
| `Control_PromotionIgnoresChannel` | `VL1_OperatorOnlyOutOfBand` violated | 189 | 70 | 5 | 3 |
| `Control_PruneIgnoresDurable` | `VL4_DurableKept` violated | 5,610 | 1,028 | 6 | 4 |
| `Finding_ReceiptErased` | `VL4_LossDetected` violated | 828 | 201 | 5 | 5 |
| `Witness_FastPath` | `NoRecordEverActionable` violated (expected) | 9,658 | 1,783 | 6 | 5 |

Shortest traces, read from the logs:

- `Control_GateSkipsCheck`: an agent writes record 1 (agent); an agent's
  activation SELECT allows it; the UPDATE sets `actionable` on an agent record.
- `Finding_ReceiptErased`: three writes, the third durable (receipt 3); the
  adversary deletes row 3 and receipt 3; `Orphans = {}`, so `health()` is ok
  while a durable record is gone.
