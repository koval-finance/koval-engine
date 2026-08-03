"""Argument parsing and command dispatch for the ``koval`` console script."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from koval import __version__
from koval.exchanges import get_exchange_adapter
from koval.exchanges.ohlcv_cache import OhlcvCache


def _load_graph(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _cmd_blocks(args: argparse.Namespace) -> int:
    from koval.strategy.registry import BLOCK_CATALOG

    specs = sorted(BLOCK_CATALOG.values(), key=lambda spec: spec.type)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "type": spec.type,
                        "category": str(spec.category),
                        "display_name": spec.display_name,
                        "description": spec.description,
                    }
                    for spec in specs
                ],
                indent=2,
            )
        )
        return 0
    width = max(len(spec.type) for spec in specs)
    for spec in specs:
        print(f"{spec.type:<{width}}  {spec.description}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    from koval.strategy.block_assembler import GraphValidationError, assemble_from_graph

    try:
        payload = _load_graph(args.graph)
    except FileNotFoundError:
        print(f"error: graph file not found: {args.graph}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: {args.graph} is not valid JSON: {exc}", file=sys.stderr)
        return 1
    try:
        assemble_from_graph(payload.get("graph", payload))
    except GraphValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("graph is valid")
    return 0


def _cmd_examples(args: argparse.Namespace) -> int:
    import shutil

    from koval.examples import EXAMPLES_DIR

    if args.copy is None:
        print(EXAMPLES_DIR)
        return 0

    destination = Path(args.copy).expanduser() / "koval-examples"
    if destination.exists():
        print(
            f"error: {destination} already exists; remove it or choose another directory",
            file=sys.stderr,
        )
        return 1
    shutil.copytree(EXAMPLES_DIR, destination, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    print(f"copied bundled examples to {destination}")
    return 0


def _cmd_backtest(args: argparse.Namespace) -> int:
    from koval.cli.data import load_csv_candles
    from koval.engine.backtest_engine import (
        EngineRunSpec,
        NoBacktestEngineError,
        load_backtest_engine,
    )
    from koval.engine.time_range import date_to_ms
    from koval.strategy.block_assembler import GraphValidationError

    try:
        payload = _load_graph(args.graph)
    except FileNotFoundError:
        print(f"error: graph file not found: {args.graph}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: {args.graph} is not valid JSON: {exc}", file=sys.stderr)
        return 1
    graph = payload.get("graph", payload)

    if args.data and args.symbol:
        print("error: pass either --data or --symbol, not both", file=sys.stderr)
        return 1
    if not args.data and not args.symbol:
        print(
            "error: pass either --data <csv> for an offline run "
            "or --symbol <symbol> to fetch candles",
            file=sys.stderr,
        )
        return 1

    if args.data:
        try:
            candles = load_csv_candles(Path(args.data))
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        if not (args.start_date and args.end_date):
            print("error: --symbol requires --from and --to", file=sys.stderr)
            return 1
        cache = OhlcvCache(root=Path(args.cache_dir))
        try:
            candles = cache.get(
                get_exchange_adapter(args.exchange),
                exchange=args.exchange,
                symbol=args.symbol,
                timeframe=args.timeframe,
                start_ms=date_to_ms(args.start_date),
                end_ms=date_to_ms(args.end_date),
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if candles.shape[0] == 0:
            print(
                f"error: no candles for {args.symbol} {args.timeframe} in the requested range",
                file=sys.stderr,
            )
            return 1

    spec = EngineRunSpec(
        graph=graph,
        feeds={args.timeframe: candles},
        initial_capital=args.capital,
        execution_config={"exchange": args.exchange, "exchange_type": "future"},
    )
    override = os.getenv("KOVAL_BACKTEST_ENGINE")
    try:
        engine = load_backtest_engine()
    except NoBacktestEngineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ImportError as exc:
        print(
            f"error: cannot import the backtest engine module '{override}' "
            f"named by KOVAL_BACKTEST_ENGINE: {exc}",
            file=sys.stderr,
        )
        return 1
    except AttributeError:
        print(
            f"error: module '{override}' named by KOVAL_BACKTEST_ENGINE "
            "does not define create_engine()",
            file=sys.stderr,
        )
        return 1

    try:
        result = engine.run(spec)
    except GraphValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps({"metrics": result.metrics, "trades": result.trades}, indent=2, default=str)
        )
        return 0
    for key in sorted(result.metrics):
        print(f"{key:<24} {result.metrics[key]}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="koval", description="Koval trading-strategy engine")
    parser.add_argument("--version", action="version", version=f"koval-engine {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    blocks = subparsers.add_parser("blocks", help="list the available strategy blocks")
    blocks.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    blocks.set_defaults(handler=_cmd_blocks)

    validate = subparsers.add_parser("validate", help="validate a strategy graph file")
    validate.add_argument("graph", help="path to a strategy graph JSON file")
    validate.set_defaults(handler=_cmd_validate)

    examples = subparsers.add_parser("examples", help="locate or copy the bundled examples")
    examples.add_argument(
        "--copy",
        metavar="DIR",
        help="copy the bundled examples into DIR/koval-examples/ so they can be edited",
    )
    examples.set_defaults(handler=_cmd_examples)

    backtest = subparsers.add_parser("backtest", help="run a backtest over OHLCV candles")
    backtest.add_argument("graph", help="path to a strategy graph JSON file")
    backtest.add_argument("--data", help="path to an OHLCV CSV file (offline run)")
    backtest.add_argument("--symbol", help="symbol to fetch, e.g. BTCUSDT")
    backtest.add_argument("--from", dest="start_date", help="inclusive start date, YYYY-MM-DD")
    backtest.add_argument("--to", dest="end_date", help="exclusive end date, YYYY-MM-DD")
    backtest.add_argument(
        "--cache-dir", default="data_cache", help="directory for cached OHLCV Parquet files"
    )
    backtest.add_argument(
        "--timeframe", required=True, help="timeframe label for the candles, e.g. 1h"
    )
    backtest.add_argument("--capital", type=float, default=10_000.0, help="initial capital")
    backtest.add_argument("--exchange", default="binance", help="venue used for the fee model")
    backtest.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    backtest.set_defaults(handler=_cmd_backtest)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
