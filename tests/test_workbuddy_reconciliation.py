from types import SimpleNamespace
from unittest.mock import Mock
import json

import pandas as pd
import pytest

from workbuddy_bridge import WorkBuddyBridge, RESULT_COLUMNS


@pytest.fixture
def bridge(tmp_path):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/rules_structured.xlsx').write_bytes(b'rules')
    return WorkBuddyBridge(tmp_path, {'reagent': {'physicochemical_property_options': ['普通类', '特殊酸', '未知类', '拒收类']}})


def row(key, **changes):
    return {'清单号': 'SJ001', '_erp_record_id': str(key), '序号': str(key),
            '试剂名称': 'sample', 'CAS号': '', '_erp_parent_state': 'wait', '物化特性': '-', **changes}


def load_results(bridge, state, categories):
    pd.DataFrame([{'批次ID': state['batch_id'], '记录ID': item['key'],
                  '试剂名称': item['input']['试剂名称'], '物化特性类别': category, '判断依据': '本批规则依据'}
                 for item, category in zip(state['items'], categories)], columns=RESULT_COLUMNS).to_excel(
                     bridge.batch_path(state) / 'result.xlsx', index=False)


@pytest.mark.parametrize('separate', [False, True])
def test_conflicting_results_block_before_new_writes_across_batches(bridge, separate):
    if separate:
        prior = bridge.prepare([row(1)])
        load_results(bridge, prior, ['普通类'])
        bridge.load_results(prior)
        prior['status'] = 'completed'
        prior['items'][0].update(status='saved', erp_category='普通类')
        bridge.save(prior)
        state = bridge.prepare([row(2)])
        load_results(bridge, state, ['特殊酸'])
    else:
        state = bridge.prepare([row(1), row(2)])
        load_results(bridge, state, ['普通类', '特殊酸'])
    client, memory = Mock(), Mock()
    bridge.process(state, client, memory, dry_run=False)
    client.save_physicochemical_property_by_id.assert_not_called()
    memory.record_workbuddy_result.assert_not_called()
    assert all(i['error_code'] == 'DUPLICATE_CATEGORY_CONFLICT' for i in state['items'])


@pytest.mark.parametrize('difference', ['CAS号', 'type', 'desc', '清单号'])
def test_different_chemical_identity_is_not_merged(bridge, difference):
    state = bridge.prepare([row(1), row(2, **{difference: 'different'})])
    load_results(bridge, state, ['普通类', '特殊酸'])
    bridge.load_results(state)
    assert not bridge.duplicate_result_conflicts(state)


@pytest.mark.parametrize('failure', ['', 'category', 'duplicate', 'identity', 'unverified', 'timeout'])
def test_recheck_refreshes_only_verified_library_prompt_and_preserves_original(bridge, failure):
    source = row(1)
    state = bridge.prepare([source])
    load_results(bridge, state, ['未知类'])
    bridge.load_results(state)
    state['status'] = 'completed'
    item = state['items'][0]
    item.update(status='saved', erp_category='未知类', library={'status': 'needs_confirmation', 'library_id': 8})
    bridge.save(state)
    live = {**source, '物化特性': '特殊酸'}
    if failure == 'identity': live['试剂名称'] = 'other'
    rows = [live]
    if failure == 'duplicate': rows.append({**live, '_erp_record_id': '2', '序号': '2', '物化特性': '未知类'})
    response = {'status': 'existing_unchanged', 'library_id': 8, 'erp_category': '特殊酸'}
    if failure == 'category': response['erp_category'] = '普通类'
    if failure == 'unverified': response['status'] = 'unverified'
    client = SimpleNamespace(fetch_reagent_detail=Mock(return_value=rows),
        recheck_reagent_library_by_id=Mock(return_value=response))
    if failure == 'timeout': client.recheck_reagent_library_by_id.side_effect = TimeoutError('unavailable')
    if failure:
        with pytest.raises((ValueError, TimeoutError)):
            bridge.recheck_library_confirmations('SJ001', client)
        assert bridge.batches()[0]['items'][0]['library']['status'] == 'needs_confirmation'
    else:
        bridge.recheck_library_confirmations('SJ001', client)
        saved = bridge.batches()[0]['items'][0]
        assert saved['library']['status'] == 'existing_unchanged'
        assert saved['reconciled_erp_category'] == '特殊酸'
        assert saved['result']['物化特性类别'] == '未知类'
        assert saved['library_rechecks'][0]['previous']['status'] == 'needs_confirmation'


@pytest.mark.parametrize('status', ['sending', 'failed', 'unverified'])
def test_recheck_never_replays_other_library_states(bridge, status):
    state = bridge.prepare([row(1)])
    state['status'] = 'completed'
    state['items'][0].update(status='saved', library={'status': status, 'library_id': 8})
    bridge.save(state)
    client = Mock()
    bridge.recheck_library_confirmations('SJ001', client)
    client.fetch_reagent_detail.assert_not_called()


def test_existing_erp_duplicate_conflict_prevents_new_category_write(bridge):
    state = bridge.prepare([row(2)])
    load_results(bridge, state, ['普通类'])
    client = SimpleNamespace(fetch_reagent_detail=Mock(return_value=[row(1, 物化特性='特殊酸'), row(2)]),
        save_physicochemical_property_by_id=Mock())
    bridge.process(state, client, Mock(), dry_run=False)
    client.save_physicochemical_property_by_id.assert_not_called()
    assert state['items'][0]['error_code'] == 'DUPLICATE_CATEGORY_CONFLICT'


def test_equal_duplicate_decisions_are_accepted(bridge):
    state = bridge.prepare([row(1), row(2)])
    load_results(bridge, state, ['普通类', '普通类'])
    bridge.load_results(state)
    assert not bridge.duplicate_result_conflicts(state)


def test_duplicate_conflict_can_be_explicitly_reviewed_without_overwriting_evidence(bridge):
    state = bridge.prepare([row(1), row(2)])
    load_results(bridge, state, ['普通类', '特殊酸'])
    client, memory = Mock(), Mock()
    bridge.process(state, client, memory, dry_run=False)
    assert state['status'] == 'review_pending'
    path = bridge.review_confirmation_path(state)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({i['key']: '普通类' for i in state['items']}), encoding='utf-8')
    client.fetch_reagent_detail.return_value = [row(1, 物化特性='普通类'), row(2, 物化特性='普通类')]
    bridge.process(state, client, memory, dry_run=False, confirm_reviews=True)
    assert state['status'] == 'completed'
    assert all(i['operator_confirmed_category'] == '普通类' for i in state['items'])
    assert state['items'][1]['result']['物化特性类别'] == '特殊酸'
    client.save_physicochemical_property_by_id.assert_not_called()


def test_rechecked_library_can_continue_full_submission_with_original_evidence(bridge):
    from workbuddy_autopass import WorkBuddyAutoPass

    state = bridge.prepare([row(1)])
    load_results(bridge, state, ['未知类'])
    bridge.load_results(state)
    state['status'] = 'completed'
    state['items'][0].update(status='saved', erp_category='未知类',
                            library={'status': 'needs_confirmation', 'library_id': 8})
    bridge.save(state)
    live = row(1, 物化特性='特殊酸', _erp_parent_id='10')
    todo = [{'name': 'SJ001'}]
    client = SimpleNamespace(fetch_reagent_detail=Mock(side_effect=lambda _: [dict(live)]),
        fetch_todo_tasks=Mock(side_effect=lambda **_: list(todo)),
        recheck_reagent_library_by_id=Mock(return_value={
            'status': 'existing_unchanged', 'library_id': 8, 'erp_category': '特殊酸'}))
    button = Mock()
    def submit():
        todo.clear()
        live['_erp_parent_state'] = 'yes'
    button.click.side_effect = submit
    bot = SimpleNamespace(root_dir=bridge.root_dir, settings=bridge.settings,
        approval_write_mode=lambda: 'generate_library', pagination_check_succeeded=True,
        current_list_has_manual_review_item=Mock(return_value=(False, '')),
        enter_reagent_judgement_page=Mock(), open_task_detail_by_list_number=Mock(return_value=True),
        read_detail_info=Mock(return_value={'当前清单号': 'SJ001'}),
        find_unmatched_reagents_across_all_pages=Mock(return_value=[]),
        find_top_approve_button=Mock(return_value=button))
    service = WorkBuddyAutoPass(bot, bridge, client, Mock())
    service.run(['SJ001'])
    assert service.receipt('SJ001') == 'submitted_verified'
    button.click.assert_called_once()
    client.recheck_reagent_library_by_id.assert_called_once()
    assert bridge.batches()[0]['items'][0]['result']['物化特性类别'] == '未知类'
