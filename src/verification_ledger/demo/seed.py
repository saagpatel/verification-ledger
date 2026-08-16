"""Synthetic data generator for the demo and tests.

Generates entirely synthetic coordination records — no real fleet names, no real
data. The demo never reads any live store; this generator is its only data source.
"""

from __future__ import annotations

from verification_ledger.ledger import Ledger
from verification_ledger.model import Channel, Trust

# Entirely fictional content.
_AGENT_NOTES = (
    "draft summary of the open ticket",
    "proposed refactor plan for the parser",
    "candidate reply to the review comment",
)
_INGESTED_ITEMS = (
    "fetched web page snippet",
    "text scanned from an uploaded file",
    "raw output from an upstream tool",
)


def seed(ledger: Ledger) -> int:
    """Populate ``ledger`` with a synthetic mix of agent, ingested, and durable records.

    Returns the number of records written. Everything is in-band and non-operator,
    exactly as an untrusted agent fleet would produce it.
    """
    written = 0
    for note in _AGENT_NOTES:
        ledger.write(note, source_trust=Trust.AGENT, channel=Channel.IN_BAND)
        written += 1
    for item in _INGESTED_ITEMS:
        ledger.write(item, source_trust=Trust.INGESTED, channel=Channel.IN_BAND)
        written += 1
    ledger.write(
        "shipped: release build",
        source_trust=Trust.AGENT,
        channel=Channel.IN_BAND,
        durable=True,
    )
    written += 1
    return written
