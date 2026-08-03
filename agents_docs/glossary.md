# Glossary

Purpose: what this codebase means by its words. Some differ from wider
industry usage; the definitions here win inside this repository.

- **Block** — a unit of strategy logic exposed in the public catalogue: a
  pure helper function plus a parameter schema, a registry entry, and a typed
  node. `koval blocks` lists all of them.
- **Node** — the typed-graph wrapper around a helper. Nodes declare input and
  output ports and belong to exactly one domain. Defined under
  `src/koval/strategy/nodes/`.
- **Graph** — a strategy expressed as JSON: nodes, parameters, and edges.
  Assembled into a runnable strategy by
  `src/koval/strategy/block_assembler.py`.
- **Domain** — the rank band a node belongs to. Five are author-facing —
  FACT, STATE, POLICY, INTERPRETATION, EXECUTION (FACT and STATE share a
  rank) — plus an internal `SOURCE` band for market data. See
  [graph_contracts.md](graph_contracts.md).
- **Entity** — a typed value travelling on a graph edge, defined in
  `src/koval/strategy/graph/entities.py`.
- **Port** — a node's typed connection point; compatibility is checked at
  validation time (`src/koval/strategy/graph/ports.py`).
- **Fact** — an observation about the market computed from data, carrying no
  opinion (for example: an EMA crossed).
- **Interpretation** — a scored reading of facts that explains *why* the
  strategy acted; surfaced in trade reasoning.
- **Policy** — a rule that turns facts and state into a decision to act.
- **Setup** (`TradeSetup`) — the complete, validated description of an
  intended trade: direction, entry, stop, target, size. Defined in
  `src/koval/strategy/base/trade_setup.py`.
- **Candidate** — a setup that has passed signal logic but not yet the risk
  gates.
- **Bracket** — the stop-loss/take-profit pair attached to every entry. In
  sandbox execution the bracket is mandatory and confirmed immediately after
  a full fill.
- **Containment** — the fail-safe response to a partially filled sandbox
  entry: close what filled rather than adopt an unprotected position.
- **Sandbox** — a venue's non-production environment (Binance Futures
  testnet). The only kind of live execution this package can reach.
- **Paper** — simulated execution against `PaperBroker` with deterministic
  OHLC-bar fills; no venue involved at all.
- **Chronological arrays** — every series a helper receives is oldest-first,
  and `arr[-1]` is the current bar.

Update this file when: a new domain concept enters the public API, or a
term's meaning shifts.
