# Graph contracts

Purpose: the typed dataflow engine's rules. This is the highest-risk area of
the repository — contracts here are public API that third-party graphs and
plugins depend on. Plan first, change little.

## Domains

`Domain` (`src/koval/strategy/graph/domains.py`) has six members, each with a
rank. `SOURCE` (rank 0) is internal — it feeds market data into the graph; no
author-written node currently uses it. The five domains an author writes
nodes for:

| Domain | Role |
|---|---|
| FACT | compute observations from market data — no opinions |
| STATE | remember things across bars |
| POLICY | decide whether and how to act |
| INTERPRETATION | score and explain — the "why" surfaced with a trade |
| EXECUTION | turn a decision into a `TradeSetup` with risk applied |

Domains are rank bands, not a strict pipeline: FACT and STATE share a rank,
so edges between them are legal in either direction (a Liquidity Pool fact
built from events; a Sweep fact born from active pool state). Execution runs
in rank/topological order; an edge may not point to a domain whose rank is
strictly lower than its producer's.

## Where each contract lives

| File | Contract |
|---|---|
| `src/koval/strategy/graph/entities.py` | entity types carried on edges |
| `src/koval/strategy/graph/domains.py` | the domain enum and its ordering |
| `src/koval/strategy/graph/ports.py` | port declarations and compatibility |
| `src/koval/strategy/graph/node.py` | the node ABC |
| `src/koval/strategy/graph/registry.py` | node registration |
| `src/koval/strategy/graph/validation.py` | structural checks: cycles, port compatibility, domain ordering |
| `src/koval/strategy/graph/executor.py` | runtime execution and emitted-entity checking |
| `src/koval/strategy/graph/state_store.py` | per-node persistent state |
| `src/koval/strategy/graph/compat.py` | the compatibility catalogue mapping blocks onto typed nodes |
| `src/koval/strategy/graph/strategy.py` | the graph-backed strategy |

## Validation boundaries

- **Assembly time** (`validation.py`): structure, port compatibility, domain
  ordering, cycle detection. A bad graph fails with `GraphValidationError`
  before any bar runs.
- **Runtime** (`executor.py`): each node's emitted entities are checked
  against its declared output ports. A node lying about its outputs fails
  loudly, not silently.

## Rules

- Never widen a port type or an entity contract to make one graph pass; that
  weakens every graph. Fix the graph, or add an explicit new port.
- A new node needs tests at both boundaries — a validation test and an
  executor test — plus golden coverage if it changes an existing preset's
  behaviour — see [testing.md](testing.md).
- The compatibility catalogue in `compat.py` is a public promise; changing an
  existing mapping is a breaking change and needs a changelog entry.

Update this file when: a domain, entity, or port contract changes; validation
gains or loses a check.
