import pytest

from koval.exchanges.whitebit_sandbox import SandboxUnavailableError, WhiteBITSandboxBroker


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "https://whitebit.com",
        "https://whitebit.com.",
        "https://api.whitebit.eu",
        "https://sandbox.whitebit.example.test",
    ],
)
def test_whitebit_sandbox_execution_is_fail_closed(base_url):
    with pytest.raises(SandboxUnavailableError, match="execution is disabled"):
        WhiteBITSandboxBroker(
            api_key="key",
            api_secret="secret",
            base_url=base_url,
            allow_test_only_non_money_url=True,
        )


def test_constructor_bypass_exposes_no_order_submission_methods():
    broker = object.__new__(WhiteBITSandboxBroker)

    assert not hasattr(broker, "submit_entry")
    assert not hasattr(broker, "place_protection")
    assert not hasattr(broker, "cancel_all")
    assert not hasattr(broker, "flatten")
