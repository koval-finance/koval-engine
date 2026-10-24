"""Importing this package registers every typed node into NODE_CATALOG."""

from koval.strategy.nodes import (  # noqa: F401
    aggregator,
    conditions,
    cross,
    exec_pipeline,
    execution,
    fact,
    interp,
    policy,
    scoring,
    spot_recipes,
    state,
    values,
)
