import json
from pathlib import Path
import runpy
import subprocess
import sys
from urllib.request import ProxyHandler, build_opener

import pytest


exercise = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/workbuddy_sandbox_acceptance.py"))


def test_synthetic_exercise_does_not_attest_policy(tmp_path):
    prepared = exercise["prepare"](tmp_path)
    workspace = Path(prepared["workspace"])
    state = json.loads((workspace / "state.json").read_bytes())
    rows = [
        {"记录ID": item["key"], "物化特性类别": category, "判断依据": rule}
        for item, (_, category, rule) in zip(state["items"], exercise["CASES"])
    ]
    (workspace / "judgments.json").write_text(json.dumps(rows), encoding="utf-8")
    with pytest.raises(ValueError, match="matching receipt"):
        exercise["verify"](Path(prepared["root"]), "wrong")
    report = exercise["verify"](Path(prepared["root"]), prepared["receipt"])
    assert report["rows_verified"] == 2 and Path(report["result"]).is_file()
    assert not report["permission_enforcement_verified"]
    assert not report["production_gate_changed"]
    assert exercise["verify"](Path(prepared["root"]), prepared["receipt"]) == report


def test_synthetic_exercise_rejects_canary_changes(tmp_path):
    prepared = exercise["prepare"](tmp_path)
    Path(prepared["protected_canary"]).write_text("changed", encoding="ascii")
    with pytest.raises(ValueError, match="outside-workspace files changed"):
        exercise["verify"](Path(prepared["root"]), prepared["receipt"])


def test_shared_fixture_uses_ordinary_directory_creation(tmp_path, monkeypatch):
    def unexpected_temp_directory(**kwargs):
        pytest.fail("shared fixture must not use private temporary directory creation")

    monkeypatch.setitem(exercise["prepare"].__globals__, "mkdtemp", unexpected_temp_directory)
    prepared = exercise["prepare"](tmp_path, inherit_parent_acl=True)
    assert Path(prepared["root"]).parent == tmp_path.resolve()
    assert Path(prepared["prompt"]).is_file()


def test_audit_counts_only_calls_for_bound_request_and_never_attests(tmp_path):
    prepared = exercise["prepare"](tmp_path)
    workspace = Path(prepared["workspace"])
    transcript = tmp_path / "session.jsonl"
    common = {"sessionId": transcript.stem, "providerData": {"conversationRequestId": "exercise"}}
    prompt = {**common, "type": "message", "role": "user", "timestamp": 1000,
              "content": [{"text": f"批次目录：{workspace}。"}]}
    def call(name, path):
        return {**common, "type": "function_call", "name": name, "arguments": {"file_path": str(path)}}
    rows = [
        prompt, call("Read", workspace / "input.json"), call("Write", workspace / "judgments.json"),
        call("Bash", workspace), call("Read", Path(prepared["protected_canary"])),
        {**call("PowerShell", workspace), "providerData": {"conversationRequestId": "unrelated"}},
    ]
    transcript.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    audit = exercise["audit_json_tool_calls"]
    assert audit(transcript, workspace, 0) == {
        "observed_calls": {"Read": 2, "Write": 1, "Bash": 1},
        "unexpected_calls": {"Bash": 1, "Read": 1},
        "permission_enforcement_verified": False,
    }
    with pytest.raises(ValueError, match="uniquely bind"):
        audit(transcript, workspace, 2)


def test_loopback_probe_excludes_self_check_and_counts_actual_request(tmp_path):
    prepared = exercise["prepare"](tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts/workbuddy_sandbox_acceptance.py"
    with subprocess.Popen([sys.executable, str(script), "network-probe", prepared["root"], "--seconds", "3"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        try:
            assert process.stdout is not None
            notice = json.loads(process.stdout.readline())
            assert notice["baseline_reachable"] and notice["probe_url"].startswith("http://127.0.0.1:")
            with build_opener(ProxyHandler({})).open(notice["probe_url"], timeout=2) as response:
                assert response.status == 204
            _, error = process.communicate(timeout=8)
            assert process.returncode == 0, error
        finally:
            if process.poll() is None:
                process.kill()
    report = json.loads(Path(notice["report"]).read_bytes())
    assert report["observed_request_count"] == 1
    assert not report["permission_enforcement_verified"]
