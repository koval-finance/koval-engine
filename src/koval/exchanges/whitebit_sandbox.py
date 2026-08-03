"""Fail-closed WhiteBIT sandbox boundary.

WhiteBIT does not publish a verified non-money trading endpoint. Execution is
therefore unavailable until an official sandbox origin and contract exist.
Read-only WhiteBIT market-data support remains in :mod:`koval.exchanges.whitebit`.
"""

from __future__ import annotations

import requests

_UNAVAILABLE = "WhiteBIT has no verified public sandbox/testnet; execution is disabled"


class SandboxUnavailableError(ValueError):
    """Raised whenever WhiteBIT sandbox execution is requested."""


class WhiteBITSandboxBroker:
    """Compatibility placeholder that cannot create an execution path."""

    target = "whitebit_sandbox"

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str,
        session: requests.Session | None = None,
        timeout: float = 10.0,
        allow_test_only_non_money_url: bool = False,
    ) -> None:
        del api_key, api_secret, base_url, session, timeout, allow_test_only_non_money_url
        raise SandboxUnavailableError(_UNAVAILABLE)
