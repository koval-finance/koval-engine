"""MIT market vocabulary shared by historical and paper runtimes."""

from dataclasses import asdict, dataclass

from koval.exchanges.execution_compatibility import assert_supported_market


@dataclass(frozen=True)
class MarketIdentity:
    exchange: str
    market: str
    canonical_symbol: str
    contract_type: str

    def __post_init__(self) -> None:
        venue, market = assert_supported_market(self.exchange, self.market)
        if venue != self.exchange or market != self.market:
            raise ValueError("market identity must use canonical venue and market names")
        if not self.canonical_symbol or self.canonical_symbol != canonical_symbol(
            self.canonical_symbol
        ):
            raise ValueError("market identity requires a canonical symbol")
        expected = "spot" if market == "spot" else "perpetual"
        if self.contract_type != expected:
            raise ValueError(f"unsupported contract_type for {market}: {self.contract_type}")

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def canonical_symbol(symbol: str) -> str:
    value = str(symbol).strip().replace("/", "").replace("_", "").upper()
    if not value or not value.isascii() or not value.isalnum():
        raise ValueError("symbol must contain ASCII letters and digits with optional / or _")
    return value


def resolve_market_identity(
    *,
    exchange: str,
    market: str,
    symbol: str,
    contract_type: str | None = None,
) -> MarketIdentity:
    venue, canonical = assert_supported_market(exchange, market)
    return MarketIdentity(
        venue,
        canonical,
        canonical_symbol(symbol),
        contract_type or ("spot" if canonical == "spot" else "perpetual"),
    )


__all__ = ["MarketIdentity", "canonical_symbol", "resolve_market_identity"]
