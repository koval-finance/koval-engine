"""Read-only Binance USD-M account evidence acquisition.

The client can read current commission and maintenance-margin brackets. It has
no configurable origin, accepts only two signed GET paths, and exposes no order
or account-mutation method. Historical consumers must archive each returned
snapshot; a current response is not historical evidence by itself.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from urllib.parse import urlencode

import requests

from koval.engine.fee_evidence import FeeScheduleEvidence
from koval.engine.instrument_risk import InstrumentSpecEvidence, MaintenanceMarginTier
from koval.engine.market_identity import canonical_symbol
from koval.engine.run_identity import content_sha256
from koval.exchanges.auth import hmac_sha256, safe_exception_message

_BASE_URL: Final = "https://fapi.binance.com"
_SIGNED_GET_PATHS: Final = frozenset({"/fapi/v1/leverageBracket", "/fapi/v1/commissionRate"})
_RECV_WINDOW_MS: Final = 5_000


class BinanceFuturesEvidenceClient:
    """Acquire current USD-M evidence with operator-owned read-only credentials."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        session: requests.Session | None = None,
        timeout: float = 10.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Binance evidence credentials must be non-empty")
        if not isinstance(api_secret, str) or not api_secret.strip():
            raise ValueError("Binance evidence credentials must be non-empty")
        parsed_timeout = float(timeout)
        if not Decimal(str(parsed_timeout)).is_finite() or parsed_timeout <= 0:
            raise ValueError("Binance evidence timeout must be positive and finite")
        self._api_key = api_key
        self._api_secret = api_secret
        self._session = session or requests.Session()
        self._timeout = parsed_timeout
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def fetch_instrument_spec(self, symbol: str) -> InstrumentSpecEvidence:
        venue_symbol = _normalize_symbol(symbol)
        observed_ms = _timestamp(self._clock_ms())
        exchange_info = self._public_get("/fapi/v1/exchangeInfo", {})
        bracket_response = self._signed_get("/fapi/v1/leverageBracket", {"symbol": venue_symbol})
        symbol_info = _unique_symbol(exchange_info.get("symbols"), venue_symbol)
        bracket_info = _unique_symbol(bracket_response, venue_symbol)
        return _instrument_spec(
            symbol_info,
            bracket_info,
            observed_ms=observed_ms,
            raw_response={
                "exchange_info": symbol_info,
                "leverage_bracket": bracket_info,
            },
        )

    def fetch_fee_schedule(self, symbol: str) -> FeeScheduleEvidence:
        venue_symbol = _normalize_symbol(symbol)
        observed_ms = _timestamp(self._clock_ms())
        raw = self._signed_get("/fapi/v1/commissionRate", {"symbol": venue_symbol})
        if raw.get("symbol") != venue_symbol:
            raise ValueError("Binance commission response symbol mismatch")
        maker = _decimal(raw.get("makerCommissionRate"), "maker commission") * 10_000
        taker = _decimal(raw.get("takerCommissionRate"), "taker commission") * 10_000
        quote = _quote_currency(venue_symbol)
        return FeeScheduleEvidence(
            evidence_id=content_sha256(
                {
                    "source": "binance_usdm_account_commission_rate",
                    "observed_ms": observed_ms,
                    "response": raw,
                }
            ),
            maker_bps=float(maker),
            taker_bps=float(taker),
            currency=quote,
            evidence_status="current_snapshot",
            source="binance_usdm_account_commission_rate",
            effective_from_ms=observed_ms,
            effective_to_ms=observed_ms,
            discount_treatment="account_rate_observed",
            tier_id="operator_account",
            exchange="binance",
            market="future",
            canonical_symbol=venue_symbol,
            raw_response=raw,
        )

    def _public_get(self, path: str, params: dict[str, object]) -> dict[str, Any]:
        response = self._request(path, params=params, headers={})
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Binance public evidence response must be an object")
        return data

    def _signed_get(self, path: str, params: dict[str, object]) -> Any:
        if path not in _SIGNED_GET_PATHS:
            raise ValueError("Binance evidence path is not allowlisted")
        query = {
            **params,
            "timestamp": _timestamp(self._clock_ms()),
            "recvWindow": _RECV_WINDOW_MS,
        }
        encoded = urlencode(query)
        signed = f"{encoded}&signature={hmac_sha256(self._api_secret, encoded)}"
        response = self._request(
            path,
            params=signed,
            headers={"X-MBX-APIKEY": self._api_key},
        )
        data = response.json()
        if not isinstance(data, (dict, list)):
            raise ValueError("Binance signed evidence response must be an object or list")
        return data

    def _request(
        self,
        path: str,
        *,
        params: object,
        headers: dict[str, str],
    ) -> requests.Response:
        try:
            response = self._session.get(
                f"{_BASE_URL}{path}",
                params=params,
                headers=headers,
                timeout=self._timeout,
                allow_redirects=False,
            )
            if response.is_redirect or response.is_permanent_redirect or response.history:
                raise RuntimeError("Binance evidence redirect refused")
            if not response.url.startswith(f"{_BASE_URL}/"):
                raise RuntimeError("Binance evidence response escaped the fixed origin")
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            exc.args = (safe_exception_message(exc),)
            exc.request = None
            exc.response = None
            raise


def _instrument_spec(
    symbol_info: dict[str, Any],
    bracket_info: dict[str, Any],
    *,
    observed_ms: int,
    raw_response: dict[str, Any],
) -> InstrumentSpecEvidence:
    if symbol_info.get("contractType") != "PERPETUAL" or symbol_info.get("status") != "TRADING":
        raise ValueError("Binance instrument must be a trading perpetual contract")
    symbol = _normalize_symbol(str(symbol_info.get("symbol", "")))
    if bracket_info.get("symbol") != symbol:
        raise ValueError("Binance leverage bracket symbol mismatch")
    filters = symbol_info.get("filters")
    if not isinstance(filters, list):
        raise ValueError("Binance instrument filters must be a list")
    by_type = {str(item.get("filterType")): item for item in filters if isinstance(item, dict)}
    price = _required_filter(by_type, "PRICE_FILTER")
    lot = _required_filter(by_type, "LOT_SIZE")
    minimum = _required_filter(by_type, "MIN_NOTIONAL")
    percent = _required_filter(by_type, "PERCENT_PRICE")
    brackets = bracket_info.get("brackets")
    if not isinstance(brackets, list) or not brackets:
        raise ValueError("Binance maintenance-margin brackets are required")
    tiers = tuple(
        MaintenanceMarginTier(
            notional_floor=_decimal(item.get("notionalFloor"), "notional floor"),
            notional_cap=_decimal(item.get("notionalCap"), "notional cap"),
            maintenance_margin_rate=_decimal(
                item.get("maintMarginRatio"), "maintenance margin ratio"
            ),
            maintenance_amount=_decimal(item.get("cum", 0), "maintenance amount"),
            maximum_leverage=_decimal(item.get("initialLeverage"), "maximum initial leverage"),
        )
        for item in brackets
        if isinstance(item, dict)
    )
    if len(tiers) != len(brackets):
        raise ValueError("Binance maintenance-margin bracket contains a malformed row")
    if any(
        current.notional_floor != previous.notional_cap
        for previous, current in zip(tiers, tiers[1:], strict=False)
    ):
        raise ValueError("Binance maintenance-margin brackets must be contiguous")
    liquidation_fee_bps = _decimal(symbol_info.get("liquidationFee"), "liquidation fee") * 10_000
    source = "binance_usdm_exchange_info_and_leverage_bracket"
    return InstrumentSpecEvidence(
        evidence_id=content_sha256(
            {"source": source, "observed_ms": observed_ms, "response": raw_response}
        ),
        exchange="binance",
        market="future",
        canonical_symbol=symbol,
        effective_from_ms=observed_ms,
        effective_to_ms=None,
        tick_size=_decimal(price.get("tickSize"), "tick size"),
        step_size=_decimal(lot.get("stepSize"), "step size"),
        minimum_quantity=_decimal(lot.get("minQty"), "minimum quantity"),
        minimum_notional=_decimal(minimum.get("notional"), "minimum notional"),
        minimum_price=_decimal(price.get("minPrice"), "minimum price"),
        maximum_price=_decimal(price.get("maxPrice"), "maximum price"),
        contract_size=Decimal("1"),
        collateral_currency=str(symbol_info.get("marginAsset", "")),
        margin_tiers=tiers,
        liquidation_fee_bps=liquidation_fee_bps,
        source=source,
        evidence_status="current_snapshot",
        price_band_low_multiplier=_decimal(
            percent.get("multiplierDown"), "price-band low multiplier"
        ),
        price_band_high_multiplier=_decimal(
            percent.get("multiplierUp"), "price-band high multiplier"
        ),
        raw_response=raw_response,
    )


def _required_filter(filters: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
    value = filters.get(name)
    if value is None:
        raise ValueError(f"Binance instrument is missing {name}")
    return value


def _unique_symbol(value: object, symbol: str) -> dict[str, Any]:
    if not isinstance(value, list):
        raise ValueError("Binance symbol evidence must be a list")
    matches = [item for item in value if isinstance(item, dict) and item.get("symbol") == symbol]
    if len(matches) != 1:
        raise ValueError("Binance symbol evidence requires one matching record")
    return matches[0]


def _decimal(value: object, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Binance {name} must be finite") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"Binance {name} must be finite and non-negative")
    return parsed


def _timestamp(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Binance evidence timestamp must be a non-negative integer")
    return value


def _normalize_symbol(symbol: str) -> str:
    value = canonical_symbol(symbol)
    if not value:
        raise ValueError("Binance evidence symbol is required")
    return value


def _quote_currency(symbol: str) -> str:
    for currency in ("FDUSD", "USDT", "USDC", "TUSD", "BUSD"):
        if symbol.endswith(currency):
            return currency
    raise ValueError("Binance USD-M evidence requires a supported quote currency")


__all__ = ["BinanceFuturesEvidenceClient"]
