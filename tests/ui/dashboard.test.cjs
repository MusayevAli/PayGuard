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
  vm.runInContext(source.replace('  navigate().then(connectLive);', '  globalThis.ui = { state, setBusy, act, render, renderDemo, refreshDataset, renderDataset, navigate, runScenario, openPayment, openDatasetRecord, datasetDocumentLink, eventDescription, authorizationState, renderAuthorization, paymentAuthorization, demoReceipt, pauseDesk, applyClock, renderClock, renderProgress, hardwareDetails, refresh, runLiveScenario, openLiveResult, pendingLiveRun };'), context);
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

function liveTestNetwork(h, onPost) {
  const originalFetch = h.context.fetch;
  h.context.fetch = async (url, options) => {
    if (options.method === 'POST') return onPost(url, options);
    if (url === '/dashboard/payments?limit=1000') return { ok: true, json: async () => ({ payments: [{ pay_id: 'PAY-FRESH', chip: { signer: 'hw', ok: false }, chip_indicator: { state: 'blocked', proof_verified: true, reason: 'not_allowlisted' }, chip_event_id: 'ev_fresh', chip_event_time: '2026-10-26T01:02:03Z' }] }) };
    return originalFetch(url, options);
  };
  h.ui.state.controllerToken = 'test';
  h.ui.state.refreshing = true;
  h.ui.state.demo = { scenarios: [{ id: 'attack', started: true }] };
}

test('live request reaches POST with fresh UUID, verified result, and distinct next-run identity', async () => {
  const h = harness(); const posted = []; let serial = 0;
  h.context.crypto.randomUUID = () => `00000000-0000-4000-8000-${String(++serial).padStart(12, '0')}`;
  liveTestNetwork(h, async (url, options) => {
    posted.push({ url, body: JSON.parse(options.body), key: options.headers['Idempotency-Key'] });
    return { ok: true, json: async () => ({ id: 'attack', run_id: JSON.parse(options.body).run_id, run_started_at: '2026-10-26T01:02:03Z', pay_id: 'PAY-FRESH', status: 'held' }) };
  });
  await h.ui.runLiveScenario('attack');
  assert.equal(posted[0].url, '/demo/runs/attack');
  assert.equal(h.storage.size, 0);
  assert.match(h.elements.get('dialog-content').innerHTML, /authorization-result blocked/);
  assert.match(h.elements.get('dialog-content').innerHTML, /Response to this live request · REAL FPGA/);
  assert.match(h.elements.get('dialog-content').innerHTML, /PAY-FRESH/);
  assert.match(h.elements.get('dialog-content').innerHTML, /Submitted · real UTC/);
  await h.ui.runLiveScenario('attack');
  assert.equal(posted.length, 2);
  assert.notEqual(posted[0].body.run_id, posted[1].body.run_id);
  assert.notEqual(posted[0].key, posted[1].key);
});

test('a lost live response survives page reload and retries exactly the same request identity', async () => {
  const storage = new Map(); const first = harness(storage); let original;
  liveTestNetwork(first, async (url, options) => { original = { body: options.body, key: options.headers['Idempotency-Key'] }; throw new Error('Network interrupted'); });
  await first.ui.runLiveScenario('attack');
  assert.equal(storage.size, 1);
  assert.match(first.elements.get('demo-scenarios').innerHTML, /Retry live request/);
  const reopened = harness(storage); let retry;
  liveTestNetwork(reopened, async (url, options) => {
    retry = { body: options.body, key: options.headers['Idempotency-Key'] };
    return { ok: true, json: async () => ({ id: 'attack', run_id: JSON.parse(options.body).run_id, pay_id: 'PAY-FRESH', status: 'held' }) };
  });
  await reopened.ui.runLiveScenario('attack');
  assert.deepEqual(retry, original);
  assert.equal(storage.size, 0);
});

test('an in-flight live request cannot be duplicated and historical viewing sends no POST', async () => {
  const h = harness(); let finish; let posts = 0;
  liveTestNetwork(h, async (url, options) => {
    posts++;
    return new Promise(resolve => { finish = () => resolve({ ok: true, json: async () => ({ id: 'attack', run_id: JSON.parse(options.body).run_id, pay_id: 'PAY-FRESH', status: 'held' }) }); });
  });
  const pending = h.ui.runLiveScenario('attack');
  await h.ui.runLiveScenario('attack');
  assert.equal(posts, 1);
  finish(); await pending;
  const originalFetch = h.context.fetch;
  h.context.fetch = async (url, options) => url === '/demo/runs/existing-run' ? { ok: true, json: async () => ({ id: 'attack', run_id: 'existing-run', pay_id: 'PAY-FRESH', status: 'held' }) } : originalFetch(url, options);
  await h.ui.openLiveResult('existing-run');
  assert.equal(posts, 1);
  assert.match(h.elements.get('dialog-content').innerHTML, /Historical recorded response/);
});
