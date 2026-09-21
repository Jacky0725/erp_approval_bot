from pathlib import Path
import shutil
import subprocess

import pytest


def test_dashboard_status_requests_handle_overlap_failure_and_timeout():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the isolated JavaScript request checks")
    root = Path(__file__).resolve().parents[1]
    program = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/static/dashboard.js', 'utf8');
const runDataHelper = source.slice(source.indexOf('    function buildRunRequestData('),
                                   source.indexOf('    document.querySelectorAll("button[data-action]")'));
class FakeFormData {
  constructor(form) { this.values = new Map(form.entries || []); }
  has(key) { return this.values.has(key); }
  set(key, value) { this.values.set(key, value); }
  get(key) { return this.values.get(key); }
  entries() { return this.values.entries(); }
}
const runContext = vm.createContext({FormData: FakeFormData});
vm.runInContext(runDataHelper, runContext);
const unchecked = runContext.buildRunRequestData(
  {entries: [['approval_write_mode', 'multi_page']]}, 'suggestions', ['SJ1']
);
assert.equal(unchecked.get('process_all_todos'), 'false');
assert.equal(unchecked.get('auto_pass'), 'false');
assert.equal(unchecked.get('target_list_numbers'), 'SJ1');
const checked = runContext.buildRunRequestData(
  {entries: [['process_all_todos', 'true'], ['auto_pass', 'true']]}, 'suggestions', []
);
assert.equal(checked.get('process_all_todos'), 'true');
assert.equal(checked.get('auto_pass'), 'true');
const helper = source.slice(source.indexOf('    let statusRefreshPromise = null;'),
                            source.indexOf('    async function readAndRenderStatus('));
let finish, calls = 0, label = '', timeout, cleared = 0;
const badge = {title: '', removeAttribute() { this.title = ''; }};
const context = vm.createContext({
  AbortController,
  document: {querySelector: () => badge},
  setText: (_, text) => { label = text; },
  setTimeout: fn => { timeout = fn; return 1; },
  clearTimeout: () => { cleared++; },
  readAndRenderStatus: () => { calls++; return new Promise(resolve => { finish = resolve; }); },
});
vm.runInContext(helper, context);
(async () => {
  const first = context.refreshStatus();
  const second = context.refreshStatus();
  assert.equal(first, second);
  assert.equal(calls, 1);
  finish();
  assert.equal(await first, true);
  context.readAndRenderStatus = async () => { throw new Error('offline'); };
  assert.equal(await context.refreshStatus(), false);
  assert.match(label, /过期/);
  assert.ok(badge.title);
  context.readAndRenderStatus = async () => {};
  assert.equal(await context.refreshStatus(), true);
  assert.equal(badge.title, '');
  context.fetch = async () => ({ok: false, status: 503, json: async () => ({})});
  await assert.rejects(context.readDashboardJSON('/api/status'), /HTTP 503/);
  context.fetch = async () => ({ok: true, json: async () => { throw new Error('invalid JSON'); }});
  await assert.rejects(context.readDashboardJSON('/api/status'), /invalid JSON/);
  context.fetch = (_, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('aborted')));
  });
  const pending = context.readDashboardJSON('/api/status');
  timeout();
  await assert.rejects(pending, /aborted/);
  assert.equal(cleared, 3);
  let capturedTimeout = 0;
  context.setTimeout = (fn, ms) => { capturedTimeout = ms; timeout = fn; return 1; };
  context.fetch = async (_url, options) => {
    assert.equal(options.method, 'POST');
    return {ok: true, json: async () => ({generated: true})};
  };
  const llmResult = await context.fetchDashboardJSON('/api/review/llm_advice', {method: 'POST'}, 120000);
  assert.equal(capturedTimeout, 120000);
  assert.equal(llmResult.payload.generated, true);
  assert.match(context.dashboardRequestErrorMessage(new Error('signal is aborted without reason')), /前端已停止等待/);
  // A failed completion-dependent read must be retried on the next refresh.
  const renderSource = source.slice(source.indexOf('    async function readAndRenderStatus('),
                                    source.indexOf('    function setUpdateState('));
  for (const name of ['setClass', 'setDisabled', 'updateDryRunUi', 'renderTodoTasks', 'renderWorkflow']) {
    context[name] = () => {};
  }
  for (const name of ['approvalWriteModeLabel', 'erpWriteBackendLabel', 'erpApiDiscoveryLabel']) {
    context[name] = value => value;
  }
  context.safe = (value, fallback = '-') => value ?? fallback;
  context.selectedTodoNumbers = () => [];
  context.lastFinishedAt = '';
  context.activePage = 'artifacts';
  const forces = [];
  context.refreshArtifacts = async options => {
    forces.push(Boolean(options.force));
    if (forces.length === 1) throw new Error('artifact read failed');
  };
  context.fetch = async () => ({ok: true, json: async () => ({status: {running: false, finished_at: 'done'}})});
  vm.runInContext(renderSource, context);
  assert.equal(await context.refreshStatus(), false);
  assert.equal(context.lastFinishedAt, '');
  assert.equal(await context.refreshStatus(), true);
  assert.equal(context.lastFinishedAt, 'done');
  assert.deepEqual(forces, [true, true]);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", program], cwd=root, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
