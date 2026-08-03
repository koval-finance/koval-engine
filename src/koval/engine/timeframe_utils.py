"""Timeframe utilities for engine ordering and conversion."""


def timeframe_to_minutes(timeframe: str) -> int:
    """Convert timeframe strings like 15m/1h/4h/1d to minutes for stable ordering."""
    if not timeframe:
        return 10**9
    value_part = str(timeframe).strip().lower()[:-1]
    unit = str(timeframe).strip().lower()[-1:]
    try:
        value = int(value_part)
    except (TypeError, ValueError):
        return 10**9
    multipliers = {
        "m": 1,
        "h": 60,
        "d": 1440,
        "w": 10080,
    }
    return value * multipliers.get(unit, 10**9)


def ordered_timeframes(timeframes):
    """
    Always add lower timeframe first so multi-timeframe strategies receive:
    data0 = LTF, data1 = HTF, regardless of config array order.
    """
    items = list(timeframes or ["1h"])
    return sorted(items, key=timeframe_to_minutes)
