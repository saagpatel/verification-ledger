# TLA+ model of the Verification Ledger

`VerificationLedger.tla` models the reference store in `src/verification_ledger`
as a state machine: records with provenance labels, the promotion gate, the
retention pass, the durable receipts (each with a content digest and a link
in a hash chain, plus the chain's length and head in the store metadata) and
the health check that re-verifies them, several concurrent in-band agents, one
out-of-band operator, and an adversary with store-level access. Digests and
hashes are modeled as ideal (injective): a digest is the pair of record id and
content version, and a link is the whole sequence of digests up to it. TLC checks
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
| `VerificationLedger` | all real mechanisms; adversary may delete, demote or overwrite a row (receipts untouched) | no violation |
| `Control_GateDecisionOnly` | `evaluate_activation` allows everything; the SQL guard stays | no violation (the guard is a real second line) |
| `Control_GateSkipsCheck` | no trust check and no SQL guard on activation | `VL2_ActionableIsOperator` violated |
| `Control_NoClamp` | in-band operator write stored as operator | `VL1_Clamp` violated |
| `Control_PromotionIgnoresChannel` | in-band promotion allowed | `VL1_OperatorOnlyOutOfBand` violated |
| `Control_PruneSelectionOnly` | `select_prunable` targets durable rows; the `durable = 0` DELETE backstop stays | no violation (the backstop is a real second line) |
| `Control_PruneIgnoresDurable` | selection and DELETE both ignore `durable` | `VL4_DurableKept` violated |
| `Finding_ReceiptErased` | adversary deletes a durable receipt, with or without its row, anywhere in the chain | no violation (was a violation before the hash chain) |
| `Control_HealthIgnoresDigest` | health skips the digest comparison; adversary may overwrite | `VL4_LossDetected` violated (an overwrite goes unseen) |
| `Control_HealthIgnoresChainMeta` | health skips the length and head check; adversary may erase receipts | `VL4_LossDetected` violated (erasing the newest receipt leaves every link valid) |
| `Control_ConsistentChainRewrite` | adversary overwrites or erases, then recomputes every digest, link and the metadata | `VL4_LossDetected` violated: the documented out-of-scope case, undetectable without an anchor outside the store |
| `Check_MetaErased` | adversary may erase receipts and delete the chain metadata (row deletion, demotion and overwrite are left to `VerificationLedger`, to keep the state space tractable); writes never recreate metadata | no violation |
| `Control_WriteRecreatesMeta` | as `Check_MetaErased`, but a durable write recreates missing metadata (the round 2 review bug) | `VL4_LossDetected` violated: erase the only durable record and its receipt, delete the metadata, write a new durable record, and the chain verifies |
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
| `VL4_LossDetected` | invariant | any durable record that is gone, demoted or overwritten makes `health()` report it, including when its receipt was removed too (short of a consistent whole-chain rewrite) |
| `VL4_NoFalseAlarm` | invariant | with no tampering, every interleaving leaves `health()` ok, so writes keep the chain and metadata consistent |

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
3. **Two statements, one transaction.** `activate()` is a SELECT plus a
   guarded UPDATE and `prune()` a SELECT plus a guarded DELETE. Since the round
   2 review both run inside one transaction holding the write lock, so no other
   process can interleave; the model still lets other actors move between the
   two steps, which over-approximates the code and keeps every safety result
   sound. `promote()` (SELECT plus UPDATE, also under the lock) is one step.
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
8. **Detection evidence lives in the same store.** Receipts carry digests and
   hash-chain links and the metadata keeps the chain's length and head, so an
   overwrite or a removed receipt (even the newest) is reported. A writer who
   recomputes the whole chain and metadata consistently is not;
   `Control_ConsistentChainRewrite` shows that trace. SPEC.md states this scope.
9. **The durable write is one step.** `write()` runs in one transaction that
   holds the write lock: it inserts the row, reads the chain head and appends
   the receipt, so no other writer can slip in between; it is modeled as one
   atomic step. If the chain metadata is missing it links from genesis and
   leaves the metadata missing.
10. **Health reads one snapshot.** `health()` reads its counts, receipts,
    records and metadata inside one read transaction; the model evaluates
    health as a predicate over one state, which is exactly that. Before the
    round 2 fix the reads were separate and a concurrent durable write could
    raise a false alarm (now a deterministic regression test).
11. **Migration is not modeled.** Opening a pre-chain store migrates it once,
    on the schema transition only; that path, and the rule that reopening
    never repairs evidence, are covered by the property tests, not by TLC.

## Results

TLC 2.x, 6 workers, breadth-first, on the bounds above. "Generated" and
"distinct" are TLC's state counts and "depth" is the search depth TLC reports
(multi-worker BFS depth can vary by one between runs). For a violation,
"trace" is the number of states in TLC's counterexample.

| Config | Result | Generated | Distinct | Depth | Trace |
|---|---|---:|---:|---:|---:|
| `VerificationLedger` | no violation | 565,526,780 | 40,783,771 | 24 | |
| `Control_GateDecisionOnly` | no violation | 915,554,994 | 66,738,595 | 24 | |
| `Control_PruneSelectionOnly` | no violation | 804,736,992 | 61,286,731 | 25 | |
| `Finding_ReceiptErased` | no violation | 2,866,764,108 | 200,875,919 | 27 | |
| `Control_GateSkipsCheck` | `VL2_ActionableIsOperator` violated | 1,089 | 258 | 6 | 6 |
| `Control_NoClamp` | `VL1_Clamp` violated | 2 | 2 | 2 | 2 |
| `Control_PromotionIgnoresChannel` | `VL1_OperatorOnlyOutOfBand` violated | 185 | 56 | 5 | 5 |
| `Control_PruneIgnoresDurable` | `VL4_DurableKept` violated | 5,975 | 1,282 | 6 | 6 |
| `Control_HealthIgnoresDigest` | `VL4_LossDetected` violated | 1,051 | 247 | 6 | 5 |
| `Control_HealthIgnoresChainMeta` | `VL4_LossDetected` violated | 1,053 | 242 | 5 | 5 |
| `Control_ConsistentChainRewrite` | `VL4_LossDetected` violated (out of scope, documented) | 934 | 198 | 5 | 3 |
| `Witness_FastPath` | `NoRecordEverActionable` violated (expected) | 13,506 | 2,894 | 7 | 7 |

Traces, read from the logs:

- `Control_GateSkipsCheck`: three writes; an agent's activation SELECT allows
  an agent record; the UPDATE sets `actionable` on it.
- `Control_HealthIgnoresDigest`: three writes; the adversary overwrites a
  durable payload; with the digest check off, health stays ok.
- `Control_HealthIgnoresChainMeta`: three writes; the adversary erases the
  newest durable receipt (with its row); every remaining link is valid, so only
  the length and head check would have caught it.
- `Control_ConsistentChainRewrite`: one durable write; the adversary overwrites
  it and recomputes its digest, every link and the metadata; health stays ok.
  This is the case SPEC.md puts out of scope.
