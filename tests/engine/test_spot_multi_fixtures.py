"""Published JSON fixtures are executable acceptance inputs for any adapter."""

import hashlib
from pathlib import Path

import pytest


def test_packaged_acceptance_fixtures_cover_controls_and_execution_edges():
    from koval.engine.spot_multi_order import run_spot_fixture
    from koval.examples import spot_multi_order_fixtures

    fixtures = spot_multi_order_fixtures()
    assert {f["id"] for f in fixtures} >= {
        "dca_control",
        "grid_control",
        "partial_fill",
        "cancel_race",
        "gap",
        "same_candle_multilevel",
        "first_partial_stop",
        "unknown_restart",
        "stale_replacement",
        "expiry",
        "rejection",
        "duplicate_conflict",
        "deferred_fee_valuation",
    }
    for fixture in fixtures:
        assert all(
            {"order_states", "intents", "fills"} <= step["expected"].keys()
            for step in fixture["observations"]
        ), fixture["id"]
        result = run_spot_fixture(fixture)
        assert result.matches, (fixture["id"], result.differences)


def test_fixture_checksums_pin_the_exact_published_json_bytes():
    from koval.examples import example_path

    root = example_path("spot_multi_order")
    entries = (root / "CHECKSUMS.txt").read_text().splitlines()
    assert entries
    for entry in entries:
        checksum, name = entry.split()
        assert Path(name).name == name
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == checksum


def test_conformance_comparison_preserves_order_identity_and_decimal_precision():
    from koval.engine.spot_multi_order import compare_spot_execution_reports

    assert compare_spot_execution_reports(
        {"cash": "1020"}, {"cash": "1020.0000000000000000005"}
    ).matches
    assert not compare_spot_execution_reports({"cash": "1020"}, {"cash": "1020.01"}).matches
    assert not compare_spot_execution_reports(
        {"orders": ["a", "b"]}, {"orders": ["b", "a"]}
    ).matches
    with pytest.raises(ValueError):
        compare_spot_execution_reports({}, {}, absolute_tolerance="1")


def test_end_of_data_flatten_uses_only_remaining_final_candle_volume():
    from tests.engine.test_spot_multi_paper import activate, candle, fills, paper

    p = paper()
    activate(p)
    p.on_candle(candle(2, 10, volume=12))
    p.finish(policy="stop_and_flatten")
    assert sum(e.quantity for e in fills(p)) == 12
    assert p.state.account.net_base == 8
    assert p.state.controller.phase != "stopped_flat"
    document = p.export_state()
    p.finish(policy="stop_and_flatten")
    assert p.state.account.net_base == 8
    assert p.export_state() == document
