"""Importing this package registers every typed node into NODE_CATALOG."""

from koval.strategy.nodes import (  # noqa: F401
    aggregator,
    condition_reducers,
    conditions,
    cross,
    exec_pipeline,
    execution,
    fact,
    interp,
    levels,
    policy,
    scoring,
    spot_recipes,
    state,
    values,
)
