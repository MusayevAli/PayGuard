const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'ui/index.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'ui/app.js'), 'utf8');
class Element {
  constructor(id) { this.id = id; this.innerHTML = ''; this.textContent = ''; this.value = ''; this.dataset = {}; this.style = {}; this.hidden = false; this.disabled = false; this.open = false; this.classList = { toggle() {} }; }
  addEventListener() {}
  setAttribute() {}
  removeAttribute() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() {}
}
function harness(storage = new Map()) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m => [m[1], new Element(m[1])]));
  const scenarios = ['normal', 'attack', 'large'].map(id => Object.assign(new Element(id), { dataset: { scenario: id } }));
  const liveScenarios = ['normal', 'attack', 'large'].map(id => Object.assign(new Element(id), { dataset: { liveScenario: id } }));
  const tabs = ['invoices', 'purchase-orders', 'receipts', 'vendors'].map(id => Object.assign(new Element(id), { dataset: { datasetTab: id } }));
  const document = {
    querySelector(selector) { if (!selector.startsWith('#')) return null; return elements.get(selector.slice(1)) || null; },
    querySelectorAll(selector) {
      if (selector === '[data-scenario]') return scenarios;
      if (selector === '[data-live-scenario]') return liveScenarios;
      if (selector === '#dataset-tabs button') return tabs;
      if (selector === '.view') return [...elements.values()].filter(e => e.id.startsWith('view-'));
      if (selector.includes(',')) return selector.split(',').map(s => elements.get(s.trim().slice(1))).filter(Boolean);
      return [];
    },
    addEventListener() {}, hidden: false
  };
  const requests = [];
  const context = {
    document, location: { hash: '', host: 'localhost' }, window: { addEventListener() {} },
    setInterval() {}, setTimeout, clearTimeout, AbortController, Intl, Date, console,
    crypto: { randomUUID: () => '00000000-0000-4000-8000-000000000001' },
    sessionStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    fetch: async (url, options) => {
      requests.push({ url, method: options.method });
      if (url === '/dataset/summary') return { ok: true, json: async () => ({ available: true, counts: { invoices: 2, purchase_orders: 1, receipts: 3, vendors: 1 }, formats: { pdf: 1, structured: 1 }, first_invoice_at: '2026-10-01T11:51:19Z' }) };
      if (url.startsWith('/dataset/invoices')) return { ok: true, json: async () => ({ items: [{ id: 'INV-001', supplier_name: '<script>unsafe</script>', status: 'queued', format: 'pdf', known_at: '2026-10-01T11:51:19Z', document_url: '/dataset/documents/INV-001' }], total: 1 }) };
      throw new Error(`Unexpected request ${url}`);
    }
  };
  vm.createContext(context);
  vm.runInContext(source.replace('  navigate().then(connectLive);', '  globalThis.ui = { state, setBusy, act, render, renderDemo, refreshDataset, renderDataset, navigate, runScenario, openPayment, openDatasetRecord, datasetDocumentLink, eventDescription, authorizationState, renderAuthorization, paymentAuthorization, demoReceipt, pauseDesk, applyClock, renderClock, renderProgress, hardwareDetails, refresh, runLiveScenario, openLiveResult, pendingLiveRun, startLiveJob, pollLiveJob, renderLiveJob, liveJobWorking, loadLiveExample, processLiveInvoice, unlockControls };'), context);
  return { ...context, context, elements, scenarios, liveScenarios, requests, storage };
}

test('visible HTML removes old terminology and uses five clear primary destinations', () => {
  const visible = html.replace(/<[^>]+>/g, ' ');
  assert.doesNotMatch(visible, /judge/i);
  assert.deepEqual([...html.matchAll(/data-nav="([^"]+)"/g)].map(m => m[1]), ['invoices', 'payments', 'vendors', 'dataset', 'events']);
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
  assert.equal(new Set(ids).size, ids.length);
  assert.match(html, /Control password/);
});

test('pending mutation keeps completed results and cached payment evidence readable without resubmitting', async () => {
  const h = harness();
  h.ui.state.demo = { scenarios: [{ id: 'normal', started: true, status: 'executed', title: 'Normal test' }, { id: 'attack', started: true, status: 'held' }, { id: 'large', started: false }] };
  h.ui.state.payments = [{ pay_id: 'PAY-1', vendor_id: 'V-1', status: 'settled', amount_minor: 100, currency: 'EUR', chip: { ok: true, signer: 'hw' }, events: [] }];
  h.ui.state.refreshing = true;
  let finish;
  const pending = h.ui.act(() => new Promise(resolve => { finish = resolve; }));
  assert.equal(h.scenarios[0].disabled, false);
  assert.equal(h.scenarios[1].disabled, false);
  assert.equal(h.scenarios[2].disabled, true);
  h.ui.renderDemo();
  assert.match(h.elements.get('demo-scenarios').innerHTML, /data-scenario="normal">View result/);
  assert.match(h.elements.get('demo-scenarios').innerHTML, /data-live-scenario="large" disabled>Run new test/);
  await h.ui.runScenario('normal');
  assert.match(h.elements.get('dialog-content').innerHTML, /Normal test/);
  h.ui.openPayment('PAY-1');
  assert.match(h.elements.get('dialog-content').innerHTML, /REAL FPGA/);
  assert.equal(h.requests.length, 0);
  finish({}); await pending;
});

test('dataset reads proceed while runtime refresh is blocked and require no mutation', async () => {
  const h = harness(); h.ui.state.refreshing = true; h.ui.state.busy = true;
  await h.ui.refreshDataset();
  assert.equal(h.requests.length, 2);
  assert.ok(h.requests.every(r => r.method === 'GET' && r.url.startsWith('/dataset/')));
  const table = h.elements.get('dataset-table').innerHTML;
  assert.match(table, /INV-001/); assert.match(table, /Queued/); assert.match(table, /Open source/);
  assert.match(table, /&lt;script&gt;unsafe/); assert.doesNotMatch(table, /<script>/);
  assert.match(h.elements.get('dataset-summary').innerHTML, /Purchase orders/);
});

test('source details are readable first with collapsed metadata and safe local document links', async () => {
  const h = harness(); await h.ui.refreshDataset(); h.ui.openDatasetRecord('INV-001');
  const detail = h.elements.get('dialog-content').innerHTML;
  assert.match(detail, /waiting for its scheduled arrival/);
  assert.match(detail, /Open original document/);
  assert.match(detail, /<details class="supporting-details detail-section"><summary>Source metadata/);
  assert.doesNotMatch(h.ui.datasetDocumentLink({ document_url: 'javascript:alert(1)' }), /href=/);
  assert.doesNotMatch(h.ui.datasetDocumentLink({ document_url: '//external.test/a' }), /href=/);
});

test('default and legacy routes resolve to useful dataset and demo screens', async () => {
  for (const [hash, expected] of [['', 'dataset'], ['#overview', 'dataset'], ['#judge', 'demo']]) {
    const h = harness(); h.location.hash = hash; h.ui.state.refreshing = true;
    await h.ui.navigate();
    assert.equal(h.ui.state.view, expected);
    assert.equal(h.elements.get(`view-${expected}`).hidden, false);
  }
});

test('test event keeps its underlying provenance while using a plain public label', () => {
  const h = harness(); const event = { type: 'judge_injection', payload: { kind: 'invoice' } };
  assert.equal(h.ui.eventDescription(event)[0], 'Test input recorded');
  assert.equal(event.type, 'judge_injection');
});

const hardwareEvent = (approved = true) => ({ id: 'ev_old_fpga', seq: 17, event_time: '2026-10-01T00:00:00Z', payload: { pay_id: 'PAY-HW', signer: 'hw', ok: approved }, indicator: { state: approved ? 'approved' : 'blocked', proof_verified: true, reason: approved ? 'Verified FPGA PAY response.' : 'Verified FPGA COMMIT rejection: account not approved.' } });

test('authorization colors require canonical verified FPGA responses, not connectivity or agent acceptance', () => {
  const h = harness();
  assert.equal(h.ui.authorizationState(hardwareEvent()).state, 'approved');
  assert.equal(h.ui.authorizationState(hardwareEvent(false)).state, 'blocked');
  assert.equal(h.ui.authorizationState({ payload: { signer: 'hw', ok: true } }).state, 'unknown');
  assert.equal(h.ui.authorizationState({ payload: { signer: 'hw', ok: false, chip_reason: 'hardware_unavailable' }, indicator: { state: 'unverified' } }).state, 'unknown');
  assert.equal(h.ui.authorizationState({ payload: { signer: 'hw', ok: true }, indicator: { state: 'approved', proof_verified: false } }).state, 'unknown');
  assert.equal(h.ui.authorizationState({ payload: { signer: 'hw', ok: true }, indicator: { state: 'software' } }).state, 'software');
  assert.equal(h.ui.authorizationState({ payload: { signer: 'stub', ok: true }, indicator: { state: 'approved' } }).state, 'software');
  assert.equal(h.ui.authorizationState({ ...hardwareEvent(), payload: { signer: 'hw', ok: 'true' } }).state, 'unknown');
  assert.equal(h.ui.authorizationState({ type: 'agent_action', payload: { status: 'accepted' } }).state, 'unknown');
});

test('global indicator recovers canonical history beyond recent events and grays out for stale or disconnected status', () => {
  const h = harness();
  h.ui.state.events = Array.from({ length: 500 }, (_, i) => ({ seq: 1000 + i, type: 'agent_action' }));
  h.ui.state.summary = { latest_authorization: hardwareEvent() };
  h.ui.state.summaryReceivedAt = Date.now(); h.ui.state.chip = { kind: 'hw' }; h.ui.state.chipReceivedAt = Date.now();
  h.ui.renderAuthorization();
  assert.match(h.elements.get('latest-authorization').innerHTML, /authorization-result approved/);
  assert.match(h.elements.get('latest-authorization').innerHTML, /Latest recorded FPGA response/);
  assert.match(h.elements.get('latest-authorization').innerHTML, /PAY-HW/);
  h.ui.state.summaryReceivedAt = Date.now() - 16000; h.ui.renderAuthorization();
  assert.match(h.elements.get('latest-authorization').innerHTML, /STATUS UNAVAILABLE/);
  assert.doesNotMatch(h.elements.get('latest-authorization').innerHTML, /authorization-result approved/);
  h.ui.state.summaryReceivedAt = Date.now(); h.ui.state.chip = null; h.ui.renderAuthorization();
  assert.doesNotMatch(h.elements.get('latest-authorization').innerHTML, /authorization-result approved/);
});

test('historical payment and demo result keep their own recorded verdict without resubmitting', () => {
  const h = harness(); const event = hardwareEvent(false);
  h.ui.state.chip = null;
  h.ui.state.payments = [{ pay_id: 'PAY-HW', status: 'blocked', amount_minor: 100, currency: 'EUR', chip: event.payload, chip_indicator: event.indicator, chip_event_id: event.id, chip_event_time: event.event_time, chip_seq: event.seq, events: [event.id] }];
  h.ui.openPayment('PAY-HW');
  assert.match(h.elements.get('dialog-content').innerHTML, /authorization-result blocked/);
  assert.match(h.elements.get('dialog-content').innerHTML, /Historical recorded response · REAL FPGA/);
  h.ui.demoReceipt({ id: 'attack', pay_id: 'PAY-HW', signer_kind: 'hw', status: 'held', evidence_event_ids: [event.id] });
  assert.match(h.elements.get('dialog-content').innerHTML, /BLOCKED/);
  assert.equal(h.requests.length, 0);
});

test('Pause stays available during a pending invoice and sends only one concurrent pause', async () => {
  const h = harness(); h.ui.state.busy = true; h.ui.state.controllerToken = 'test';
  h.ui.applyClock({ running: true, sim_now: '2026-10-01T11:11:12Z' });
  assert.equal(h.elements.get('pause-button').disabled, false);
  assert.equal(h.elements.get('run-button').disabled, true);
  let reply; let calls = 0;
  h.context.fetch = async (url, options) => {
    assert.equal(url, '/clock/pause'); assert.equal(options.method, 'POST'); calls++;
    return new Promise(resolve => { reply = () => resolve({ ok: true, json: async () => ({ running: false, sim_now: '2026-10-01T11:11:12Z' }) }); });
  };
  const pending = h.ui.pauseDesk();
  await h.ui.pauseDesk();
  assert.equal(calls, 1); assert.equal(h.elements.get('pause-button').disabled, true);
  reply(); await pending;
  assert.equal(h.ui.state.clock.running, false); assert.equal(h.ui.state.busy, true);
  assert.match(h.elements.get('clock-notice').textContent, /Work already submitted may still finish/);
});

test('clock displays exact full UTC date while pending progress uses actual reported counts and events', () => {
  const h = harness();
  h.ui.state.summary = { invoices: { posted: 23, held: 64 } };
  h.ui.state.events = [{ id: 'ev_123', seq: 123, type: 'invoice_received', payload: { invoice_id: 'INV-23' }, event_time: '2026-10-01T11:11:12Z' }];
  h.ui.state.pendingAction = { label: 'Processing your invoice', selector: '#invoice-progress', startedAt: Date.now() - 12000, waiting: true };
  h.ui.applyClock({ running: false, sim_now: '2026-10-01T14:11:12+03:00' });
  h.ui.renderProgress();
  assert.match(h.elements.get('sim-time').textContent, /01 Oct 2026, 11:11:12 UTC/);
  assert.equal(h.elements.get('clock-state').textContent, 'Paused');
  assert.match(h.elements.get('invoice-progress').textContent, /87 invoices recorded, 23 posted/);
  assert.match(h.elements.get('invoice-progress').textContent, /Latest activity #123/);
  assert.match(h.elements.get('invoice-progress').textContent, /still pending/);
  assert.doesNotMatch(h.elements.get('invoice-progress').textContent, /%/);
});

test('hardware details distinguish real FPGA UTC clock from dataset UTC and collapse raw status', () => {
  const h = harness();
  h.ui.state.chip = { kind: 'hw', hardware: { clock: 'fpga-real-time', hardware_time: 1792886400 }, slots: [] };
  h.ui.state.clock = { sim_now: '2026-10-01T11:11:12Z' };
  h.ui.hardwareDetails();
  const detail = h.elements.get('dialog-content').innerHTML;
  assert.match(detail, /FPGA clock · UTC/); assert.match(detail, /Dataset clock · UTC/);
  assert.match(detail, /The dataset clock controls invoice arrivals/);
  assert.match(detail, /01 Oct 2026, 11:11:12 UTC/);
  assert.match(detail, /<details class="supporting-details detail-section"><summary>Raw hardware status/);
});

test('a fresh clock response renders immediately while slower dashboard reads are still pending', async () => {
  const h = harness(); const releases = []; let done = false;
  h.context.fetch = async (url) => {
    if (url === '/state') return { ok: true, json: async () => ({ running: true, sim_now: '2026-10-02T01:02:03Z', latest_seq: 0 }) };
    return new Promise(resolve => releases.push(() => resolve({ ok: true, json: async () => ({
      '/chip/status': { kind: 'hw' }, '/dashboard/summary': { invoices: {}, authorizations: {}, agents: {} },
      '/packets': { packets: [] }, '/dashboard/payments?limit=1000': { payments: [] }, '/dashboard/vendors': { vendors: [] }
    })[url] || {} })));
  };
  const pending = h.ui.refresh().then(() => { done = true; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(done, false);
  assert.match(h.elements.get('sim-time').textContent, /02 Oct 2026, 01:02:03 UTC/);
  assert.equal(h.elements.get('clock-state').textContent, 'Running');
  assert.equal(h.elements.get('pause-button').disabled, false);
  for (const release of releases) release();
  await pending;
});

test('every completed scenario offers a fresh run and a separate historical result', () => {
  const h = harness();
  h.ui.state.demo = { scenarios: [{ id: 'normal', started: true }, { id: 'attack', started: true, latest_run_id: 'run-123' }] };
  h.ui.renderDemo();
  const cards = h.elements.get('demo-scenarios').innerHTML;
  assert.equal((cards.match(/Run new test/g) || []).length, 2);
  assert.match(cards, /data-live-result="run-123">View last result/);
  h.ui.setBusy(true);
  assert.ok(h.liveScenarios.every(button => button.disabled));
  assert.equal(h.scenarios[0].disabled, false);
});

function liveTestNetwork(h, { onPost, onGet } = {}) {
  const originalFetch = h.context.fetch;
  h.context.fetch = async (url, options) => {
    if (options.method === 'POST') return onPost(url, options);
    if (url.startsWith('/demo/jobs/')) return onGet(url, options);
    if (url === '/dashboard/payments?limit=1000') return { ok: true, json: async () => ({ payments: [] }) };
    return originalFetch(url, options);
  };
  h.ui.state.controllerToken = 'test';
  h.ui.state.refreshing = true;
  h.ui.state.demo = { scenarios: [{ id: 'attack', started: true }] };
}
const ok = data => ({ ok: true, json: async () => data });
const queuedJob = runId => ({ run_id: runId, kind: 'attack', job_status: 'queued', submitted_at: '2026-10-25T16:00:00Z', progress: [{ agent: 'desk', status: 'queued', message: 'Waiting for the current desk task' }], evidence_events: [] });
const finishedJob = runId => ({
  ...queuedJob(runId), job_status: 'completed', pay_id: 'PAY-FRESH', invoice_id: 'LIVE-INV', result: { status: 'held', held_reason: 'not_allowlisted' },
  evidence_events: [
    { id: 'ev_invoice', seq: 11, type: 'invoice_received', actor: 'demo_operator', payload: { invoice_id: 'LIVE-INV', format: 'email' } },
    { id: 'ev_invoice_action', seq: 12, type: 'agent_action', actor: 'invoice_agent', payload: { action_type: 'posted', explanation: 'Matched PO-90001 and receipt GR-9001.', rule_id: 'R2a' } },
    { id: 'ev_payment', seq: 13, type: 'payment_requested', actor: 'payment_agent', payload: { pay_id: 'PAY-FRESH', amount_minor: 125000, currency: 'EUR', vendor_id: 'V-1' } },
    { id: 'ev_fresh', seq: 14, type: 'chip_response', actor: 'payment_agent', payload: { signer: 'hw', ok: false, pay_id: 'PAY-FRESH', chip_reason: 'not_allowlisted' } }
  ],
  chip_event: { id: 'ev_fresh', seq: 14, event_time: '2026-10-25T16:00:01Z', payload: { signer: 'hw', ok: false, pay_id: 'PAY-FRESH' }, indicator: { state: 'blocked', proof_verified: true, reason: 'account', op: 'COMMIT_INVOICE' } }
});

test('live job returns immediately and shows waiting while the worker has no result', async () => {
  const h = harness(); const posts = []; let job;
  liveTestNetwork(h, {
    onPost: async (url, options) => { posts.push({ url, body: JSON.parse(options.body) }); job = queuedJob(posts[0].body.run_id); return ok(job); },
    onGet: async () => ok(job)
  });
  await h.ui.runLiveScenario('attack');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/demo/jobs/attack');
  assert.equal(h.ui.liveJobWorking(), true);
  assert.match(h.elements.get('live-timeline').innerHTML, /Waiting for the current desk task/);
  assert.match(h.elements.get('live-fpga-result').innerHTML, /WAITING FOR REQUEST/);
  assert.doesNotMatch(h.elements.get('live-fpga-result').innerHTML, /authorization-result approved/);
  assert.equal(h.elements.get('detail-dialog').open, false);
  await h.ui.runLiveScenario('attack');
  assert.equal(posts.length, 1);
});

test('polling shows actual agent explanations and verified hardware output on the same page', async () => {
  const h = harness(); let job; const posts = []; let serial = 0;
  h.context.crypto.randomUUID = () => `00000000-0000-4000-8000-${String(++serial).padStart(12, '0')}`;
  liveTestNetwork(h, {
    onPost: async (url, options) => { posts.push(JSON.parse(options.body)); job = queuedJob(posts.at(-1).run_id); return ok(job); },
    onGet: async () => ok(job)
  });
  await h.ui.runLiveScenario('attack');
  job = finishedJob(job.run_id);
  await h.ui.pollLiveJob();
  assert.equal(h.ui.liveJobWorking(), false);
  assert.match(h.elements.get('live-timeline').innerHTML, /Matched PO-90001 and receipt GR-9001/);
  assert.match(h.elements.get('live-timeline').innerHTML, /Rule R2a/);
  assert.match(h.elements.get('live-agents').innerHTML, /No action from this agent was recorded/);
  assert.match(h.elements.get('live-fpga-result').innerHTML, /authorization-result blocked/);
  assert.match(h.elements.get('live-fpga-result').innerHTML, /REAL FPGA/);
  assert.match(h.elements.get('live-run-meta').innerHTML, /PAY-FRESH/);
  assert.equal(h.elements.get('detail-dialog').open, false);
  await h.ui.runLiveScenario('attack');
  assert.equal(posts.length, 2);
  assert.notEqual(posts[0].run_id, posts[1].run_id);
});

test('a lost reply restores the original run with GET after reload, without a second POST', async () => {
  const storage = new Map(); const first = harness(storage); let original; let posts = 0;
  liveTestNetwork(first, {
    onPost: async (url, options) => { posts++; original = JSON.parse(options.body); throw new Error('Network interrupted'); },
    onGet: async () => ok(finishedJob(original.run_id))
  });
  await first.ui.startLiveJob('invoice', { document_text: 'Original invoice text' });
  assert.equal(posts, 1);
  assert.equal(first.ui.pendingLiveRun('invoice'), original.run_id);
  const reopened = harness(storage);
  liveTestNetwork(reopened, {
    onPost: async () => { throw new Error('Must not submit again'); },
    onGet: async () => ok({ ...finishedJob(original.run_id), kind: 'invoice' })
  });
  await reopened.ui.startLiveJob('invoice', { document_text: 'Edited text is a different request' });
  assert.equal(reopened.ui.pendingLiveRun('invoice'), null);
  assert.equal(reopened.ui.state.liveJob.run_id, original.run_id);
  assert.match(reopened.elements.get('live-fpga-result').innerHTML, /authorization-result blocked/);
});

test('a retry after a confirmed missing job keeps its original invoice body and ID', async () => {
  const h = harness(); let original; const posts = [];
  liveTestNetwork(h, {
    onPost: async (url, options) => { original = JSON.parse(options.body); throw new Error('Network lost before receipt'); },
    onGet: async () => ({ ok: false, status: 404, json: async () => ({ detail: 'Unknown run' }) })
  });
  await h.ui.startLiveJob('invoice', { document_text: 'Original' });
  liveTestNetwork(h, {
    onPost: async (url, options) => { posts.push(JSON.parse(options.body)); return ok({ ...queuedJob(original.run_id), kind: 'invoice' }); },
    onGet: async () => ({ ok: false, status: 404, json: async () => ({ detail: 'Unknown run' }) })
  });
  await h.ui.startLiveJob('invoice', { document_text: 'Changed' });
  assert.deepEqual(posts[0], original);
});

test('plain invoice intake sends the actual edited text and uses a read-only example request', async () => {
  const h = harness(); let posted; let job; const urls = [];
  h.ui.state.controllerToken = 'test';
  h.context.fetch = async (url, options) => {
    urls.push({ url, method: options.method });
    if (url === '/judge/invoice/example') return ok({ document_text: 'Invoice from the actual PO', notice: 'Matches a recorded receipt.' });
    if (options.method === 'POST') { posted = JSON.parse(options.body); job = { ...queuedJob(posted.run_id), kind: 'invoice' }; return ok(job); }
    if (url.startsWith('/demo/jobs/')) return ok(job);
    throw new Error('Unexpected request');
  };
  await h.ui.loadLiveExample();
  assert.equal(h.elements.get('live-invoice-text').value, 'Invoice from the actual PO');
  h.elements.get('live-invoice-text').value = 'Changed invoice text';
  await h.ui.processLiveInvoice();
  assert.equal(posted.invoice.document_text, 'Changed invoice text');
  assert.deepEqual(urls[0], { url: '/judge/invoice/example', method: 'GET' });
});

test('the local control password is checked before controls are marked unlocked', async () => {
  const h = harness(); h.elements.get('operator-token').value = 'wrong-password';
  h.elements.get('operator-dialog').open = true;
  h.context.fetch = async (url, options) => {
    assert.equal(url, '/controller/check');
    assert.equal(options.headers['X-Payguard-Controller'], 'wrong-password');
    return { ok: false, status: 403, json: async () => ({ detail: 'Unlock controls' }) };
  };
  await h.ui.unlockControls();
  assert.equal(h.ui.state.controllerToken, '');
  assert.equal(h.elements.get('operator-dialog').open, true);
  assert.match(h.elements.get('operator-error').textContent, /Could not unlock/);
  h.elements.get('operator-token').value = 'valid-password';
  h.context.fetch = async () => ok({ unlocked: true, payment_authorized: false });
  await h.ui.unlockControls();
  assert.equal(h.elements.get('operator-dialog').open, false);
  assert.equal(h.elements.get('operator-label').textContent, 'Controls unlocked');
  assert.equal(h.elements.get('operator-token').value, '');
});

test('no FPGA response remains gray and agent-supplied HTML is escaped', () => {
  const h = harness();
  h.ui.state.liveJob = { ...finishedJob('run'), chip_event: null, evidence_events: [{ id: 'a', seq: 1, type: 'agent_action', actor: 'invoice_agent', payload: { action_type: 'held', explanation: '<img onerror="bad">Missing receipt', rule_id: 'R3' } }], pay_id: null, result: { status: 'held', reason: 'Missing receipt' } };
  h.ui.renderLiveJob();
  assert.match(h.elements.get('live-fpga-result').innerHTML, /NO FPGA REQUEST/);
  assert.doesNotMatch(h.elements.get('live-timeline').innerHTML, /<img onerror/);
  assert.match(h.elements.get('live-timeline').innerHTML, /&lt;img/);
  assert.match(h.elements.get('live-next-action').innerHTML, /Missing receipt/);
});

test('approval reason explains cumulative vendor spending instead of implying a payment passed', () => {
  const h = harness();
  const result = h.ui.authorizationState({ ...hardwareEvent(false), indicator: { state: 'blocked', proof_verified: true, reason: 'approval', op: 'PAY' } });
  assert.match(result.reason, /cumulative vendor spending threshold/);
  assert.equal(result.state, 'blocked');
});

test('a response for another run cannot replace the current invoice or FPGA verdict', async () => {
  const h = harness();
  h.ui.state.liveRunId = 'original-run';
  h.ui.state.liveJob = finishedJob('original-run');
  h.context.fetch = async () => ok(finishedJob('different-run'));
  await h.ui.pollLiveJob();
  assert.equal(h.ui.state.liveJob.run_id, 'original-run');
  assert.match(h.elements.get('live-connection-note').textContent, /different request ID/);
});
