from koval.strategy.graph.validation import _find_cycle


def test_find_cycle_detects_back_edge():
    assert _find_cycle({"a", "b"}, [("a", "b"), ("b", "a")]) is not None


def test_find_cycle_none_for_dag():
    assert _find_cycle({"a", "b", "c"}, [("a", "b"), ("b", "c")]) is None
