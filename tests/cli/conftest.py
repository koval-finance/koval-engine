import pytest

from koval.examples import example_path

HEADER = "timestamp_ms,open,high,low,close,volume"


@pytest.fixture
def sample_csv(tmp_path):
    """A 300-bar deterministic uptrend, enough for a 200-period trend filter."""
    rows = []
    price = 100.0
    for index in range(300):
        price *= 1.002
        rows.append(
            f"{1_700_000_000_000 + index * 3_600_000},"
            f"{price:.4f},{price * 1.004:.4f},{price * 0.996:.4f},{price * 1.001:.4f},1000"
        )
    path = tmp_path / "sample.csv"
    path.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def example_graph():
    return str(example_path("graphs", "ema_cross_trend.json"))
