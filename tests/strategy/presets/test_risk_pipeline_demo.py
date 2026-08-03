def test_risk_pipeline_demo_assembles_and_validates():
    from koval.strategy.base.declarative import DeclarativeStrategy
    from koval.strategy.block_assembler import assemble_from_graph
    from koval.strategy.presets.risk_pipeline_demo import build_risk_pipeline_demo_graph

    graph = build_risk_pipeline_demo_graph()
    strat = assemble_from_graph(graph)  # must not raise GraphValidationError
    assert isinstance(strat, DeclarativeStrategy)
