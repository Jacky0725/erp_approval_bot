from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pandas as pd
from openpyxl import load_workbook
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from workbuddy_bridge import RESULT_COLUMNS, WorkBuddyBridge
from reagent_memory import ReagentMemory


@pytest.fixture
def bridge(tmp_path):
    (tmp_path / "config").mkdir()
    # Rule payload is opaque to the bridge, not parsed as decision logic.
    (tmp_path / "config" / "rules_structured.xlsx").write_bytes(b"rule snapshot")
    return WorkBuddyBridge(tmp_path, {})


def records(count=2):
    return [{"清单号": "SJ001", "_erp_record_id": str(i), "_erp_parent_state": "wait", "试剂名称": f"样品{i}", "CAS号": "", "序号": str(i), "物化特性": "-"} for i in range(1, count + 1)]


def results(bridge, state, category="普通类"):
    rows = [{"批次ID": state["batch_id"], "记录ID": i["key"], "试剂名称": i["input"]["试剂名称"], "物化特性类别": category, "判断依据": "依据本批规则"} for i in state["items"]]
    pd.DataFrame(rows, columns=RESULT_COLUMNS).to_excel(bridge.batch_path(state) / "result.xlsx", index=False)
    return rows


class Client:
    def __init__(self, source):
        self.records = deepcopy(source)
        self.writes = []
        self.fail = False
        self.unknown = False

    def fetch_reagent_detail(self, number):
        return deepcopy(self.records)

    def save_physicochemical_property_by_id(self, key, target, identity, record):
        self.writes.append(key)
        if self.fail:
            return SimpleNamespace(saved=False, verified=False, detail="ERP category is not available through JSON-RPC: 普通类")
        next(i for i in self.records if i["_erp_record_id"] == key)["物化特性"] = target
        if self.unknown:
            raise TimeoutError("response lost")
        return SimpleNamespace(saved=True, verified=True, detail="saved and verified")


def test_prepare_limit_resume_and_duplicate_names(bridge):
    source = records(25)
    source[1]["试剂名称"] = source[0]["试剂名称"]
    state = bridge.prepare(source)
    assert len(state["items"]) == 20
    assert state["items"][0]["key"] != state["items"][1]["key"]
    assert bridge.prepare(source)["batch_id"] == state["batch_id"]
    assert (bridge.batch_path(state) / "rules.xlsx").read_bytes() == b"rule snapshot"


def test_prepare_stops_reading_after_twenty_records(bridge):
    def source():
        yield from records(20)
        pytest.fail("prepared a batch but continued fetching later ERP records")
    assert len(bridge.prepare(source())["items"]) == 20


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "foreign", "name", "formula", "headers"])
def test_corrupt_result_never_imported(bridge, corruption):
    state = bridge.prepare(records())
    rows = results(bridge, state)
    if corruption == "missing":
        rows.pop()
    elif corruption == "duplicate":
        rows[1] = rows[0]
    elif corruption == "foreign":
        rows[0]["批次ID"] = "another batch"
    elif corruption == "name":
        rows[0]["试剂名称"] = "changed"
    elif corruption == "formula":
        rows[0]["判断依据"] = "=1+1"
    columns = RESULT_COLUMNS if corruption != "headers" else RESULT_COLUMNS[::-1]
    pd.DataFrame(rows, columns=columns).to_excel(bridge.batch_path(state) / "result.xlsx", index=False)
    with pytest.raises(ValueError):
        bridge.load_results(state)
    assert all("result" not in i for i in state["items"])


def test_reviewed_row_only_reconciles_after_explicit_confirmation_and_erp_readback(bridge):
    source = records(2)
    state = bridge.prepare(source)
    results(bridge, state, "需人工复核")
    client = Client(source)
    library_calls = []
    client.generate_reagent_library_by_id = lambda key, *_: (
        library_calls.append(key) or {"status": "created_verified", "detail": "模拟回读"}
    )
    memory = ReagentMemory(bridge.root_dir)
    bridge.process(state, client, memory, dry_run=False, generate_library=True)
    assert state["status"] == "review_pending"
    item = state["items"][0]
    path = bridge.root_dir / "data" / "workbuddy_review_confirmations" / f"{state['batch_id']}.json"
    path.parent.mkdir()
    path.write_text(json.dumps({item["key"]: "普通类"}, ensure_ascii=False), encoding="utf-8")
    bridge.process(state, client, memory, dry_run=False)
    with pytest.raises(ValueError, match="人工保存类别未核对"):
        bridge.process(state, client, memory, dry_run=False, confirm_reviews=True)
    assert not client.writes and not library_calls
    bridge.rules_path().write_bytes(b"new formal rules")
    client.records[0]["物化特性"] = "普通类"
    bridge.process(state, client, memory, dry_run=False, confirm_reviews=True)
    bridge.process(state, client, memory, dry_run=False, confirm_reviews=True)
    assert state["status"] == "review_pending"
    assert item["result"]["物化特性类别"] == "需人工复核"
    assert item["operator_confirmed_category"] == "普通类"
    assert state["items"][1]["status"] == "review"
    assert library_calls == ["1"] and not client.writes


def test_reviewed_erp_record_is_not_rebatched_after_rules_or_identity_change(bridge):
    source = records(1)
    state = bridge.prepare(source)
    results(bridge, state, "需人工复核")
    bridge.process(state, Client(source), ReagentMemory(bridge.root_dir), dry_run=False)
    bridge.rules_path().write_bytes(b"new rules")
    source[0]["试剂名称"] = "人工核对前已有名称更新"
    assert bridge.prepare(source) is None


def test_model_review_does_not_hide_an_uncertain_library_write(bridge):
    source = records(2)
    state = bridge.prepare(source)
    rows = results(bridge, state)
    rows[1]["物化特性类别"] = "需人工复核"
    pd.DataFrame(rows, columns=RESULT_COLUMNS).to_excel(bridge.batch_path(state) / "result.xlsx", index=False)
    client = Client(source)
    calls = []

    def generate(key, *_):
        calls.append(key)
        raise TimeoutError("试剂库请求响应丢失")

    client.generate_reagent_library_by_id = generate
    memory = ReagentMemory(bridge.root_dir)
    bridge.process(state, client, memory, dry_run=False, generate_library=True)
    bridge.process(state, client, memory, dry_run=False)
    assert state["status"] == "needs_attention"
    assert state["items"][0]["library"]["status"] == "failed"
    assert state["items"][1]["status"] == "review"
    assert calls == client.writes == ["1"]
    with pytest.raises(RuntimeError, match="1 条写入或入库待核对"):
        bridge.require_safe_continuation(state, {"SJ001"})


def test_dry_run_never_writes_memory_or_erp(bridge):
    source = records()
    state = bridge.prepare(source)
    results(bridge, state)
    client = Client(source)
    memory = ReagentMemory(bridge.root_dir)
    bridge.process(state, client, memory)
    assert not client.writes
    assert not memory.path.exists()
    assert (bridge.batch_path(state) / "result.xlsx").exists()


def test_success_stores_provenance_and_cleans_only_result(bridge):
    source = records()
    state = bridge.prepare(source)
    results(bridge, state)
    memory = ReagentMemory(bridge.root_dir)
    client = Client(source)
    bridge.process(state, client, memory, dry_run=False)
    assert state["status"] == "completed"
    assert client.writes == ["1", "2"]
    assert not (bridge.batch_path(state) / "result.xlsx").exists()
    assert (bridge.batch_path(state) / "input.xlsx").exists()
    with sqlite3.connect(memory.path) as conn:
        rows = conn.execute("SELECT reusable, manual_verified, reason FROM reagent_memory").fetchall()
    assert len(rows) == 2
    assert all(r[0] == r[1] == 0 and "ERP同步=成功" in r[2] for r in rows)
    bridge.process(state, client, memory, dry_run=False)
    assert client.writes == ["1", "2"]


def test_unmapped_category_preserved_and_no_erp_write(bridge):
    source = records()
    state = bridge.prepare(source)
    results(bridge, state, "not an ERP category")
    client = Client(source)
    bridge.process(state, client, ReagentMemory(bridge.root_dir), dry_run=False)
    assert not client.writes
    assert all(i["error_code"] == "CATEGORY_MAPPING" for i in state["items"])
    assert (bridge.batch_path(state) / "result.xlsx").exists()


def test_response_lost_reconciles_without_duplicate_write(bridge):
    source = records(1)
    state = bridge.prepare(source)
    results(bridge, state)
    client = Client(source)
    client.unknown = True
    memory = ReagentMemory(bridge.root_dir)
    bridge.process(state, client, memory, dry_run=False)
    assert state["items"][0]["status"] == "failed"
    # Reload persisted state, as after worker/process restart.
    state = bridge.batches()[0]
    bridge.process(state, client, memory, dry_run=False)
    assert state["status"] == "completed"
    assert client.writes == ["1"]


def test_changed_erp_identity_or_property_never_overwritten(bridge):
    source = records()
    state = bridge.prepare(source)
    results(bridge, state)
    client = Client(source)
    client.records[0]["CAS号"] = "changed"
    client.records[1]["物化特性"] = "高毒类"
    bridge.process(state, client, ReagentMemory(bridge.root_dir), dry_run=False)
    assert not client.writes
    assert {i["error_code"] for i in state["items"]} == {"IDENTITY_CHANGED", "PROPERTY_CHANGED"}


def test_retry_limit_and_erp_category_error(bridge):
    source = records(1)
    state = bridge.prepare(source)
    results(bridge, state)
    client = Client(source)
    client.fail = True
    memory = ReagentMemory(bridge.root_dir)
    for _ in range(3):
        bridge.process(state, client, memory, dry_run=False)
    assert len(client.writes) == 2
    assert state["items"][0]["error_code"] == "ERP_CATEGORY_UNAVAILABLE"
    assert (bridge.batch_path(state) / "result.xlsx").exists()


def test_result_mutation_after_import_blocked(bridge):
    state = bridge.prepare(records())
    results(bridge, state)
    bridge.load_results(state)
    path = bridge.batch_path(state) / "result.xlsx"
    workbook = load_workbook(path)
    workbook.active["D2"] = "高毒类"
    workbook.save(path)
    workbook.close()
    with pytest.raises(ValueError, match="发生变化"):
        bridge.load_results(state)


def test_model_memory_does_not_modify_existing_verified_entry(bridge):
    memory = ReagentMemory(bridge.root_dir)
    memory.add_record(raw_name="样品1", final_category="普通类", source="manual", manual_verified=True)
    old = memory.find_any(raw_name="样品1")
    state = bridge.prepare(records(1))
    results(bridge, state, "高毒类")
    bridge.load_results(state)
    item = state["items"][0]
    memory.record_workbuddy_result(state["batch_id"], item["key"], item["input"], item["result"])
    preserved = memory.find_by_id(old["id"])
    assert preserved["source"] == "manual"
    assert preserved["manual_verified"] == preserved["reusable"] == 1
    assert preserved["final_category"] == "普通类"


def test_blocked_mapping_can_be_retried_after_mapping_fix(bridge):
    source = records(1)
    state = bridge.prepare(source)
    results(bridge, state, "测试别名")
    client = Client(source)
    memory = ReagentMemory(bridge.root_dir)
    bridge.process(state, client, memory, dry_run=False)
    assert state["items"][0]["status"] == "blocked"
    bridge.settings["reagent"] = {"physicochemical_property_aliases": {"测试别名": ["普通类"]}}
    bridge.process(state, client, memory, dry_run=False)
    assert state["status"] == "completed"
    assert client.writes == ["1"]


@pytest.mark.parametrize("parent_state", ["done", "", None])
def test_finished_or_unreadable_parent_cannot_be_written(bridge, parent_state):
    source = records(1)
    state = bridge.prepare(source)
    results(bridge, state)
    client = Client(source)
    if parent_state is None:
        client.records[0].pop("_erp_parent_state")
    else:
        client.records[0]["_erp_parent_state"] = parent_state
    bridge.process(state, client, ReagentMemory(bridge.root_dir), dry_run=False)
    assert not client.writes
    assert state["items"][0]["error_code"] == "TASK_NOT_PENDING"


def test_modified_rules_rejected(bridge):
    state = bridge.prepare(records(1))
    results(bridge, state)
    (bridge.batch_path(state) / "rules.xlsx").write_bytes(b"changed")
    with pytest.raises(ValueError, match="规则快照"):
        bridge.load_results(state)


def test_os_lock_rejects_overlapping_worker(bridge):
    with bridge.exclusive():
        with pytest.raises(RuntimeError, match="另一个"):
            with bridge.exclusive():
                pytest.fail("concurrent worker acquired lock")
    with bridge.exclusive():
        pass


def test_project_prompt_only_sends_batch_directory(bridge):
    bridge.config["prompt_mode"] = "project"
    state = bridge.prepare(records(1))
    prompt = (bridge.batch_path(state) / "prompt.txt").read_text(encoding="utf-8")
    assert "WB-EXCEL-v1" in prompt and str(bridge.batch_path(state)) in prompt
    assert "判断依据" not in prompt and "rules.xlsx" not in prompt


def test_new_official_rules_block_old_results_before_memory_or_write(bridge):
    state = bridge.prepare(records(1))
    results(bridge, state)
    bridge.rules_path().write_bytes(b"new reviewed rules")
    with pytest.raises(ValueError, match="正式规则已更新"):
        bridge.process(state, object(), object(), dry_run=False)
    assert state["items"][0]["attempts"] == 0


def test_new_rules_allow_reclassification_without_deleting_old_batch(bridge):
    old = bridge.prepare(records(1))
    bridge.rules_path().write_bytes(b"new reviewed rules")
    new = bridge.prepare(records(1))
    assert new["batch_id"] != old["batch_id"]
    assert new["items"][0]["key"] == old["items"][0]["key"]
    assert (bridge.batch_path(old) / "state.json").exists()

@pytest.mark.parametrize('library_status',['created_verified','existing_unchanged','needs_confirmation','unverified','failed'])
def test_library_state_is_separate_and_never_repeated(bridge,library_status):
    source=records(1)
    state=bridge.prepare(source)
    results(bridge,state)
    client=Client(source)
    calls=[]
    def generate(key,identity,category):
        calls.append(key)
        assert client.records[0]['物化特性']==category
        if library_status=='failed':raise TimeoutError('library response lost')
        return {'status':library_status,'detail':'business reason'}
    client.generate_reagent_library_by_id=generate
    memory=ReagentMemory(bridge.root_dir)
    bridge.process(state,client,memory,dry_run=False,generate_library=True)
    assert state['items'][0]['status']=='saved'
    assert state['items'][0]['library']['status']==library_status
    complete=library_status in {'created_verified','existing_unchanged'}
    assert (state['status']=='completed')==complete
    assert (bridge.batch_path(state)/'result.xlsx').exists()!=complete
    bridge.process(bridge.batches()[0],client,memory,dry_run=False,generate_library=False)
    assert calls==['1'] and client.writes==['1']


def test_library_is_not_generated_when_category_save_fails(bridge):
    source=records(1);state=bridge.prepare(source);results(bridge,state)
    client=Client(source);client.fail=True
    client.generate_reagent_library_by_id=lambda *args:pytest.fail('not saved')
    bridge.process(state,client,ReagentMemory(bridge.root_dir),dry_run=False,generate_library=True)
    assert 'library' not in state['items'][0]


def test_match_checkpoint_prevents_repeat_and_dry_run_is_read_only(bridge):
    source=records(1);client=Client(source);calls=[]
    client.match_reagent_list=lambda number:calls.append(number) or {'changes':[],'records':deepcopy(source)}
    assert bridge.matched_records(client,'SJ001',dry_run=True)==source
    assert not calls
    bridge.matched_records(client,'SJ001',dry_run=False)
    bridge.matched_records(client,'SJ001',dry_run=False)
    assert calls==['SJ001']


def test_match_response_lost_is_not_repeated(bridge):
    client=Client(records(1));calls=[]
    def match(number):
        calls.append(number)
        raise TimeoutError('response lost')
    client.match_reagent_list=match
    with pytest.raises(TimeoutError):bridge.matched_records(client,'SJ001',dry_run=False)
    with pytest.raises(RuntimeError,match='待核对'):bridge.matched_records(client,'SJ001',dry_run=False)
    assert len(calls)==1


def test_unsent_stale_batch_retains_evidence_and_requeues_only_empty(bridge):
    source=records(2);state=bridge.prepare(source);client=Client(source)
    client.records[0]['物化特性']='普通类'
    assert bridge.reconcile_unsent_batch(state,client)
    assert state['status']=='superseded_before_dispatch'
    assert (bridge.batch_path(state)/'input.xlsx').exists()
    new=bridge.prepare(client.records)
    assert [i['input']['_erp_record_id'] for i in new['items']]==['2']


@pytest.mark.parametrize('dispatch',['sending','submitted'])
def test_sent_batch_is_never_superseded(bridge,dispatch):
    source=records(1);state=bridge.prepare(source);state['dispatch_state']=dispatch
    client=Client(source);client.records[0]['物化特性']='普通类'
    assert bridge.reconcile_unsent_batch(state,client) is False
    assert state['status']=='waiting_result'


def test_checkpoint_sharing_violation_retries_only_local_replace(bridge,monkeypatch):
    original=Path.replace;calls=[]
    def replace(path,target):
        calls.append(str(path))
        if len(calls)<3:raise PermissionError('sharing violation')
        return original(path,target)
    monkeypatch.setattr(Path,'replace',replace)
    monkeypatch.setattr('workbuddy_bridge.time.sleep',lambda duration:None)
    state=bridge.prepare(records(1))
    assert len(calls)==3 and bridge.batches()[0]['batch_id']==state['batch_id']


def test_checkpoint_permanent_access_error_is_bounded(bridge,monkeypatch):
    calls=[]
    def replace(path,target):
        calls.append(str(path));raise PermissionError('denied')
    monkeypatch.setattr(Path,'replace',replace)
    monkeypatch.setattr('workbuddy_bridge.time.sleep',lambda duration:None)
    with pytest.raises(PermissionError):bridge.prepare(records(1))
    assert len(calls)==6


@pytest.mark.parametrize('size',[30,40,50,100])
def test_larger_batches_round_trip_and_stop_at_configured_size(bridge,size):
    bridge.config['batch_size']=size
    def source():
        yield from records(size)
        pytest.fail('batch consumed more than the configured limit')
    state=bridge.prepare(source())
    assert len(state['items'])==size and state['batch_size_limit']==size
    results(bridge,state)
    bridge.config['batch_size']=20
    bridge.process(state,Client(records(size)),ReagentMemory(bridge.root_dir),dry_run=False)
    assert state['status']=='completed' and state['cleaned']
    assert len(state['items'])==size


@pytest.mark.parametrize('size',[0,-1,101,True,None,'30.5','bad'])
def test_invalid_batch_sizes_fail_explicitly(bridge,size):
    bridge.config['batch_size']=size
    with pytest.raises(ValueError,match='batch_size'):
        bridge.prepare(records(1))


def test_larger_batch_rejects_foreign_extra_row_before_any_write(bridge):
    bridge.config['batch_size']=30
    state=bridge.prepare(records(30));rows=results(bridge,state)
    rows.append({**rows[-1],'记录ID':'foreign'})
    pd.DataFrame(rows,columns=RESULT_COLUMNS).to_excel(bridge.batch_path(state)/'result.xlsx',index=False)
    client=Client(records(30))
    with pytest.raises(ValueError):bridge.process(state,client,ReagentMemory(bridge.root_dir),dry_run=False)
    assert not client.writes
