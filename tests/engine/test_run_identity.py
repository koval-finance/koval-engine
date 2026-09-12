"""Public identities name actual inputs, independent of runtime and chunking."""

from dataclasses import replace
from decimal import Decimal

import numpy as np
import pytest

from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal
from koval.engine.paper_broker import PaperBroker
from koval.engine.paper_profile import resolve_paper_profile
from koval.exchanges.sandbox_factory import SandboxBrokerConfig, build_broker
from tests.engine.live_fixtures import ema_cross_graph
from tests.engine.test_runtime_contract import EXECUTION


def test_factory_preserves_all_evidence_and_records_content_identity():
    from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig
    from koval.engine.fee_evidence import FeeScheduleEvidence
    from koval.engine.funding import build_funding_series
    from koval.engine.instrument_risk import MarkPriceRecord, MarkPriceSeries
    from tests.engine.test_instrument_risk import _spec

    evidence = dict(
        funding=build_funding_series(
            [],
            exchange="binance",
            market="future",
            symbol="BTCUSDT",
            requested_start_ms=0,
            requested_end_ms=60_000,
            interval_ms=28_800_000,
            settlement_anchor_ms=3_600_000,
            schedule_source="archived_schedule",
        ),
        fee_schedule=FeeScheduleEvidence("fees", 1, 4, "USDT", "approximation", "test"),
        instrument_specs=(_spec(start=0, end=60_000),),
        mark_prices=MarkPriceSeries(
            "binance",
            "BTCUSDT",
            60_000,
            0,
            0,
            (MarkPriceRecord(0, Decimal("100"), "archive"),),
            True,
        ),
        execution_proxy=ExecutionProxyConfig(Decimal("0.1"), "carry", ExecutionLatency()),
    )
    broker = build_broker("paper", SandboxBrokerConfig(paper_profile=EXECUTION, **evidence))
    direct = PaperBroker(10_000, profile=resolve_paper_profile(EXECUTION), **evidence)
    assert broker.execution_evidence == direct.execution_evidence
    assert all(item["sha256"] for item in broker.execution_evidence.values())
    changed = PaperBroker(
        10_000,
        profile=resolve_paper_profile(EXECUTION),
        **{
            **evidence,
            "fee_schedule": replace(evidence["fee_schedule"], taker_bps=5),
        },
    )
    assert changed.execution_evidence != broker.execution_evidence


def test_missing_evidence_is_explicit_even_with_realistic_profile():
    broker = PaperBroker(10_000, profile=resolve_paper_profile(EXECUTION))
    assert all(item["status"] == "unavailable" for item in broker.execution_evidence.values())
    assert broker.resolved_metadata["execution_evidence"] == broker.execution_evidence


def test_injected_broker_cannot_silently_discard_config_evidence():
    from koval.engine.fee_evidence import FeeScheduleEvidence

    config = LiveEngineConfig(
        "BTCUSDT",
        "1m",
        10_000,
        execution=EXECUTION,
        fee_schedule=FeeScheduleEvidence("fees", 1, 4, "USDT", "approximation", "test"),
    )
    broker = PaperBroker(10_000, profile=resolve_paper_profile(EXECUTION))
    with pytest.raises(ValueError, match="evidence"):
        LiveEngine(ema_cross_graph(), config, broker=broker)


def test_market_identity_vocabulary_and_venue_validation():
    from koval.engine.market_identity import resolve_market_identity

    identity = resolve_market_identity(exchange=" Binance ", market="futures", symbol="btc/usdt")
    assert identity.as_dict() == {
        "exchange": "binance",
        "market": "future",
        "canonical_symbol": "BTCUSDT",
        "contract_type": "perpetual",
    }
    for kwargs in [dict(exchange="unknown"), dict(contract_type="delivery"), dict(symbol="")]:
        with pytest.raises(ValueError):
            resolve_market_identity(
                **(dict(exchange="binance", market="future", symbol="BTCUSDT") | kwargs)
            )


def test_stream_identity_is_deterministic_and_includes_warmup():
    from koval.engine.run_identity import CandleStreamIdentity

    rows = np.array([[0, 100, 101, 99, 100, 1], [60_000, 100, 102, 99, 101, 2]], dtype=float)
    first, second = CandleStreamIdentity("1m"), CandleStreamIdentity("1m")
    for row in rows:
        first.append(row)
        second.append(row.copy())
    assert first.as_dict() == second.as_dict()
    assert first.as_dict()["actual_start_ms"] == 0
    assert first.as_dict()["actual_end_ms"] == 120_000
    changed = CandleStreamIdentity("1m")
    changed.append(rows[0])
    changed.append([60_000, 100, 102, 99, 101, 3])
    assert first.as_dict()["sha256"] != changed.as_dict()["sha256"]


def test_live_run_records_actual_market_warmup_evidence_and_profile():
    statuses = []
    engine = LiveEngine(
        ema_cross_graph(),
        LiveEngineConfig(
            "BTCUSDT",
            "1m",
            10_000,
            exchange="binance",
            execution=EXECUTION,
            history=np.array([[0, 100, 101, 99, 100, 1]], dtype=float),
        ),
        on_status=statuses.append,
    )
    engine.run(ReplayFeed(np.array([[60_000, 100, 102, 99, 101, 2]], dtype=float)), StopSignal())
    identity = statuses[-1]["run_identity"]
    assert identity["market_identity"]["market"] == "future"
    assert identity["dataset_identity"]["primary"]["row_count"] == 1
    assert identity["dataset_identity"]["warmup"]["actual_start_ms"] == 0
    assert identity["execution_identity"]["profile"]["version"] == EXECUTION["version"]
    assert identity["reproducibility_grade"] == "identified_simulation"
    assert "funding" in identity["unavailable_effects"]


def test_unknown_venue_run_is_explicitly_not_comparable():
    statuses = []
    engine = LiveEngine(
        ema_cross_graph(), LiveEngineConfig("BTCUSDT", "1m", 10_000), on_status=statuses.append
    )
    engine.run(ReplayFeed(np.array([[0, 100, 101, 99, 100, 1]], dtype=float)), StopSignal())
    assert statuses[-1]["run_identity"]["reproducibility_grade"] == "not_comparable"


def test_factory_rejects_evidence_from_a_different_venue():
    from koval.engine.funding import build_funding_series

    funding = build_funding_series(
        [],
        exchange="whitebit",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=0,
        requested_end_ms=60_000,
        interval_ms=28_800_000,
        settlement_anchor_ms=3_600_000,
        schedule_source="archived_schedule",
    )
    with pytest.raises(ValueError, match="exchange"):
        build_broker("paper", SandboxBrokerConfig(exchange="binance", funding=funding))


def test_direct_spot_broker_refuses_leverage():
    with pytest.raises(ValueError, match="spot leverage"):
        PaperBroker(
            10_000, market="spot", profile=resolve_paper_profile({**EXECUTION, "leverage": 2})
        )


def test_injected_broker_cannot_mislabel_starting_capital():
    with pytest.raises(ValueError, match="starting balances"):
        LiveEngine(
            ema_cross_graph(), LiveEngineConfig("BTCUSDT", "1m", 10_000), broker=PaperBroker(20_000)
        )
