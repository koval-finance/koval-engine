import time

import pytest
import responses

from koval.engine.paper_broker import PaperBroker
from koval.exchanges.binance_sandbox import BinanceSandboxBroker, ClockSkewExceeded
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


def test_paper_broker_is_built_with_the_requested_profile():
    from koval.exchanges.sandbox_factory import SandboxBrokerConfig, build_broker

    broker = build_broker(
        "paper",
        SandboxBrokerConfig(
            initial_capital=10_000.0,
            paper_profile={
                "version": "paper_ohlcv_fixed_v1",
                "commission_bps": 4.0,
                "spread_bps": 20.0,
                "slippage_bps": 10.0,
            },
        ),
    )
    assert broker.profile.is_fixed
    assert build_broker("paper", SandboxBrokerConfig()).profile.version == "paper_legacy_v1"


@responses.activate
def test_sandbox_broker_is_clock_synchronized_before_it_signs_anything():
    """A host clock outside Binance's recvWindow rejects every signed request,
    so the session's broker measures venue time when it is built rather than
    leaving it to whoever remembers."""
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/time",
        json={"serverTime": int(time.time() * 1000) + 4_000},
    )
    env = {"BINANCE_SANDBOX_API_KEY": "k", "BINANCE_SANDBOX_API_SECRET": "s"}

    broker = build_broker("binance_sandbox", SandboxBrokerConfig(env=env))

    assert 3_000 < broker.clock_skew_ms <= 4_000


@responses.activate
def test_an_unreachable_server_clock_does_not_block_building_the_broker():
    """The venue validates the timestamp itself; an unavailable time endpoint
    must not become a second way to lose the session."""
    responses.add(responses.GET, "https://testnet.binancefuture.com/fapi/v1/time", status=503)
    env = {"BINANCE_SANDBOX_API_KEY": "k", "BINANCE_SANDBOX_API_SECRET": "s"}

    broker = build_broker("binance_sandbox", SandboxBrokerConfig(env=env))

    assert broker.clock_skew_ms == 0


@responses.activate
def test_a_host_clock_too_far_from_venue_time_fails_the_session_closed():
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/time",
        json={"serverTime": 1},
    )
    env = {"BINANCE_SANDBOX_API_KEY": "k", "BINANCE_SANDBOX_API_SECRET": "s"}

    with pytest.raises(ClockSkewExceeded, match="clock_skew_exceeded"):
        build_broker("binance_sandbox", SandboxBrokerConfig(env=env))
