"""VL-2 promotion gate (Phase 2) — the keystone.

A record whose source_trust != operator cannot become actionable intent through
the in-band channel. The strict sink (ingested) is refuse-until-promoted. Only
the out-of-band ceremony in ``promote.py`` mints operator trust. See SPEC.md
§ VL-2.
"""
