"""Hypothesis profiles for the property tests.

Select one with ``HYPOTHESIS_PROFILE`` (default ``ci``):

- ``ci``: a few hundred examples per test, short stateful runs. Fast enough for
  every ``uv run pytest``.
- ``hard``: 20000 examples per test and long stateful runs (many steps per run).
  Run locally, once, before trusting a change to the ledger:

      HYPOTHESIS_PROFILE=hard uv run pytest tests/properties --hypothesis-show-statistics
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

settings.register_profile(
    "ci",
    max_examples=300,
    stateful_step_count=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "hard",
    max_examples=20000,
    stateful_step_count=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
