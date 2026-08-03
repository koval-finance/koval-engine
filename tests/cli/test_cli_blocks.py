import json

from koval.cli import main
from koval.strategy.registry import BLOCK_CATALOG


def test_blocks_lists_every_registered_block(capsys):
    exit_code = main(["blocks", "--json"])
    captured = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert {entry["type"] for entry in captured} == set(BLOCK_CATALOG)


def test_blocks_human_output_includes_type_and_description(capsys):
    exit_code = main(["blocks"])
    out = capsys.readouterr().out

    assert exit_code == 0
    sample = next(iter(BLOCK_CATALOG.values()))
    assert sample.type in out
    assert sample.description[:20] in out
