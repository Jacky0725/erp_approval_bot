from pathlib import Path
import subprocess
import sys

import yaml


def test_packaged_defaults_do_not_carry_operator_state(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "operator.yaml"
    destination = tmp_path / "stage" / "settings.yaml"
    original = {
        "app": {"dry_run": False},
        "workbuddy": {"executable": "D:/Dev/WorkBuddy.exe", "project_name": "Operator project",
                      "desktop_enabled": True, "write_enabled": True, "auto_recover": True, "batch_size": 20},
        "approval": {"write_mode": "generate_library", "auto_pass": True},
        "scheduler": {"enabled": True, "auto_pass": True},
        "erp_api": {"enabled_for_write": True, "base_url": "https://example.invalid"},
        "dingtalk_stream": {"enabled": True, "corp_id": "operator-corp"},
    }
    source.write_text(yaml.safe_dump(original), encoding="utf-8")
    subprocess.run([sys.executable, str(root / "packaging/windows/build_runtime_defaults.py"), str(source), str(destination)], check=True)
    assert yaml.safe_load(source.read_text(encoding="utf-8")) == original
    actual = yaml.safe_load(destination.read_text(encoding="utf-8"))
    assert actual["app"]["dry_run"] is True
    assert actual["approval"]["write_mode"] == "disabled"
    assert not actual["approval"]["auto_pass"]
    assert not actual["scheduler"]["enabled"]
    assert not actual["erp_api"]["enabled_for_write"]
    assert not actual["dingtalk_stream"]["enabled"]
    assert not actual["dingtalk_stream"]["corp_id"]
    workbuddy = actual["workbuddy"]
    assert not any(workbuddy[key] for key in ("executable", "project_name", "desktop_enabled", "write_enabled", "auto_recover"))
    assert workbuddy["batch_dir"] == "data/workbuddy"
    assert workbuddy["batch_size"] == 20
