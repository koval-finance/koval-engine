"""Assemble a `DeclarativeStrategy` from a JSON block graph.

This is a thin dispatcher onto the typed dataflow engine
(`koval/strategy/graph/`):

* complete, connected legacy linear-AND graphs (using types in
  ``BLOCK_CATALOG``) are compiled to a native typed graph by the compatibility
  compiler, then run through the typed engine; compatibility validation is
  stricter and rejects incomplete or disconnected graphs;
* native typed graphs (typed node ``type``s, ``from_port``/``to_port`` edges)
  are validated and built directly.

`GraphValidationError` is the public validation error shared by this dispatcher
and the typed validator. The graph imports are deferred into the function to
avoid an import cycle
(`validation.py` -> `block_assembler.GraphValidationError`).
"""

from __future__ import annotations

from pydantic import ValidationError

from koval.strategy.base.declarative import DeclarativeStrategy


class GraphValidationError(ValueError):
    """Raised when a block graph fails structural or schema validation."""


def assemble_from_graph(graph: dict) -> DeclarativeStrategy:
    """Validate `graph` and return a runnable `DeclarativeStrategy` instance.

    Legacy linear-AND graphs are compiled into the typed dataflow engine; native
    typed graphs are built directly. Raises `GraphValidationError` on any
    structural or schema-level problem; callers may surface the message
    verbatim, so keep them specific.
    """
    if not isinstance(graph, dict):
        raise GraphValidationError("Graph must be an object")

    import koval.strategy.nodes  # noqa: F401 — registers nodes into NODE_CATALOG
    from koval.strategy.graph.compat import (
        compile_legacy,
        extract_dynamic_exit,
        is_legacy_graph,
    )
    from koval.strategy.graph.strategy import build_graph_strategy
    from koval.strategy.graph.validation import validate_graph

    try:
        if is_legacy_graph(graph):
            typed = compile_legacy(graph)
            dynamic_exit = extract_dynamic_exit(graph)
        else:
            typed = graph
            dynamic_exit = None
        validate_graph(typed)
    except GraphValidationError:
        raise
    except (ValidationError, TypeError, KeyError, AttributeError) as exc:
        raise GraphValidationError(f"Invalid graph: {exc}") from exc
    return build_graph_strategy(typed, dynamic_exit)()
