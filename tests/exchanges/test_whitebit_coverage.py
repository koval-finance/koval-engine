"""WhiteBIT closed-window and coverage regressions."""

import json
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest
import responses

from koval.exchanges.whitebit import WhiteBITAdapter

URL = "https://whitebit.com/api/v1/public/kline"
START = 1788998400


def body(times):
    return {"success": True, "result": [[t, "100", "101", "102", "99", "2", "202"] for t in times]}


@responses.activate
def test_current_window_does_not_request_the_forming_minute(monkeypatch):
    requested_end = START + 1000 * 60 + 21
    monkeypatch.setattr("koval.exchanges.whitebit.time.time", lambda: requested_end)
    responses.add(
        responses.GET,
        URL,
        json=body(range(START, START + 1000 * 60, 60)),
        status=200,
    )
    adapter = WhiteBITAdapter(market="future")
    rows = adapter.fetch_ohlcv("BTC_PERP", "1m", START * 1000, requested_end * 1000)
    assert rows.shape == (1000, 6)
    assert len(responses.calls) == 1
    assert adapter.last_fetch_metadata["requested_end_ms"] == requested_end * 1000
    assert adapter.last_fetch_metadata["closed_cutoff_ms"] == (START + 1000 * 60) * 1000
    assert adapter.last_fetch_metadata["coverage_complete"] is True


@pytest.mark.parametrize("market", ["spot", "future"])
@responses.activate
def test_bounded_pages_cannot_silently_skip_the_start_of_a_long_range(market):
    def reply(request):
        query = parse_qs(urlparse(request.url).query)
        start, end = int(query["start"][0]), int(query["end"][0])
        # The server truncates from the left when the request exceeds its limit.
        times = list(range(start, end, 60))[-2:]
        return 200, {}, json.dumps(body(times))

    responses.add_callback(responses.GET, URL, callback=reply)
    adapter = WhiteBITAdapter(market=market, page_limit=2)
    rows = adapter.fetch_ohlcv("BTC/USDT", "1m", START * 1000, (START + 300) * 1000)
    np.testing.assert_array_equal(rows[:, 0], np.arange(START, START + 300, 60) * 1000)
    assert len(responses.calls) == 3


@responses.activate
def test_gap_inside_page_fails_instead_of_certifying_partial_history():
    responses.add(responses.GET, URL, json=body([START, START + 120]))
    with pytest.raises(RuntimeError, match="incomplete.*coverage"):
        WhiteBITAdapter().fetch_ohlcv("BTC/USDT", "1m", START * 1000, (START + 180) * 1000)


@responses.activate
def test_conflicting_duplicate_is_not_silently_dropped():
    page = body([START, START])
    page["result"][1][5] = "3"
    responses.add(responses.GET, URL, json=page)
    with pytest.raises(ValueError, match="conflicting"):
        WhiteBITAdapter().fetch_ohlcv("BTC/USDT", "1m", START * 1000, (START + 60) * 1000)


@responses.activate
def test_malformed_success_payload_is_not_an_empty_dataset():
    responses.get(URL, json={"success": True, "result": {"error": "unexpected"}})
    with pytest.raises(ValueError, match="result.*list"):
        WhiteBITAdapter().fetch_ohlcv("BTC/USDT", "1m", START * 1000, (START + 60) * 1000)
