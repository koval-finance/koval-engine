from koval.strategy.helpers.interp.scoring import score_additive


def test_empty_keys_score_zero():
    assert score_additive({"bos": 20}, []) == (0, "")


def test_sums_present_keys_with_breakdown():
    total, breakdown = score_additive({"bos": 20, "choch": 25}, ["bos", "choch"])
    assert total == 45
    assert breakdown == "bos +20, choch +25"


def test_missing_key_is_ignored():
    assert score_additive({"bos": 20}, ["bos", "unknown"]) == (20, "bos +20")


def test_negative_points_render_with_sign():
    assert score_additive({"counter_trend": -15}, ["counter_trend"]) == (
        -15,
        "counter_trend -15",
    )


def test_repeated_key_counts_each_occurrence():
    assert score_additive({"bos": 20}, ["bos", "bos"]) == (40, "bos +20, bos +20")
