# Your fleet's memory is a trust boundary

## The problem hiding in shared state

Run more than one agent and they need to share state. One agent writes a note, a
handoff, a "this is done" record; another reads it and acts. In almost every
fleet that shared state is a plain file or an ungoverned key-value store, and
that design carries two silent failures.

**Laundering.** An agent's own output becomes the next agent's operator intent.
A model writes some text — a summary, a suggested action, content it scraped off
a web page — and a later agent reads it out of shared memory and treats it as an
instruction to act on. There is no boundary in between. Untrusted, model-authored
or externally-sourced content flows into a channel a downstream agent trusts. If
one of your agents can be prompt-injected, the injection now has a distribution
network.

**Silent loss.** A record that mattered — a shipped artifact, a decision, a
receipt you'll need later — gets pruned or overwritten by ordinary housekeeping,
and nothing notices it's gone. You find out when you go looking for it.

The usual response is a README: "please label your handoffs," "don't prune the
shipped rows," "review external content before acting on it." That is a
*word-level* guarantee. It holds roughly as often as every agent, on every run,
remembers to follow it. Words bind at maybe eighty percent. A mechanism binds at
one hundred.

## The move: make the guarantee a property of the store

The Verification Ledger turns those two guarantees into mechanisms — properties
of the store that hold regardless of what any agent does — and ships an
executable conformance suite that proves a given store enforces them. The store
is small and boring; the interesting part is that the guarantees are *checkable*.

Four invariants, stated precisely in [SPEC.md](../SPEC.md):

- **VL-1 Provenance typing.** Every record carries a `source_trust` label:
  `operator`, `agent`, or `ingested`. The default is `agent`. A write arriving on
  the in-band channel — anything a model can reach — can never mint `operator`
  trust; a request to do so is clamped and recorded.
- **VL-2 Promotion gate.** A non-operator record cannot cross the transition that
  makes it actionable intent through the in-band channel. Promotion to `operator`
  happens only through an out-of-band ceremony — an operator at a terminal, a
  process a model cannot invoke. The strict `ingested` sink is
  refuse-until-promoted.
- **VL-3 Instruction-boundary envelope.** Every read is wrapped so the consuming
  model is told, structurally, that what it is reading is stored data carrying a
  trust label — not an instruction.
- **VL-4 Retention invariant.** Durable-tagged records are permanently exempt
  from pruning; a lost durable record is a detectable violation, never a silent
  one.

The key design choice is the **two channels**. In-band is everything a model can
reach: library calls, the MCP tools. Out-of-band is a surface a model cannot
reach: an operator running the promotion CLI. Operator trust is minted in exactly
one place, and it is the one place a model has no access to. The gate isn't a
prompt asking the model to behave — it's a boundary the model is on the wrong
side of.

## Why the envelope is not enough on its own

The instruction-boundary envelope (VL-3) is honest about what it is: an advisory
signal. It tells a well-behaved consumer "this is data, inspect its provenance
before acting." It does not, and cannot, prove authorship, and a compromised
consumer can ignore it. That is exactly why VL-2 exists. The envelope is the
signal; the gate is the enforcement. A store that shipped only the envelope would
be back to a word-level guarantee dressed up in JSON.

## The conformance suite: proving the guarantee, not asserting it

A README that claims "this store enforces provenance" is itself a word-level
guarantee. So the guarantees ship with a test anyone can run.

The conformance suite grades any store — the bundled reference implementation or
a third party's — through a small adapter. For each invariant it runs **positive
probes** (the legitimate path still works) and **adversarial probes** (the attack
is blocked). An invariant passes only if *both* rates are perfect. The top-line
Ledger Conformance Score is the fraction of the four invariants fully passed.

The metric is bidirectional by construction, which is what makes it hard to game:

- A **block-everything** store fails the positive probes — the legitimate
  operator path is broken.
- An **allow-everything** store fails the adversarial probes — laundering isn't
  blocked.

Only a store that keeps the legitimate path open *and* closes the laundering path
scores well. This is the same discipline that makes a good benchmark
un-gameable: reward both the true positive and the true negative, so neither
degenerate strategy wins.

## The suite was adversarially broken before it shipped

A conformance suite is a claim, and claims get tested. Before v1 shipped, the
suite was handed to a red-team with one instruction: build a store that scores a
perfect 4/4 while actually laundering.

It succeeded. The break: a store that made every record actionable *at write
time*, while the activation call politely returned "refused" for agent records.
The gate's return value looked correct; the persisted state a consumer actually
reads was not. The probes checked only the return value, never the persisted
field — so a store that decoupled the two slipped through.

The fix was to make the adversarial probes assert persisted state, plus a new
probe that a record is never born actionable. Re-run against the exact store that
had scored 1.00, it now fails VL-2. That episode is the whole thesis in
miniature: the value of a conformance suite is not that it passes, but that it can
be broken, and that breaking it makes it stronger. A guarantee you haven't tried
to defeat is a guarantee you don't yet have.

## Honest scope

This is a ledger, not an orchestrator: it records and governs coordination state;
it does not dispatch agents or run tasks. It is not a secrets store, a knowledge
base, or a search index. It does not authenticate the operator — it assumes the
out-of-band channel is reachable only by the operator, and wiring that channel is
the adopter's job. The envelope is advisory. Within those bounds, the four
guarantees are mechanisms, and the suite is how you check that a store keeps them.
