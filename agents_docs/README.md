# agents_docs — knowledge index

Purpose: the index of the agent-facing documentation. The root
[AGENTS.md](../AGENTS.md) holds what every task needs; these files hold
depth. Load only what the task calls for.

## Files

| File | Owns |
|---|---|
| [invariants.md](invariants.md) | every load-bearing rule and the test that pins it |
| [agent_workflow.md](agent_workflow.md) | the work loop, the git policy, the hard rules |
| [architecture.md](architecture.md) | module map, execution flow, the plugin seam |
| [graph_contracts.md](graph_contracts.md) | domains, ports, entities, validation boundaries |
| [adding_a_block.md](adding_a_block.md) | the four pieces of a block, end to end |
| [testing.md](testing.md) | suite layout, markers, what "tested" means |
| [code_style.md](code_style.md) | conventions ruff cannot enforce |
| [exchanges_and_data.md](exchanges_and_data.md) | adapters, cache, sandbox brokers, safety posture |
| [release_process.md](release_process.md) | version truth, changelog gate, the tag pipeline |
| [troubleshooting.md](troubleshooting.md) | dated failure memory |
| [glossary.md](glossary.md) | what this codebase means by its words |

## Task routing

| Task | Read first |
|---|---|
| Add or change a block | [adding_a_block.md](adding_a_block.md), then [graph_contracts.md](graph_contracts.md) |
| Change graph execution or validation | [graph_contracts.md](graph_contracts.md), then [architecture.md](architecture.md) |
| Exchange adapter, cache, or broker work | [exchanges_and_data.md](exchanges_and_data.md), then [invariants.md](invariants.md) |
| Paper profiles, fills, or live-engine accounting | [architecture.md](architecture.md), then [exchanges_and_data.md](exchanges_and_data.md) |
| Test failure or unexpected behaviour | [troubleshooting.md](troubleshooting.md), then [testing.md](testing.md) |
| Anything touching safety or licensing | [invariants.md](invariants.md) — before writing code |
| Cutting a release | [release_process.md](release_process.md) |
| Unfamiliar term | [glossary.md](glossary.md) |

## Cold-session reading order

1. [AGENTS.md](../AGENTS.md)
2. This index
3. The routed file for your task

## Maintenance

One file owns each topic; cross-link rather than repeat. Every file ends
with an "Update this file when" note — honour it in the same change that
makes it true. Consistency between this index, the files, and the root entry
file is pinned by
[`tests/test_agents_docs.py`](../tests/test_agents_docs.py).
