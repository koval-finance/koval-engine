"""Public JSON transport must not silently discard requested execution effects."""

from decimal import Decimal

import pytest


def decode(value):
    from koval.engine.execution_evidence import decode_execution_evidence

    return decode_execution_evidence(value)


def proxy():
    return {
        "maximum_volume_participation": "0.05",
        "entry_remainder_policy": "carry",
        "latency": {"acknowledgement_to_fill_ms": 250, "protection_activation_ms": 100},
    }


def test_evidence_json_round_trip_preserves_decimal_and_latency():
    evidence = decode({"execution_proxy": proxy()})
    assert evidence.execution_proxy.maximum_volume_participation == Decimal("0.05")
    assert evidence.execution_proxy.latency.acknowledgement_to_fill_ms == 250
    assert decode(evidence.as_config()) == evidence
    assert evidence.manifest()["execution_proxy"]["status"] == "supplied"
    assert evidence.manifest()["funding"]["status"] == "unavailable"


@pytest.mark.parametrize(
    "value",
    [
        {"unknown": {}},
        {"execution_proxy": {**proxy(), "queue_priority": "first"}},
        {"execution_proxy": {**proxy(), "latency": {"cancellation_ms": True}}},
        {"execution_proxy": {**proxy(), "latency": {"cancellation_ms": 0.5}}},
        {"execution_proxy": {**proxy(), "maximum_volume_participation": "NaN"}},
        {"execution_proxy": {**proxy(), "maximum_volume_participation": False}},
    ],
)
def test_evidence_transport_rejects_lossy_or_nonfinite_inputs(value):
    with pytest.raises(ValueError):
        decode(value)


def test_evidence_report_never_invents_accuracy_or_observed_liquidity():
    evidence = decode({"execution_proxy": proxy()})
    report = evidence.realism_report()
    assert report["accuracy"] == "unmeasured"
    assert report["maximum_error_pct"] is None
    assert report["effects"]["partial_fills"] == "ohlcv_proxy"
    assert report["effects"]["funding"] == "unavailable"
    assert "queue_position" in report["unavailable_effects"]
    assert "intrabar_price_path" in report["unavailable_effects"]


def test_absent_evidence_stays_unavailable():
    assert decode({}).as_config() == {}
    assert decode({}).realism_report()["effects"]["liquidation"] == "unavailable"


def test_paper_metadata_carries_unmeasured_realism_report():
    from koval.engine.paper_broker import PaperBroker
    from koval.engine.paper_profile import resolve_paper_profile

    broker = PaperBroker(
        10000,
        profile=resolve_paper_profile(
            {
                "version": "paper_ohlcv_realistic_v2",
                "commission_bps": 4,
                "spread_bps": 2,
                "slippage_bps": 1,
            }
        ),
        **decode({"execution_proxy": proxy()}).as_kwargs(),
    )
    assert broker.resolved_metadata["realism_report"]["accuracy"] == "unmeasured"
    assert broker.resolved_metadata["realism_report"]["effects"]["partial_fills"] == "ohlcv_proxy"
