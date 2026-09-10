import pytest

from koval.engine.paper_broker import PaperBroker
from koval.exchanges.execution_compatibility import compatibility_for
from koval.exchanges.sandbox_factory import SandboxBrokerConfig, build_broker


def test_canonical_matrix_allows_only_binance_future_sandbox_execution():
    sandbox = compatibility_for("binance", "future", "binance_sandbox")
    assert sandbox.sandbox_execution and sandbox.allow_short and sandbox.max_leverage == 125.0

    with pytest.raises(ValueError, match="incompatible execution selection"):
        compatibility_for("binance", "spot", "binance_sandbox")
    with pytest.raises(ValueError, match="incompatible execution selection"):
        compatibility_for("whitebit", "future", "binance_sandbox")


def test_spot_paper_factory_rejects_leverage_and_broker_rejects_synthetic_short():
    with pytest.raises(ValueError, match="spot leverage"):
        build_broker(
            "paper",
            SandboxBrokerConfig(
                exchange="binance",
                exchange_type="spot",
                paper_profile={
                    "version": "paper_ohlcv_realistic_v2",
                    "commission_bps": 10.0,
                    "spread_bps": 0.0,
                    "slippage_bps": 0.0,
                    "leverage": 2.0,
                },
            ),
        )

    broker = build_broker(
        "paper",
        SandboxBrokerConfig(exchange="whitebit", exchange_type="spot"),
    )
    assert isinstance(broker, PaperBroker)
    with pytest.raises(ValueError, match="spot paper execution does not support short"):
        broker.submit_bracket(
            side="sell",
            entry_price=100.0,
            stop_price=110.0,
            target_price=90.0,
            quantity=1.0,
            order_type="market",
        )


def test_factory_rejects_exchange_mode_disagreement_before_credentials_are_read():
    with pytest.raises(ValueError, match="incompatible execution selection"):
        build_broker(
            "binance_sandbox",
            SandboxBrokerConfig(exchange="whitebit", exchange_type="future", env={}),
        )
