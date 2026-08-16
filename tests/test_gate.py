"""VL-2 promotion gate — the keystone. Pure decisions plus the ledger matrix."""

from __future__ import annotations

import pytest

from verification_ledger.gate import evaluate_activation, evaluate_promotion
from verification_ledger.ledger import Ledger
from verification_ledger.model import Channel, Trust

# --- pure decision logic ---


def test_only_operator_activates() -> None:
    assert evaluate_activation(Trust.OPERATOR).allowed is True
    assert evaluate_activation(Trust.AGENT).allowed is False
    assert evaluate_activation(Trust.INGESTED).allowed is False


def test_promotion_requires_out_of_band() -> None:
    assert evaluate_promotion(Channel.OUT_OF_BAND).allowed is True
    assert evaluate_promotion(Channel.IN_BAND).allowed is False


# --- ledger integration matrix ---


def _agent_record(led: Ledger) -> int:
    return led.write("work", source_trust=Trust.AGENT).record_id


def test_operator_record_activates_in_one_in_band_call() -> None:
    with Ledger() as led:
        rid = led.write(
            "directive", source_trust=Trust.OPERATOR, channel=Channel.OUT_OF_BAND
        ).record_id
        result = led.activate(rid, channel=Channel.IN_BAND)
        assert result.allowed is True
        assert result.actionable is True


def test_agent_record_refused_in_band_and_unchanged() -> None:
    with Ledger() as led:
        rid = _agent_record(led)
        result = led.activate(rid, channel=Channel.IN_BAND)
        assert result.allowed is False
        assert result.actionable is False
        env = led.read(rid)
        assert env is not None
        assert env.record.actionable is False  # no mutation on refusal
        assert env.record.source_trust is Trust.AGENT


def test_ingested_is_refuse_until_promoted() -> None:
    with Ledger() as led:
        rid = led.write("scraped", source_trust=Trust.INGESTED).record_id
        assert led.activate(rid).allowed is False


def test_in_band_promotion_is_refused() -> None:
    with Ledger() as led:
        rid = _agent_record(led)
        result = led.promote(rid, channel=Channel.IN_BAND)
        assert result.promoted is False
        assert result.source_trust is Trust.AGENT  # unchanged


def test_no_in_band_path_makes_a_non_operator_record_actionable() -> None:
    # The adversarial core of VL-2: an agent record has NO in-band route to
    # actionable — activation is refused and promotion is refused.
    with Ledger() as led:
        rid = _agent_record(led)
        assert led.activate(rid, channel=Channel.IN_BAND).allowed is False
        assert led.promote(rid, channel=Channel.IN_BAND).promoted is False
        env = led.read(rid)
        assert env is not None
        assert env.record.actionable is False
        assert env.record.source_trust is Trust.AGENT


def test_out_of_band_promotion_then_activation_is_the_only_path() -> None:
    with Ledger() as led:
        rid = _agent_record(led)
        promo = led.promote(rid, channel=Channel.OUT_OF_BAND)
        assert promo.promoted is True
        assert promo.source_trust is Trust.OPERATOR
        result = led.activate(rid, channel=Channel.IN_BAND)
        assert result.allowed is True
        assert result.actionable is True


def test_activate_missing_raises() -> None:
    with Ledger() as led, pytest.raises(KeyError):
        led.activate(999)


def test_promote_missing_raises() -> None:
    with Ledger() as led, pytest.raises(KeyError):
        led.promote(999, channel=Channel.OUT_OF_BAND)


def test_promote_fails_closed_when_channel_omitted() -> None:
    # The keystone must fail closed: an integrator who calls promote() without
    # threading the channel is refused, not silently granted operator trust.
    with Ledger() as led:
        rid = _agent_record(led)
        result = led.promote(rid)  # channel omitted → defaults to the safe IN_BAND
        assert result.promoted is False
        assert result.source_trust is Trust.AGENT
        env = led.read(rid)
        assert env is not None
        assert env.record.source_trust is Trust.AGENT


def test_refused_activation_leaves_record_byte_identical() -> None:
    with Ledger() as led:
        rid = led.write("payload", source_trust=Trust.AGENT, durable=True).record_id
        before = led.read(rid)
        assert before is not None
        led.activate(rid)  # refused
        after = led.read(rid)
        assert after is not None
        assert after.record == before.record  # every field unchanged


def test_promote_touches_exactly_one_record() -> None:
    with Ledger() as led:
        keep = _agent_record(led)
        target = _agent_record(led)
        led.promote(target, channel=Channel.OUT_OF_BAND)
        other = led.read(keep)
        assert other is not None
        assert other.record.source_trust is Trust.AGENT  # isolation: untouched
