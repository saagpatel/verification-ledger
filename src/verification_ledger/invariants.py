"""Loud-assertion vocabulary with a configurable failure policy (Phase 3).

``always``/``sometimes`` in the TigerBeetle style: an invariant violation is
either raised loudly or reported as degraded health (adopter's choice), but it
may never pass unnoticed. Distilled from the reference store's assertion layer.
"""
