from koval.strategy.graph.domains import Domain, can_flow, rank


def test_fact_and_state_are_co_rank():
    assert rank(Domain.FACT) == rank(Domain.STATE)


def test_fact_to_state_and_state_to_fact_allowed():
    assert can_flow(Domain.FACT, Domain.STATE) is True
    assert can_flow(Domain.STATE, Domain.FACT) is True


def test_detector_cannot_consume_interpretation():
    # Manifesto §4 invariant — enforced structurally by the rank rule.
    assert can_flow(Domain.INTERPRETATION, Domain.FACT) is False
    assert can_flow(Domain.EXECUTION, Domain.STATE) is False
    assert can_flow(Domain.INTERPRETATION, Domain.POLICY) is False


def test_forward_flow_allowed():
    assert can_flow(Domain.FACT, Domain.POLICY) is True
    assert can_flow(Domain.POLICY, Domain.INTERPRETATION) is True
    assert can_flow(Domain.FACT, Domain.EXECUTION) is True
