------------------------- MODULE VerificationLedger -------------------------
(***************************************************************************)
(* A model of the reference Verification Ledger (src/verification_ledger), *)
(* written to follow the code, not SPEC.md. Every place the two differ is *)
(* listed in specs/tla/README.md.                                          *)
(*                                                                         *)
(* Records live in slots 1..MaxRecords, allocated in order and never      *)
(* reused (SQLite AUTOINCREMENT). Several in-band agents act concurrently:*)
(* activation and pruning are each two SQL statements in the code (a      *)
(* SELECT, then a guarded UPDATE or DELETE), so they are two steps here,  *)
(* and other actors may interleave between them. One out-of-band operator *)
(* writes operator records and runs the promotion CLI.                    *)
(*                                                                         *)
(* Mechanism flags let the same module describe the real ledger (all     *)
(* TRUE, adversary flags FALSE or as stated) and broken variants used as  *)
(* controls (one flag FALSE): TLC must find a violation for each control. *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets, Sequences, TLC

CONSTANTS
    Agents,                 \* in-band actors (models behind the MCP tools)
    MaxRecords,             \* record slots
    MaxKeep,                \* largest keep an agent passes to vl_prune
    \* mechanisms of the real code (TRUE in the faithful model)
    ClampOn,                \* ledger.write clamps in-band operator to agent
    GateChecksTrust,        \* gate.evaluate_activation is trust-only
    ActivationSqlGuard,     \* activate's UPDATE ... AND source_trust = 'operator'
    PromotionChecksChannel, \* gate.evaluate_promotion refuses IN_BAND
    PruneRespectsDurable,   \* retention.select_prunable skips durable rows
    PruneSqlBackstop,       \* prune's DELETE ... AND durable = 0
    HealthChecksDigest,     \* health() compares content to the receipt digest
    HealthChecksChain,      \* health() re-verifies every receipt link
    HealthChecksMeta,       \* health() compares chain length and head to ledger_meta
    \* adversary with store-level access (outside the API)
    StoreTamper,            \* may delete, demote or overwrite a row, leaving receipts alone
    ReceiptTamper,          \* may delete a durable receipt, with or without its row
    ChainRewrite            \* may overwrite or erase and then rewrite the chain consistently

Ids     == 1..MaxRecords
Trusts  == {"operator", "agent", "ingested"}
Request == Trusts \cup {"default"}     \* "default": no label passed
Idle    == [kind |-> "idle"]

\* The ghost record of the last action; only writes carry request details.
Act(k, i) == [kind |-> k, id |-> i]

VARIABLES
    nextId,      \* next AUTOINCREMENT id
    present,     \* row exists in `records`
    trust,       \* source_trust column ("none" for an unallocated slot)
    durable,     \* durable column
    actionable,  \* actionable column
    receipts,    \* durable_receipts table (the set of record ids with a receipt)
    content,     \* payload version: 0 as written, 1 after an overwrite
    rdigest,     \* receipt digest: <<id, content>> (an ideal, injective digest)
    rprev,       \* receipt prev_hash
    rhash,       \* receipt receipt_hash: an ideal hash, the sequence of digests so far
    metaCount,   \* ledger_meta durable_count
    metaHead,    \* ledger_meta durable_head
    pc,          \* per-agent pending second statement
    \* ghost state, for stating the invariants
    mintedOOB,   \* operator trust on this record was minted out-of-band
    wroteDurable,\* written with durable = 1
    tampered,    \* touched by the store-level adversary
    last         \* the last action, for per-step invariants

integ == <<content, rdigest, rprev, rhash, metaCount, metaHead>>
data == <<nextId, present, trust, durable, actionable, receipts, integ>>
vars == <<data, pc, mintedOOB, wroteDurable, tampered, last>>

Allocated == {i \in Ids : i < nextId}
Live      == {i \in Allocated : present[i]}

\* The ideal hash: a link is the whole sequence of digests up to it, so two
\* links are equal only if every digest before them is (no collisions).
Genesis == <<>>
Link(prev, d) == Append(prev, d)
Max(S) == CHOOSE m \in S : \A k \in S : k <= m
RECURSIVE ChainOf(_, _)
ChainOf(S, dig) == IF S = {} THEN Genesis
                   ELSE LET m == Max(S) IN Link(ChainOf(S \ {m}, dig), dig[m])

\* health(), as integrity.verify computes it.
MissingRecords  == {r \in receipts : ~(present[r] /\ durable[r])}
MissingReceipts == {i \in Live : durable[i] /\ i \notin receipts}
DigestOk(r) == (present[r] /\ durable[r]) => rdigest[r] = <<r, content[r]>>
ExpectedPrev(r) == LET before == {k \in receipts : k < r}
                   IN IF before = {} THEN Genesis ELSE rhash[Max(before)]
LinkOk(r) == rprev[r] = ExpectedPrev(r) /\ rhash[r] = Link(rprev[r], rdigest[r])
ChainHead == IF receipts = {} THEN Genesis ELSE rhash[Max(receipts)]
MetaOk == metaCount = Cardinality(receipts) /\ metaHead = ChainHead
HealthOk ==
    /\ MissingRecords = {}
    /\ MissingReceipts = {}
    /\ HealthChecksDigest => \A r \in receipts : DigestOk(r)
    /\ HealthChecksChain => \A r \in receipts : LinkOk(r)
    /\ HealthChecksMeta => MetaOk

\* select_prunable: among the candidates, everything past the newest k.
PastNewest(S, k) == {i \in S : Cardinality({j \in S : j > i}) >= k}

Stored(t, inBand) ==
    IF t = "default" THEN "agent"
    ELSE IF t = "operator" /\ inBand /\ ClampOn THEN "agent"
    ELSE t

TypeOK ==
    /\ nextId \in 1..(MaxRecords + 1)
    /\ present \in [Ids -> BOOLEAN]
    /\ trust \in [Ids -> Trusts \cup {"none"}]
    /\ durable \in [Ids -> BOOLEAN]
    /\ actionable \in [Ids -> BOOLEAN]
    /\ receipts \subseteq Ids
    /\ content \in [Ids -> 0..1]
    /\ metaCount \in 0..MaxRecords
    /\ mintedOOB \in [Ids -> BOOLEAN]
    /\ wroteDurable \in [Ids -> BOOLEAN]
    /\ tampered \subseteq Ids

Init ==
    /\ nextId = 1
    /\ present = [i \in Ids |-> FALSE]
    /\ trust = [i \in Ids |-> "none"]
    /\ durable = [i \in Ids |-> FALSE]
    /\ actionable = [i \in Ids |-> FALSE]
    /\ receipts = {}
    /\ content = [i \in Ids |-> 0]
    /\ rdigest = [i \in Ids |-> Genesis]
    /\ rprev = [i \in Ids |-> Genesis]
    /\ rhash = [i \in Ids |-> Genesis]
    /\ metaCount = 0
    /\ metaHead = Genesis
    /\ pc = [a \in Agents |-> Idle]
    /\ mintedOOB = [i \in Ids |-> FALSE]
    /\ wroteDurable = [i \in Ids |-> FALSE]
    /\ tampered = {}
    /\ last = Act("init", 0)

\* ledger.write: one INSERT, plus for a durable record the receipt (digest and
\* link over the current head) and the metadata update, in the same transaction.
\* The INSERT takes the write lock before the head is read, so it is one step.
DoWrite(t, inBand, d) ==
    LET id == nextId
        s  == Stored(t, inBand)
        dg == <<id, 0>>
        ln == Link(metaHead, dg)
    IN
    /\ nextId <= MaxRecords
    /\ nextId' = nextId + 1
    /\ present' = [present EXCEPT ![id] = TRUE]
    /\ trust' = [trust EXCEPT ![id] = s]
    /\ durable' = [durable EXCEPT ![id] = d]
    /\ actionable' = [actionable EXCEPT ![id] = FALSE]
    /\ receipts' = IF d THEN receipts \cup {id} ELSE receipts
    /\ rdigest' = IF d THEN [rdigest EXCEPT ![id] = dg] ELSE rdigest
    /\ rprev' = IF d THEN [rprev EXCEPT ![id] = metaHead] ELSE rprev
    /\ rhash' = IF d THEN [rhash EXCEPT ![id] = ln] ELSE rhash
    /\ metaCount' = IF d THEN metaCount + 1 ELSE metaCount
    /\ metaHead' = IF d THEN ln ELSE metaHead
    /\ mintedOOB' = [mintedOOB EXCEPT ![id] = (s = "operator" /\ ~inBand)]
    /\ wroteDurable' = [wroteDurable EXCEPT ![id] = d]
    /\ last' = [kind |-> "write", id |-> id, req |-> t, inBand |-> inBand,
                clamped |-> (t = "operator" /\ inBand /\ ClampOn)]
    /\ UNCHANGED <<pc, tampered, content>>

\* An agent writes in-band (vl_write, or the library with the default channel).
AgentWrite(a, t, d) == DoWrite(t, TRUE, d)

\* The operator writes out-of-band (seed_operator / a direct operator write).
OperatorWrite(t, d) == t # "default" /\ DoWrite(t, FALSE, d)

\* ledger.activate, statement 1: SELECT and the pure gate decision.
\* A refusal returns before any write: nothing is mutated.
ActivateSelect(a, i) ==
    /\ pc[a] = Idle
    /\ i \in Live
    /\ LET allowed == IF GateChecksTrust THEN trust[i] = "operator" ELSE TRUE
       IN IF allowed
          THEN /\ pc' = [pc EXCEPT ![a] = [kind |-> "activate", id |-> i]]
               /\ last' = Act("activate_select", i)
          ELSE /\ last' = Act("refused_activate", i)
               /\ UNCHANGED pc
    /\ UNCHANGED <<data, mintedOOB, wroteDurable, tampered>>

\* ledger.activate, statement 2: the guarded UPDATE.
ActivateUpdate(a) ==
    /\ pc[a].kind = "activate"
    /\ LET i == pc[a].id
           hit == present[i] /\ (~ActivationSqlGuard \/ trust[i] = "operator")
       IN /\ actionable' = IF hit THEN [actionable EXCEPT ![i] = TRUE] ELSE actionable
          /\ last' = Act("activate_update", i)
    /\ pc' = [pc EXCEPT ![a] = Idle]
    /\ UNCHANGED <<nextId, present, trust, durable, receipts, integ, mintedOOB, wroteDurable, tampered>>

\* ledger.promote called in-band (the library default channel). The MCP surface
\* has no promotion tool at all; the library refuses this channel.
AgentPromote(a, i) ==
    /\ i \in Live
    /\ IF PromotionChecksChannel
       THEN /\ last' = Act("refused_promote", i)
            /\ UNCHANGED trust
       ELSE /\ trust' = [trust EXCEPT ![i] = "operator"]
            /\ last' = Act("promote", i)
    /\ UNCHANGED <<nextId, present, durable, actionable, receipts, integ, pc, mintedOOB,
                   wroteDurable, tampered>>

\* The out-of-band promotion CLI on one exact record.
OperatorPromote(i) ==
    /\ i \in Live
    /\ trust' = [trust EXCEPT ![i] = "operator"]
    /\ mintedOOB' = [mintedOOB EXCEPT ![i] = TRUE]
    /\ last' = Act("op_promote", i)
    /\ UNCHANGED <<nextId, present, durable, actionable, receipts, integ, pc, wroteDurable, tampered>>

\* ledger.prune, statement 1: SELECT ids and select_prunable(keep).
PruneSelect(a, k) ==
    /\ pc[a] = Idle
    /\ LET cands == IF PruneRespectsDurable THEN {i \in Live : ~durable[i]} ELSE Live
       IN pc' = [pc EXCEPT ![a] = [kind |-> "prune", targets |-> PastNewest(cands, k)]]
    /\ last' = Act("prune_select", 0)
    /\ UNCHANGED <<data, mintedOOB, wroteDurable, tampered>>

\* ledger.prune, statement 2: DELETE ... WHERE id IN (...) AND durable = 0.
PruneDelete(a) ==
    /\ pc[a].kind = "prune"
    /\ LET gone == {i \in pc[a].targets : present[i] /\ (~PruneSqlBackstop \/ ~durable[i])}
       IN present' = [i \in Ids |-> IF i \in gone THEN FALSE ELSE present[i]]
    /\ pc' = [pc EXCEPT ![a] = Idle]
    /\ last' = Act("prune_delete", 0)
    /\ UNCHANGED <<nextId, trust, durable, actionable, receipts, integ, mintedOOB, wroteDurable, tampered>>

\* Store-level adversary: delete, demote or overwrite a row; receipts stay.
TamperDelete(i) ==
    /\ StoreTamper /\ i \in Live /\ durable[i]
    /\ present' = [present EXCEPT ![i] = FALSE]
    /\ tampered' = tampered \cup {i}
    /\ last' = Act("tamper_delete", i)
    /\ UNCHANGED <<nextId, trust, durable, actionable, receipts, integ, pc, mintedOOB, wroteDurable>>

TamperDemote(i) ==
    /\ StoreTamper /\ i \in Live /\ durable[i]
    /\ durable' = [durable EXCEPT ![i] = FALSE]
    /\ tampered' = tampered \cup {i}
    /\ last' = Act("tamper_demote", i)
    /\ UNCHANGED <<nextId, present, trust, actionable, receipts, integ, pc, mintedOOB, wroteDurable>>

TamperOverwrite(i) ==
    /\ StoreTamper /\ i \in Live /\ content[i] = 0
    /\ content' = [content EXCEPT ![i] = 1]
    /\ tampered' = tampered \cup {i}
    /\ last' = Act("tamper_overwrite", i)
    /\ UNCHANGED <<nextId, present, trust, durable, actionable, receipts, rdigest, rprev,
                   rhash, metaCount, metaHead, pc, mintedOOB, wroteDurable>>

\* Store-level adversary that also removes the evidence: a durable receipt,
\* with or without its row, anywhere in the chain.
TamperErase(i, withRow) ==
    /\ ReceiptTamper /\ i \in Live /\ durable[i] /\ i \in receipts
    /\ present' = IF withRow THEN [present EXCEPT ![i] = FALSE] ELSE present
    /\ receipts' = receipts \ {i}
    /\ tampered' = tampered \cup {i}
    /\ last' = Act("tamper_erase", i)
    /\ UNCHANGED <<nextId, trust, durable, actionable, integ, pc, mintedOOB, wroteDurable>>

\* Out of scope (no anchor outside the store): change a durable record, then
\* recompute every digest, link and the metadata so the chain verifies.
Rechain(R, dig) ==
    /\ rdigest' = dig
    /\ rprev' = [j \in Ids |-> IF j \in R THEN ChainOf({k \in R : k < j}, dig) ELSE rprev[j]]
    /\ rhash' = [j \in Ids |-> IF j \in R THEN ChainOf({k \in R : k <= j}, dig) ELSE rhash[j]]
    /\ metaCount' = Cardinality(R)
    /\ metaHead' = ChainOf(R, dig)

RewriteOverwrite(i) ==
    /\ ChainRewrite /\ i \in Live /\ durable[i] /\ i \in receipts /\ content[i] = 0
    /\ content' = [content EXCEPT ![i] = 1]
    /\ Rechain(receipts, [rdigest EXCEPT ![i] = <<i, 1>>])
    /\ tampered' = tampered \cup {i}
    /\ last' = Act("rewrite_overwrite", i)
    /\ UNCHANGED <<nextId, present, trust, durable, actionable, receipts, pc, mintedOOB, wroteDurable>>

RewriteErase(i) ==
    /\ ChainRewrite /\ i \in Live /\ durable[i] /\ i \in receipts
    /\ present' = [present EXCEPT ![i] = FALSE]
    /\ receipts' = receipts \ {i}
    /\ Rechain(receipts \ {i}, rdigest)
    /\ tampered' = tampered \cup {i}
    /\ last' = Act("rewrite_erase", i)
    /\ UNCHANGED <<nextId, trust, durable, actionable, content, pc, mintedOOB, wroteDurable>>

Next ==
    \/ \E a \in Agents, t \in Request, d \in BOOLEAN : AgentWrite(a, t, d)
    \/ \E t \in Trusts, d \in BOOLEAN : OperatorWrite(t, d)
    \/ \E a \in Agents, i \in Ids : ActivateSelect(a, i) \/ AgentPromote(a, i)
    \/ \E a \in Agents : ActivateUpdate(a) \/ PruneDelete(a)
    \/ \E a \in Agents, k \in 0..MaxKeep : PruneSelect(a, k)
    \/ \E i \in Ids : \/ OperatorPromote(i) \/ TamperDelete(i) \/ TamperDemote(i)
                     \/ TamperOverwrite(i) \/ RewriteOverwrite(i) \/ RewriteErase(i)
    \/ \E i \in Ids, w \in BOOLEAN : TamperErase(i, w)

Spec == Init /\ [][Next]_vars

\* Agents are interchangeable: no invariant or property names one.
AgentSymmetry == Permutations(Agents)

-----------------------------------------------------------------------------
(* VL-1 provenance typing *)

\* Every live record carries a label in {operator, agent, ingested}.
VL1_Typed == \A i \in Live : trust[i] \in Trusts

\* No label means agent; an in-band operator request is stored as agent and the
\* clamp is reported; agent and ingested are stored as-is.
VL1_Clamp ==
    last.kind = "write" =>
        /\ last.req = "default" => trust[last.id] = "agent"
        /\ (last.req = "operator" /\ last.inBand) => (trust[last.id] = "agent" /\ last.clamped)
        /\ last.req \in {"agent", "ingested"} => trust[last.id] = last.req

\* Operator trust exists only where the out-of-band channel minted it.
VL1_OperatorOnlyOutOfBand == \A i \in Live : trust[i] = "operator" => mintedOOB[i]

(* VL-2 promotion gate *)

\* An actionable record is operator trust minted out-of-band.
VL2_ActionableIsOperator ==
    \A i \in Live : actionable[i] => (trust[i] = "operator" /\ mintedOOB[i])

\* A record is never born actionable.
VL2_NotBornActionable == last.kind = "write" => ~actionable[last.id]

\* A refused activation or promotion mutates nothing.
VL2_RefusalMutatesNothing ==
    [][last'.kind \in {"refused_activate", "refused_promote"} => UNCHANGED data]_vars

\* The out-of-band ceremony changes one exact record, and only its label.
VL2_PromotionIsExact ==
    [][last'.kind = "op_promote" =>
        /\ UNCHANGED <<nextId, present, durable, actionable, receipts, integ>>
        /\ \A j \in Ids : j # last'.id => trust'[j] = trust[j]]_vars

\* The fast path: a guarded UPDATE on a live operator record sets actionable.
VL2_FastPathOpen ==
    [][(last'.kind = "activate_update" /\ present[last'.id] /\ trust[last'.id] = "operator")
        => actionable'[last'.id]]_vars

(* VL-4 retention invariant *)

\* No API action removes or demotes a durable record.
VL4_DurableKept ==
    \A i \in Allocated : (wroteDurable[i] /\ i \notin tampered) => (present[i] /\ durable[i])

\* A durable record is intact if it is present, still durable, and unchanged.
Intact(i) == present[i] /\ durable[i] /\ content[i] = 0

\* Any durable record that is gone, demoted or overwritten makes health() report
\* it, whether or not its receipt was removed too (up to a consistent rewrite of
\* the whole chain, which needs an anchor outside the store).
VL4_LossDetected ==
    \A i \in Allocated : (wroteDurable[i] /\ ~Intact(i)) => ~HealthOk

\* No false alarm: with no tampering, every interleaving of writes, prunes,
\* promotions and activations leaves health() ok (the chain is maintained).
VL4_NoFalseAlarm == tampered = {} => HealthOk

\* Positive control: an operator record can become actionable. Checked as an
\* invariant that TLC is expected to VIOLATE, which exhibits a witness trace.
NoRecordEverActionable == \A i \in Live : ~actionable[i]
=============================================================================
