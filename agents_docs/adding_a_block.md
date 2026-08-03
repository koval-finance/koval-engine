# Adding a block

Purpose: the most common contribution, end to end. A block is four pieces;
all four ship together or the block does not exist.

## The four pieces

1. **Pure helper** — `src/koval/strategy/helpers/<family>/<name>.py`. Arrays
   and parameters in, values out. No framework imports, no I/O, no state.
   Arrays are chronological; `arr[-1]` is the current bar.
2. **Parameter schema** — a Pydantic model in
   `src/koval/strategy/schemas.py`, following the naming of the existing
   models there.
3. **Catalogue entry** — a `BlockSpec` in `src/koval/strategy/registry.py`
   wiring the name, schema, and helper together.
4. **Typed node** — in `src/koval/strategy/nodes/`, declaring domain and
   ports; see [graph_contracts.md](graph_contracts.md) for what those mean.

## Order of work

1. Write the helper's tests first —
   `tests/strategy/helpers/<family>/test_<name>.py`. Cover the calculation's
   boundaries: empty input, the exact threshold, both sides of the edge —
   not just a happy path. Watch them fail.
2. Implement the helper until they pass.
3. Add the schema, the `BlockSpec`, and the node, each with tests beside the
   existing ones (`tests/strategy/test_schemas.py`,
   `tests/strategy/test_registry_blocks.py`, `tests/strategy/nodes/`).
4. Run `./scripts/verify.sh`.
5. Check the result: `.venv/bin/koval blocks` — the catalogue is generated
   from the code, so a correctly registered block appears with no extra step.

## Copy an existing block

The fastest reliable path is to read one existing block across all four
files and mirror it. The filter families are compact examples:
`src/koval/strategy/helpers/filters/momentum.py` and
`src/koval/strategy/helpers/filters/trend.py`, with their schema, registry,
and node counterparts.

Update this file when: the set of pieces changes, a file in the list moves,
or the registration flow changes.
