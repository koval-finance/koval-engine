"""The shipped parity fixtures are a published contract, not editable data."""

from __future__ import annotations

import hashlib
import json

import pytest

from koval.examples import EXAMPLES_DIR, parity_fixtures

_PARITY_DIR = EXAMPLES_DIR / "parity"
_REQUIRED_KEYS = {
    "fixture_id",
    "contract",
    "exchange",
    "exchange_type",
    "timeframe",
    "capital",
    "costs",
    "graph",
    "candles",
    "expected",
}


def test_expected_fixtures_are_shipped():
    ids = sorted(fixture["fixture_id"] for fixture in parity_fixtures())
    assert ids == [
        "long_entry_bar_ambiguity_v2",
        "long_favorable_limit_gap_v1",
        "long_gap_stop_v1",
        "long_leverage_rejected_v1",
        "long_leverage_spread_rejected_v1",
        "long_leverage_v1",
        "long_take_profit_touch_v1",
        "short_favorable_limit_gap_v1",
        "short_full_notional_bracket_v1",
        "short_leverage_v1",
    ]


@pytest.mark.parametrize("fixture", parity_fixtures(), ids=lambda f: f["fixture_id"])
def test_every_fixture_carries_the_contract_keys(fixture):
    assert _REQUIRED_KEYS <= set(fixture)
    assert fixture["contract"] in {
        "koval_execution_contract_v1",
        "koval_execution_contract_v2",
    }
    assert set(fixture["costs"]) == {
        "commission_bps",
        "spread_bps",
        "slippage_bps",
        "leverage",
    }
    assert all(len(row) == 6 for row in fixture["candles"])


def test_checksums_match_so_a_silent_edit_fails_the_build():
    recorded = {}
    for line in (_PARITY_DIR / "CHECKSUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        recorded[name] = digest
    actual = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(_PARITY_DIR.glob("*.json"))
    }
    assert actual == recorded


def test_fixture_ids_match_their_filenames():
    for path in sorted(_PARITY_DIR.glob("*.json")):
        assert json.loads(path.read_text(encoding="utf-8"))["fixture_id"] == path.stem
