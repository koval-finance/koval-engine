import pytest

from koval.engine.execution_conformance import (
    IntentionalDifference,
    compare_execution_results,
    generate_conformance_scenarios,
)


def test_deterministic_scenarios_cover_both_sides_and_all_entry_types():
    first = generate_conformance_scenarios()
    second = generate_conformance_scenarios()
    assert first == second
    assert {(case.side, case.order_type) for case in first} == {
        (side, order_type) for side in ("buy", "sell") for order_type in ("market", "limit", "stop")
    }
    tags = {tag for case in first for tag in case.coverage_tags}
    assert {
        "market_entry",
        "limit_entry",
        "stop_entry",
        "stop_loss",
        "take_profit",
        "favorable_gap",
        "adverse_gap",
        "ambiguous_protection",
        "cancellation",
        "oco",
        "insufficient_margin",
        "end_of_data_open_at_stop",
        "dynamic_protection",
    } <= tags

    cancellation = next(case for case in first if "cancellation" in case.coverage_tags)
    assert cancellation.after_bar_actions[0].kind == "cancel_entry"
    margin = next(case for case in first if "insufficient_margin" in case.coverage_tags)
    assert margin.initial_capital < margin.entry_price * margin.quantity
    dynamic = next(case for case in first if "dynamic_protection" in case.coverage_tags)
    assert dynamic.after_bar_actions[0].kind == "modify_stop"


def test_conformance_comparison_requires_reason_for_every_difference():
    expected = {"fills": [{"price": 100.0}], "final_equity": 10_000.0}
    observed = {"fills": [{"price": 100.0}], "final_equity": 9_999.0}
    with pytest.raises(AssertionError, match="final_equity"):
        compare_execution_results(expected, observed)

    compare_execution_results(
        expected,
        observed,
        allowed_differences=(
            IntentionalDifference(
                path="final_equity",
                reason_code="live_session_terminal_flatten",
            ),
        ),
    )


def test_conformance_comparison_rejects_blank_or_unused_waivers():
    expected = {"fills": []}
    with pytest.raises(ValueError, match="reason_code"):
        compare_execution_results(
            expected,
            expected,
            allowed_differences=(IntentionalDifference(path="fills", reason_code=""),),
        )
    with pytest.raises(AssertionError, match="unused"):
        compare_execution_results(
            expected,
            expected,
            allowed_differences=(
                IntentionalDifference(path="fills", reason_code="documented_but_not_present"),
            ),
        )


def test_last_bit_float_noise_between_runtimes_is_not_a_parity_difference():
    """Two runtimes computing the same fee in a different operation order differ
    in the last bits for roughly half of realistic inputs. That is a float
    representation artefact, not a disagreement about execution."""
    paper = 8.764686915555002
    plugin = 8.764686915555

    assert paper != plugin
    compare_execution_results({"final_equity": paper}, {"final_equity": plugin})


def test_an_economically_meaningful_difference_is_still_reported():
    with pytest.raises(AssertionError, match="final_equity"):
        compare_execution_results({"final_equity": 8.7646869}, {"final_equity": 8.7646870})


def test_comparison_tolerance_is_configurable_and_defaults_to_negligible():
    compare_execution_results(
        {"fill_price": 100.0}, {"fill_price": 100.000001}, relative_tolerance=1e-5
    )

    with pytest.raises(AssertionError, match="fill_price"):
        compare_execution_results({"fill_price": 100.0}, {"fill_price": 100.000001})


def test_non_numeric_values_are_still_compared_exactly():
    with pytest.raises(AssertionError, match="reason_code"):
        compare_execution_results({"reason_code": "stop_loss"}, {"reason_code": "stop-loss"})
