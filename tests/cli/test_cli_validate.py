import json

from koval.cli import main

VALID_GRAPH = {
    "blocks": [
        {"id": "sig", "type": "signal.ema_cross", "params": {"fast": 9, "slow": 21}},
        {"id": "ent", "type": "entry.long_only", "params": {"entry_type": "market"}},
        {"id": "ex", "type": "exit.fixed_sl_tp", "params": {"sl_pct": 2.0, "risk_reward": 2.0}},
        {"id": "rsk", "type": "risk.pct_risk", "params": {"risk_pct": 1.0}},
    ],
    "connections": [
        {"from": "sig", "to": "ent"},
        {"from": "ent", "to": "ex"},
        {"from": "ex", "to": "rsk"},
    ],
}


def _write(tmp_path, payload):
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_validate_accepts_a_valid_graph(tmp_path, capsys):
    assert main(["validate", _write(tmp_path, VALID_GRAPH)]) == 0
    assert "valid" in capsys.readouterr().out.lower()


def test_validate_rejects_an_invalid_graph_with_exit_code_1(tmp_path, capsys):
    broken = {"blocks": [{"id": "sig", "type": "no.such.block", "params": {}}], "connections": []}
    assert main(["validate", _write(tmp_path, broken)]) == 1
    assert "no.such.block" in capsys.readouterr().err


def test_validate_reports_a_missing_file_without_a_traceback(capsys):
    assert main(["validate", "/nonexistent/graph.json"]) == 1
    assert "not found" in capsys.readouterr().err.lower()


def test_validate_reports_malformed_json_without_a_traceback(tmp_path, capsys):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    assert main(["validate", str(path)]) == 1
    assert "json" in capsys.readouterr().err.lower()
