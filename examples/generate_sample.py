"""Generate the deterministic sample OHLCV file used by the README quickstart.

Synthetic data — not exchange data. Regenerate with:
    python examples/generate_sample.py

The parameters below are chosen so the bundled example strategy actually trades,
and loses some of those trades. A mild upward drift with two superimposed
oscillations produces EMA crossings while price is above the 200-period trend
filter; the faster oscillation is what makes some of those entries fail. 1200
bars leaves room for the filter's warm-up and still gives a readable number of
trades.

Nothing here is tuned to flatter the strategy — a sample that never loses would
say more about the data than about the engine.
"""

from __future__ import annotations

import math
from pathlib import Path

BARS = 1200
START_MS = 1_700_000_000_000
STEP_MS = 3_600_000
DRIFT = 1.0005
WAVE_AMPLITUDE = 0.006
WAVE_PERIOD = 17.0
NOISE_AMPLITUDE = 0.012
NOISE_PERIOD = 7.0
NOISE_PHASE = 1.7
OUT = (
    Path(__file__).resolve().parent.parent / "src" / "koval" / "examples" / "data" / "sample-1h.csv"
)


def main() -> None:
    rows = ["timestamp_ms,open,high,low,close,volume"]
    price = 100.0
    for index in range(BARS):
        wave = (
            1.0
            + WAVE_AMPLITUDE * math.sin(index / WAVE_PERIOD)
            + NOISE_AMPLITUDE * math.sin(index / NOISE_PERIOD + NOISE_PHASE)
        )
        open_price = price
        close_price = price * DRIFT * wave
        high = max(open_price, close_price) * 1.003
        low = min(open_price, close_price) * 0.997
        rows.append(
            f"{START_MS + index * STEP_MS},{open_price:.4f},{high:.4f},"
            f"{low:.4f},{close_price:.4f},{1000 + index}"
        )
        price = close_price
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({BARS} bars)")


if __name__ == "__main__":
    main()
