import importlib.util
import json
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("data-quality-gate.py")
SPEC = importlib.util.spec_from_file_location("data_quality_gate", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _write_report(root, *, date="2026-08-08", guild_override=None):
    headers = [
        "sid",
        "create_date(day)",
        "guild_name",
        "level",
        "online_minute",
        "all_diamond_amount",
    ]
    for label, tokens in MODULE.EXPECTED.items():
        guild = guild_override or tokens[0]
        payload = {
            "headers": headers,
            "rows": [
                {
                    "sid": "sid-1",
                    "create_date(day)": date.replace("-", ""),
                    "guild_name": guild,
                    "level": "A",
                    "online_minute": 1,
                    "all_diamond_amount": 2,
                }
            ],
        }
        (root / f"{label}_20260808.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


def test_quality_gate_passes_complete_reports(tmp_path, monkeypatch, capsys):
    _write_report(tmp_path)
    monkeypatch.setattr("sys.argv", ["data-quality-gate.py", str(tmp_path), "2026-08-08"])
    assert MODULE.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"


def test_quality_gate_fails_wrong_date(tmp_path, monkeypatch, capsys):
    _write_report(tmp_path, date="2026-08-07")
    monkeypatch.setattr("sys.argv", ["data-quality-gate.py", str(tmp_path), "2026-08-08"])
    assert MODULE.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "FAIL"
    assert any("目标日期" in failure for failure in result["failures"])
