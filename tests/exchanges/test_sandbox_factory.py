from koval.engine.paper_broker import PaperBroker
from koval.exchanges.binance_sandbox import BinanceSandboxBroker
from koval.exchanges.sandbox_factory import SandboxBrokerConfig, build_broker


def test_factory_returns_paper_broker():
    broker = build_broker("paper", SandboxBrokerConfig(initial_capital=10_000.0, env={}))

    assert isinstance(broker, PaperBroker)


def test_factory_returns_binance_sandbox_only_with_credentials():
    env = {"BINANCE_SANDBOX_API_KEY": "key", "BINANCE_SANDBOX_API_SECRET": "secret"}
    broker = build_broker("binance_sandbox", SandboxBrokerConfig(env=env))

    assert isinstance(broker, BinanceSandboxBroker)


def test_factory_rejects_whitebit_until_an_official_sandbox_exists():
    env = {
        "WHITEBIT_SANDBOX_BASE_URL": "https://sandbox.whitebit.local.test",
        "WHITEBIT_SANDBOX_API_KEY": "key",
        "WHITEBIT_SANDBOX_API_SECRET": "secret",
    }

    try:
        build_broker("whitebit_sandbox", SandboxBrokerConfig(env=env))
    except ValueError as exc:
        assert "unsupported sandbox broker mode" in str(exc)
    else:
        raise AssertionError("expected WhiteBIT runtime rejection")


def test_factory_rejects_real_money_like_modes():
    for mode in ("live", "real", "binance", "binance_live", "unknown"):
        try:
            build_broker(mode, SandboxBrokerConfig(env={}))
        except ValueError:
            continue
        raise AssertionError(f"expected {mode} to be rejected")
