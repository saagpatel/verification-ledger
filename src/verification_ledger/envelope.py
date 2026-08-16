"""VL-3 instruction-boundary envelope (Phase 1).

Wraps every read so a consuming model is told, structurally, that the content is
stored data carrying a provenance label — not an instruction. Advisory signal,
not proof of authorship. See SPEC.md § VL-3.
"""
