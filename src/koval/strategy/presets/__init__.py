"""Preset strategy graphs expressed as Python builders.

Each module exposes a ``build_*_graph()`` function returning a graph dict
accepted by :func:`koval.strategy.block_assembler.assemble_from_graph`.
These builders do not self-register in ``STRATEGY_REGISTRY``.
"""

from __future__ import annotations
