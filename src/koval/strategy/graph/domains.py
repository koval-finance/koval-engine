"""Execution domains and the inter-domain flow rule.

Domains are responsibility bands, not a strict pipeline. FACT and STATE are
co-rank so they may feed each other (Liquidity Pool built from events; Sweep
born from an active pool). A consumer may only read from a producer whose rank
is <= its own, which structurally forbids any FACT/STATE/POLICY node from
consuming INTERPRETATION/EXECUTION output.
"""

from __future__ import annotations

from enum import StrEnum


class Domain(StrEnum):
    SOURCE = "source"
    FACT = "fact"
    STATE = "state"
    POLICY = "policy"
    INTERPRETATION = "interpretation"
    EXECUTION = "execution"


_RANK: dict[Domain, int] = {
    Domain.SOURCE: 0,
    Domain.FACT: 1,
    Domain.STATE: 1,  # co-rank with FACT
    Domain.POLICY: 2,
    Domain.INTERPRETATION: 3,
    Domain.EXECUTION: 4,
}


def rank(domain: Domain) -> int:
    return _RANK[domain]


def can_flow(producer: Domain, consumer: Domain) -> bool:
    """True iff an edge producer -> consumer is allowed by domain rank."""
    return _RANK[consumer] >= _RANK[producer]
