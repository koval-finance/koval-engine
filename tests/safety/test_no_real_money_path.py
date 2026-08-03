"""The engine must have no code path to a real-money trading endpoint.

These tests are the evidence behind the claim made in README.md and SECURITY.md.
If any of them fails, that claim is false.

Unlike the rest of the suite these assertions pin behaviour that already exists;
their job is to make the guarantee impossible to erode silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import responses

from koval.exchanges import sandbox_factory
from koval.exchanges.binance_sandbox import BinanceSandboxBroker
from koval.exchanges.sandbox_factory import SandboxBrokerConfig, build_broker
from koval.exchanges.whitebit_sandbox import SandboxUnavailableError, WhiteBITSandboxBroker

BINANCE_PRODUCTION_URLS = [
    "https://fapi.binance.com",
    "https://fapi.binance.com/",
    "https://api.binance.com",
    "https://dapi.binance.com",
    "https://api1.binance.com",
    "https://testnet.binancefuture.com.evil.example",
]

WHITEBIT_PRODUCTION_URLS = [
    "https://whitebit.com",
    "https://whitebit.com.",
    "https://api.whitebit.com",
    "https://whitebit.eu",
    "https://api.whitebit.eu",
    "https://trade.whitebit.com",
    "http://sandbox.whitebit.example.test",
    "https://sandbox.whitebit.example.test",
]


@pytest.mark.parametrize("url", BINANCE_PRODUCTION_URLS)
def test_binance_sandbox_broker_refuses_production_endpoints(url):
    with pytest.raises(ValueError, match="allowlisted"):
        BinanceSandboxBroker(api_key="k", api_secret="s", base_url=url)


def test_binance_sandbox_broker_accepts_only_the_futures_testnet():
    broker = BinanceSandboxBroker(
        api_key="k", api_secret="s", base_url="https://testnet.binancefuture.com"
    )
    assert broker.base_url == "https://testnet.binancefuture.com"


@responses.activate
def test_binance_signed_request_refuses_redirect_to_production_endpoint():
    testnet_url = "https://testnet.binancefuture.com/fapi/v1/order"
    production_url = "https://fapi.binance.com/fapi/v1/order"
    responses.add(
        responses.DELETE,
        testnet_url,
        status=307,
        headers={"Location": production_url},
    )
    responses.add(responses.DELETE, production_url, json={"status": "ok"})
    broker = BinanceSandboxBroker(api_key="k", api_secret="s")
    broker._track_order(  # noqa: SLF001 - exercise the session-scoped cancel path
        "kv-stop", "BTCUSDT", "stop", "buy", "session"
    )

    with pytest.raises(RuntimeError, match="redirect"):
        broker.cancel_all("BTCUSDT", "session")

    assert len(responses.calls) == 1
    assert responses.calls[0].request.url.startswith(testnet_url)


@pytest.mark.parametrize("url", WHITEBIT_PRODUCTION_URLS)
def test_whitebit_sandbox_broker_is_unavailable_without_a_verified_public_testnet(url):
    with pytest.raises(SandboxUnavailableError):
        WhiteBITSandboxBroker(
            api_key="k",
            api_secret="s",
            base_url=url,
            allow_test_only_non_money_url=True,
        )


def test_whitebit_sandbox_broker_is_fail_closed_without_a_url():
    with pytest.raises(SandboxUnavailableError):
        WhiteBITSandboxBroker(api_key="k", api_secret="s", base_url="")


@pytest.mark.parametrize("mode", ["live", "real", "binance", "binance_live", "", "PAPER"])
def test_broker_factory_rejects_every_unlisted_mode(mode):
    with pytest.raises(ValueError, match="unsupported sandbox broker mode"):
        build_broker(mode, SandboxBrokerConfig())


def test_allowlisted_modes_are_exactly_paper_and_verified_binance_sandbox():
    assert sandbox_factory._ALLOWED_MODES == {
        "paper",
        "binance_sandbox",
    }


def test_factory_rejects_whitebit_sandbox_until_a_verified_endpoint_exists():
    with pytest.raises(ValueError, match="unsupported sandbox broker mode"):
        build_broker(
            "whitebit_sandbox",
            SandboxBrokerConfig(
                env={
                    "WHITEBIT_SANDBOX_API_KEY": "k",
                    "WHITEBIT_SANDBOX_API_SECRET": "s",
                    "WHITEBIT_SANDBOX_BASE_URL": "https://sandbox.whitebit.example.test",
                }
            ),
        )


def test_contributor_documentation_matches_the_execution_allowlist():
    contributing = (Path(__file__).resolve().parents[2] / "CONTRIBUTING.md").read_text(
        encoding="utf-8"
    )

    assert "`paper`, `binance_sandbox`, and `whitebit_sandbox`" not in contributing
    assert "Only `paper` and `binance_sandbox` are allowlisted modes." in contributing
