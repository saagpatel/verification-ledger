"""The conformance suite: executable proof that a store enforces the contract.

Runs bidirectional probes (positive + adversarial) per invariant against any
store that implements the ``LedgerAdapter`` protocol, and reports a top-line
Ledger Conformance Score. The suite is the standard; the bundled reference
implementation is one conformant store. See SPEC.md § "Conformance scoring".
"""
