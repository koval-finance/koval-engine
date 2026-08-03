# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| `0.9.x` | Yes |
| < `0.9` | No |

This project is pre-1.0. Fixes land on the latest `0.9.x` release; there are no backports.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Use GitHub's private reporting: go to the [Security tab](https://github.com/koval-finance/koval-engine/security/advisories/new) and choose "Report a vulnerability". This opens a private advisory visible only to the maintainers.

If you cannot use GitHub, email **security@koval.finance**.

Please include what you found, how to reproduce it (a graph JSON or CLI invocation is ideal), what you expected instead, and the `koval-engine` and Python versions.

### What to expect

- Acknowledgement within 7 days.
- An assessment, with a severity and a rough timeline, within 30 days.
- Credit in the advisory and changelog, unless you prefer otherwise.

This is a small project with best-effort maintenance. If you have not heard back within 7 days, feel free to send a reminder.

## The invariant we most want broken

The engine claims to have **no code path that reaches a real-money trading endpoint**. Only `paper` and `binance_sandbox` are reachable execution modes. The Binance broker accepts only its allowlisted futures-testnet origin, and WhiteBIT execution is disabled because no verified public sandbox exists.

The claim rests on three mechanisms:

- `koval.exchanges.binance_sandbox._ALLOWED_BASE_URLS` — the broker refuses to construct against any base URL outside the futures testnet.
- `WhiteBITSandboxBroker` — fail-closed: construction always raises until a verified public non-money endpoint and contract exist.
- `sandbox_factory._ALLOWED_MODES` — the factory raises on any mode outside the allowlist.

These are pinned by [`tests/safety/`](tests/safety/), which exists so a reader can check the guarantee rather than take it on faith.

**A demonstration that those tests can pass while a real-money endpoint remains reachable is the highest-severity report this project accepts.** That includes reaching a production endpoint through configuration, environment variables, credential handling, URL normalisation, redirects, a supplied `session` object, or any path that bypasses the broker constructors entirely. Please report it privately.

## Scope

In scope: anything in this repository — the engine, the strategy graph executor, the exchange adapters and sandbox brokers, the CLI, and the release pipeline.

Out of scope: the hosted product at koval.finance (report those to security@koval.finance separately), independently distributed backtest-engine plugins, vulnerabilities in third-party dependencies without a demonstrated impact here, and results that depend on an attacker already having arbitrary code execution on the machine running the engine.

Note that a strategy graph is data, but the engine executes it. Running an untrusted graph is not a supported threat model — treat graph files like scripts.
