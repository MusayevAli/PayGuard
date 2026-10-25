/* PayGuard's UI is a projection of server evidence, never an authorization source. */
'use strict';
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const state = { view: 'dataset', datasetSummary: null, datasetRows: {}, datasetTab: 'invoices', datasetLoading: false, clock: null, clockReceivedAt: 0, clockRevision: 0, pausePending: false, pendingAction: null, summaryReceivedAt: 0, chip: null, chipReceivedAt: 0, summary: null, packets: [], payables: [], payments: [], vendors: [], invoices: [], banks: [], ledger: [], balances: {}, demo: null, invoiceOptions: null, events: [], eventSeq: 0, connected: false, refreshing: false, busy: false, packetTab: 'open', paymentTab: 'requests', pages: {}, controllerToken: '', controllerName: 'controller', ws: null, socketGeneration: 0, ledgerHasMore: false, receivedAt: null };
  const pageInfo = {
    dataset: ['Loaded files', 'Dataset', 'Browse the invoices, purchase orders and receipts already loaded into this desk.'],
    payments: ['Accounts payable', 'Payments', 'Inspect requested, blocked and settled payments.'],
    invoices: ['Accounts payable', 'Invoices', 'Agent decisions on invoices received so far.'],
    evidence: ['Payment controls', 'Approvals', 'Review the evidence and record your decision.'],
    vendors: ['Accounts payable', 'Vendors', 'Vendor records, approved accounts and bank-change history.'],
    ledger: ['Accounting', 'Ledger', 'Double-entry journals and the evidence behind them.'],
    events: ['Desk activity', 'Activity', 'Decisions, approvals and errors from the running desk.'],
    demo: ['Watch the desk', 'Live demo', 'Submit new information, follow the agents, and inspect the FPGA response.']
  };
  const navNames = { dataset: 'Dataset', payments: 'Payments', invoices: 'Invoices', evidence: 'Approvals', vendors: 'Vendors', ledger: 'Ledger', events: 'Activity', demo: 'Demo' };
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  const human = value => String(value ?? '').replaceAll('_', ' ').replace(/\bjudge\b/gi, 'test').replace(/^./, x => x.toUpperCase());
  const money = (minor, currency = 'EUR') => {
    if (minor == null || !Number.isFinite(Number(minor))) return '—';
    const digits = ['JPY', 'KRW'].includes(currency) ? 0 : ['BHD', 'KWD'].includes(currency) ? 3 : 2;
    try { return new Intl.NumberFormat('en-GB', { style: 'currency', currency, currencyDisplay: 'symbol', minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(minor) / 10 ** digits); }
    catch { return `${(Number(minor) / 100).toFixed(2)} ${currency}`; }
  };
  const date = (value, includeTime = false) => {
    if (!value) return '—';
    const dt = new Date(value);
    if (Number.isNaN(dt.getTime())) return String(value);
    return new Intl.DateTimeFormat('en-GB', { day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC', ...(includeTime ? { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false } : {}) }).format(dt) + (includeTime ? ' UTC' : '');
  };
  const shortTime = value => value ? new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' }).format(new Date(value)) : '—';
  const vendorName = id => state.vendors.find(v => v.id === id)?.legal_name || id || 'Unknown vendor';
  const pill = (text, tone = '') => `<span class="pill ${tone}">${escape(text)}</span>`;
  const empty = (title, detail = '') => `<div class="empty"><strong>${escape(title)}</strong>${escape(detail)}</div>`;
  const statusPill = status => pill(human(status), ['blocked', 'rejected', 'bank_rejected', 'suspended', 'error'].includes(status) ? 'danger' : ['held', 'escalated', 'pending', 'scheduled', 'open', 'needs_button', 'action', 'urgent'].includes(status) ? 'warning' : ['queued', 'received', 'extracted', 'requested', 'expired', 'onboarding'].includes(status) ? 'muted' : '');
  const eventButton = (id, label = id) => `<button class="row-button mono" data-event="${escape(id)}">${escape(label)}</button>`;
  const setNotice = (selector, message, tone = '') => { const element = $(selector); element.textContent = message; element.className = `notice ${tone}`; element.hidden = !message; };
  const connection = (ok, label) => { state.connected = ok; $('#connection').innerHTML = `<i class="dot ${ok ? 'green' : 'orange'}"></i>${escape(label || (ok ? 'Connected to desk' : 'Reconnecting'))}`; };
  function ensureController() {
    if (state.controllerToken || state.chip?.controller_required === false) return true;
    $('#operator-dialog').showModal();
    $('#operator-token').focus();
    return false;
  }
  async function api(path, { method = 'GET', body, controller = false, idempotencyKey } = {}) {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (controller && state.controllerToken) headers['X-Payguard-Controller'] = state.controllerToken;
    if (method !== 'GET') headers['Idempotency-Key'] = idempotencyKey || (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
    const abort = new AbortController();
    const timeout = method === 'GET' ? setTimeout(() => abort.abort(), 10000) : null;
    let response;
    try { response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin', cache: 'no-store', signal: abort.signal }); }
    catch (error) { if (error.name === 'AbortError') throw new Error('The desk did not answer within 10 seconds.'); throw error; }
    finally { if (timeout !== null) clearTimeout(timeout); }
    let data;
    try { data = await response.json(); } catch { throw new Error(`The desk returned an unreadable response (${response.status}).`); }
    if (!response.ok) {
      const error = new Error(data.error?.message || (typeof data.detail === 'string' ? data.detail : data.detail ? JSON.stringify(data.detail) : `Request failed (${response.status})`));
      error.status = response.status;
      error.hint = data.error?.hint;
      if (response.status === 401 || response.status === 403) error.message += ' Open Settings to unlock controls.';
      throw error;
    }
    return data;
  }
  function setBusy(busy) {
    state.busy = busy;
    $$('#step-button, #judge-form button[type="submit"], #invoice-judge-form button[type="submit"]').forEach(button => { button.disabled = busy; });
    $$('[data-scenario]').forEach(button => {
      const completed = state.demo?.scenarios?.some(s => s.id === button.dataset.scenario && s.started);
      button.disabled = busy && !completed;
    });
    $$('[data-live-scenario]').forEach(button => { button.disabled = busy || liveJobWorking(); });
    updateLiveControls();
    renderClock();
  }
  function renderProgress() {
    const pending = state.pendingAction;
    if (!pending?.waiting) return;
    const invoiceCounts = state.summary?.invoices;
    const total = invoiceCounts ? Object.values(invoiceCounts).reduce((sum, n) => sum + n, 0) : null;
    const latest = state.events[state.events.length - 1];
    const parts = [`${pending.label} · ${Math.max(0, Math.floor((Date.now() - pending.startedAt) / 1000))}s elapsed.`];
    if (total != null) parts.push(`Desk totals: ${total} invoices recorded, ${invoiceCounts.posted || 0} posted.`);
    if (latest) parts.push(`Latest activity #${latest.seq}: ${eventDescription(latest)[0]} (${date(latest.event_time || latest.known_at, true)}).`);
    parts.push('The submitted request is still pending. Browse existing results or pause future scheduled work; do not resubmit.');
    const message = parts.join(' ');
    setNotice('#action-notice', message);
    if (pending.selector) setNotice(pending.selector, message);
  }
  async function act(work, success, { label = 'Request pending', progressSelector = null } = {}) {
    if (state.busy) return;
    state.pendingAction = { label, selector: progressSelector, startedAt: Date.now(), waiting: true };
    setBusy(true); renderProgress();
    try {
      const result = await work();
      state.pendingAction.waiting = false;
      const message = typeof success === 'function' ? success(result) : success || 'The desk recorded the request.';
      setNotice('#action-notice', message, 'success');
      if (progressSelector) setNotice(progressSelector, message, 'success');
      await refresh(true);
      refreshDataset(true);
      return result;
    } catch (error) {
      state.pendingAction.waiting = false;
      const message = `${error.message}${error.hint ? ` ${error.hint}` : ''}`;
      setNotice('#action-notice', message, 'error');
      if (progressSelector) setNotice(progressSelector, message, 'error');
      throw error;
    } finally { state.pendingAction = null; setBusy(false); }
  }
  function ingest(events) {
    const byId = new Map(state.events.map(event => [event.id, event]));
    for (const event of events) { byId.set(event.id, event); state.eventSeq = Math.max(state.eventSeq, event.seq || 0); }
    state.events = Array.from(byId.values()).sort((a, b) => a.seq - b.seq).slice(-500);
  }
  function eventDescription(event) {
    const p = event.payload || {};
    const definitions = {
      agent_action: [human(p.action_type), p.explanation || p.rule_id || 'Agent decision recorded', p.action_type === 'error' || p.action_type === 'policy_violation' ? 'danger' : ['held', 'payment_held', 'duplicate_rejected', 'post_rejected'].includes(p.action_type) ? 'warning' : ''],
      chip_response: [p.ok ? 'Authorization granted' : 'Authorization blocked', `${p.pay_id || ''} · ${p.ok ? 'Rules passed' : (p.reasons || [p.chip_reason]).filter(Boolean).map(human).join(', ')}`, p.ok ? '' : 'danger'],
      payment_executed: ['Simulated payment settled', `${money(p.amount_minor, p.currency)} to ${vendorName(p.vendor_id)}`, ''],
      payment_requested: ['Payment proposed', `${money(p.amount_minor, p.currency)} to ${vendorName(p.vendor_id)}`, 'muted'],
      escalation: ['Evidence packet opened', p.question || p.packet_id, 'warning'],
      controller_decision: ['Human decision recorded', `${human(p.decision)} · ${p.decided_by || 'Controller'}`, ''],
      invoice_received: ['Invoice received', p.invoice_id || p.document_path, 'muted'],
      vendor_message: ['Vendor message received', p.subject || p.claimed_supplier_name, 'warning'],
      system_error: ['System error', p.message || p.code, 'danger'],
      bank_rejected: ['Simulated bank refused settlement', p.detail || p.reason, 'danger'],
      provisioning: [p.ok ? 'Account provisioned' : 'Provisioning refused', `${vendorName(p.vendor_id)} · ${p.reason || p.account_hash_prefix}`, p.ok ? '' : 'warning'],
      judge_injection: ['Test input recorded', `A ${p.kind} entered the desk as untrusted input.`, 'warning'],
      score_update: ['Challenge score updated', `${p.headline?.caught || 0} caught, ${p.headline?.missed || 0} missed, ${p.headline?.pending || 0} pending`, 'muted'],
      day_boundary: ['Simulation day advanced', date(p.date), 'muted']
    };
    return definitions[event.type] || [human(event.type), p.description || p.po_id || p.gr_id || event.actor || event.source || 'Evidence recorded', 'muted'];
  }
  function eventRow(event) {
    const [title, detail, tone] = eventDescription(event);
    return `<button class="event-row" data-event="${escape(event.id)}"><span class="event-symbol ${tone}">${tone === 'danger' ? '×' : tone === 'warning' ? '!' : '✓'}</span><span class="event-main"><strong>${escape(title)}</strong><p>${escape(detail)}</p>${event.payload?.rule_id ? `<div class="event-rule">Rule ${escape(event.payload.rule_id)}</div>` : ''}</span><span class="event-time">${escape(shortTime(event.event_time))}<br><span class="mono">#${event.seq}</span></span></button>`;
  }
  function applyClock(data) {
    state.clock = data;
    state.clockReceivedAt = Date.now();
    renderClock();
  }
  function renderClock() {
    const clock = state.clock;
    const stale = clock && state.clockReceivedAt && Date.now() - state.clockReceivedAt > 15000;
    $('#sim-time').textContent = clock ? date(clock.sim_now, true) : 'Waiting for the desk';
    $('#sim-time').title = clock?.sim_now || 'No clock response received';
    $('#clock-state').textContent = clock ? `${stale ? 'Last reported · ' : ''}${clock.running ? 'Running' : 'Paused'}` : 'Waiting';
    $('#run-button').textContent = clock?.running ? 'Running' : 'Run desk';
    $('#run-button').disabled = state.busy || state.pausePending || !clock || clock.running;
    $('#pause-button').textContent = state.pausePending ? 'Pausing…' : 'Pause';
    $('#pause-button').disabled = state.pausePending || (!state.busy && clock?.running === false);
    $('#step-button').disabled = state.busy || state.pausePending || !clock || clock.running;
    $('#clock-note').textContent = stale ? 'Clock status is stale; this is the last reported date.' : clock?.running ? 'Processes scheduled arrivals. Pause stops future work.' : state.pendingAction?.waiting ? 'Paused. Work already submitted may still finish.' : clock?.next_known_at ? `Next arrival: ${date(clock.next_known_at, true)}` : 'Run processes scheduled arrivals; +1 day processes one day.';
  }
  async function pauseDesk() {
    if (state.pausePending || !ensureController()) return;
    state.pausePending = true;
    const revision = ++state.clockRevision;
    renderClock();
    setNotice('#clock-notice', 'Pausing future scheduled work. Submitted requests may still finish.');
    try {
      const result = await api('/clock/pause', { method: 'POST', controller: true });
      if (revision === state.clockRevision) applyClock(result);
      setNotice('#clock-notice', 'Desk paused. Work already submitted may still finish.', 'success');
    } catch (error) { setNotice('#clock-notice', `${error.message} Pause is not confirmed.`, 'error'); }
    finally { state.pausePending = false; renderClock(); }
  }
  function authorizationState(event) {
    const response = event?.payload;
    const indicator = event?.indicator;
    if (!response) return { state: 'unknown', label: 'NO FPGA RESULT', reason: 'No recorded payment response is available.' };
    if (response.signer === 'stub' || indicator?.state === 'software') return { state: 'software', label: response.ok === true ? 'SIMULATED APPROVAL' : response.ok === false ? 'SIMULATED REJECTION' : 'SOFTWARE SIMULATION', reason: 'Software result; this is not an FPGA decision.' };
    if (response.signer !== 'hw') return { state: 'unknown', label: 'UNVERIFIED RESPONSE', reason: 'The response is not identified as an FPGA result.' };
    const reasons = { authenticated_pay: 'Verified payment authorization', not_allowlisted: 'Account is not on the approved list', needs_button: 'Human approval required: unusual payment or cumulative vendor spending threshold reached', approval: 'Human approval required: unusual payment or cumulative vendor spending threshold reached', account: 'Destination account does not match the FPGA approved account', in_hold: 'Bank-change waiting period has not ended', missing_hardware_proof: 'No authenticated FPGA response was recorded', invalid_hardware_proof: 'Recorded hardware proof could not be verified' };
    const detail = indicator?.reason ? reasons[indicator.reason] || human(indicator.reason) : 'No verified FPGA decision was recorded.';
    const reason = `${indicator?.op ? `${indicator.op.replaceAll('_', ' ')} · ` : ''}${detail}`;
    if (indicator?.state === 'approved' && indicator.proof_verified === true && response.ok === true) return { state: 'approved', label: 'APPROVED', reason };
    if (indicator?.state === 'blocked' && indicator.proof_verified === true && response.ok === false) return { state: 'blocked', label: 'BLOCKED', reason };
    return { state: 'unknown', label: 'UNVERIFIED RESPONSE', reason };
  }
  function paymentAuthorization(payment) {
    if (!payment?.chip) return null;
    return { id: payment.chip_event_id, seq: payment.chip_seq, event_time: payment.chip_event_time, payload: payment.chip, indicator: payment.chip_indicator };
  }
  function authorizationMarkup(event, { historical = false, live = false, unavailable = false, software = false, compact = false } = {}) {
    const result = unavailable ? { state: 'unknown', label: 'STATUS UNAVAILABLE', reason: 'Live status is unavailable or stale. Historical responses remain in Payments.' } : software ? { state: 'software', label: 'SOFTWARE SIMULATION', reason: 'The current signer is software. This is not a live FPGA result.' } : authorizationState(event);
    const context = live ? 'Response to this live request' : historical ? 'Historical recorded response' : software ? 'Current signer mode' : 'Latest recorded FPGA response';
    const identity = event?.payload?.pay_id || '';
    const timestamp = event?.event_time ? date(event.event_time, true) : '';
    return `<div class="authorization-result ${result.state}${compact ? ' compact' : ''}" role="status"><span class="authorization-lamp" aria-hidden="true"></span><div><span class="eyebrow">${escape(context)}${(historical || live) && ['approved', 'blocked'].includes(result.state) ? ' · REAL FPGA' : ''}</span><strong>${escape(result.label)}</strong><p>${escape(result.reason)}</p>${identity || timestamp ? `<span class="authorization-meta">${escape(identity)}${timestamp ? ` · ${escape(timestamp)}` : ''}</span>` : ''}${(historical || live) && event?.id ? `<div>${eventButton(event.id, 'Inspect recorded response')}</div>` : ''}</div></div>`;
  }
  function renderAuthorization() {
    const freshChip = state.chip && state.chipReceivedAt && Date.now() - state.chipReceivedAt <= 15000;
    const freshSummary = state.summary && state.summaryReceivedAt && Date.now() - state.summaryReceivedAt <= 15000;
    const html = authorizationMarkup(state.summary?.latest_authorization, { unavailable: !freshChip || !freshSummary, software: freshChip && freshSummary && state.chip.kind !== 'hw' });
    if ($('#latest-authorization').innerHTML !== html) $('#latest-authorization').innerHTML = html;
    $('#latest-authorization').className = 'latest-authorization';
  }
  function renderRail() {
    const chip = state.chip;
    renderAuthorization();
    if (!chip) {
      $('#hardware-mode').textContent = 'BOUNDARY UNAVAILABLE';
      $('#authorization-rail').dataset.status = 'unavailable';
      $('#chip-slots').textContent = '—';
      $('#chip-cap').textContent = 'Status not verified';
      $('#chip-meter').style.width = '0%';
      $('#chip-latest').textContent = 'Live boundary status unavailable';
      $$('[data-authorize]').forEach(button => { button.disabled = true; });
      if ($('#authorize-result')) setNotice('#authorize-result', 'Boundary status is unavailable. Reconnect the signer and refresh before authorizing.', 'error');
      if ($('#detail-dialog').open && $('#dialog-kicker').textContent === 'Authorization boundary') hardwareDetails();
      return;
    }
    $('#authorization-rail').dataset.status = 'connected';
    $('#hardware-mode').textContent = chip.kind === 'hw' ? 'REAL FPGA' : 'SOFTWARE SIMULATION';
    $('#chip-slots').textContent = chip.slots_used ?? '—';
    $('#chip-cap').textContent = `${money(chip.daily_spent_minor)} / ${money(chip.daily_cap_minor)}`;
    const ratio = chip.daily_cap_minor ? Math.min(100, Math.max(0, 100 * chip.daily_spent_minor / chip.daily_cap_minor)) : 0;
    $('#chip-meter').style.width = `${ratio}%`;
    const latest = state.summary?.latest_authorization;
    $('#chip-latest').textContent = latest ? `${authorizationState(latest).label} · ${latest.payload?.pay_id || latest.id}` : 'No recorded response';
  }
  function renderOverview() {
    const summary = state.summary;
    if (!summary) return;
    const inv = summary.invoices || {};
    const decisions = Object.values(inv).reduce((sum, n) => sum + n, 0);
    const blocked = summary.authorizations?.blocked || 0;
    const cards = [
      ['Invoices in the desk', String(decisions), `${inv.posted || 0} posted · ${(inv.held || 0) + (inv.escalated || 0)} awaiting resolution`],
      ['Waiting for a person', String(summary.open_packets || 0), 'Full evidence packets, ready to review'],
      ['Blocked authorizations', String(blocked), `${(summary.authorizations?.approved || 0)} authorization responses approved`],
      ['Open payables · EUR', money(summary.open_amounts_minor?.EUR || 0), Object.keys(summary.open_amounts_minor || {}).filter(ccy => ccy !== 'EUR').map(ccy => `${ccy}: ${money(summary.open_amounts_minor[ccy], ccy)}`).join(' · ') || 'Open, held and scheduled payables']
    ];
    $('#kpis').innerHTML = cards.map(([label, number, detail]) => `<div class="kpi"><span>${escape(label)}</span><strong>${escape(number)}</strong><small>${escape(detail)}</small></div>`).join('');
    const agents = [ ['invoice_agent', 'Invoice agent', 'Reads bills. Matches purchase orders and receipts.', '▤'], ['vendor_agent', 'Vendor agent', 'Checks vendors. Verifies changes to bank details.', '▦'], ['payment_agent', 'Payment agent', 'Schedules payments. Requests authorization.', '⇄'] ];
    $('#agents').innerHTML = agents.map(([id, title, description, icon]) => {
      const stats = summary.agents?.[id] || {};
      const last = stats.last?.payload;
      return `<article class="agent-card"><div class="agent-top"><span class="agent-symbol">${icon}</span><span class="agent-state"><i class="dot ${stats.errors ? 'orange' : 'green'}"></i>${stats.errors ? `${stats.errors} errors logged` : stats.actions ? 'Monitoring' : 'Ready'}</span></div><h3>${title}</h3><p>${description}</p><div class="agent-last"><strong>${stats.actions || 0} actions</strong>${escape(last ? human(last.action_type) : 'Awaiting its first event')}</div></article>`;
    }).join('');
    $('#event-count').textContent = `${state.clock?.events_total ?? summary.latest_seq ?? 0} events`;
    const open = state.packets.filter(p => p.status === 'open');
    $('#packet-count').textContent = open.length;
    renderModelStatus();
    renderScore();
  }
  function renderModelStatus() {
    const info = state.summary?.llm;
    if (!info) return;
    const labels = {
      responses_recorded: `${info.responses_recorded} API responses recorded`,
      configured_unverified: 'API configured · not yet verified',
      credentials_missing: 'No API credentials configured',
      custom_client_unverified: 'Custom client · live API unverified'
    };
    const explanation = info.mode === 'credentials_missing' ? 'Set ANTHROPIC_API_KEY on the server for unstructured document extraction. Structured challenges still run.' : info.mode === 'responses_recorded' ? 'Recorded responses prove past API calls; they do not guarantee current service availability.' : 'Structured challenges use deterministic extraction and policy checks. A configured client alone does not prove a live API call.';
    const html = `<span class="dot ${info.api_verified ? 'green' : 'orange'}"></span><div><strong>${escape(labels[info.mode] || 'API status unknown')}</strong><p>${escape(explanation)}</p></div>${info.model ? `<span class="model-name mono">${escape(info.model)}</span>` : ''}`;
    $('#model-status').innerHTML = html;
  }
  function pageRows(key, rows, size = 35) {
    const pages = Math.max(1, Math.ceil(rows.length / size));
    const page = Math.min(Math.max(state.pages[key] || 0, 0), pages - 1);
    state.pages[key] = page;
    return { rows: rows.slice(page * size, (page + 1) * size), footer: `<div class="pagination"><span>${rows.length ? page * size + 1 : 0}–${Math.min((page + 1) * size, rows.length)} of ${rows.length}</span><div class="pagination-controls"><button class="button small" data-page="${key}" data-delta="-1" ${page === 0 ? 'disabled' : ''}>Previous</button><button class="button small" data-page="${key}" data-delta="1" ${page === pages - 1 ? 'disabled' : ''}>Next</button></div></div>` };
  }
  const matches = (row, term) => JSON.stringify(row).toLowerCase().includes(term.toLowerCase());
  function renderPayments() {
    const term = $('#payment-search').value;
    if (state.paymentTab === 'requests') {
      const rows = state.payments.filter(row => matches({ ...row, vendor_name: vendorName(row.vendor_id) }, term));
      const page = pageRows('payments', rows);
      $('#payments-table').innerHTML = rows.length ? `<table><thead><tr><th>Payment / vendor</th><th>Account</th><th class="number">Amount</th><th>Status</th><th>Boundary result</th><th></th></tr></thead><tbody>${page.rows.map(p => `<tr><td><span class="table-primary">${escape(vendorName(p.vendor_id))}</span><span class="table-secondary mono">${escape(p.pay_id)}</span></td><td class="mono">${escape(p.account_masked || '—')}</td><td class="number">${escape(money(p.amount_minor, p.currency))}</td><td>${statusPill(p.status)}</td><td><span class="recorded-verdict ${authorizationState(paymentAuthorization(p)).state}">${escape(authorizationState(paymentAuthorization(p)).label)}</span><span class="table-secondary">${escape((p.chip?.reasons || []).map(human).join(', ') || p.bank_reason || '')}</span></td><td><button class="row-button" data-payment="${escape(p.pay_id)}">Evidence</button></td></tr>`).join('')}</tbody></table>${page.footer}` : empty('No matching payment requests', 'A scheduled payment will appear here when the payment agent requests authorization.');
    } else {
      const rows = [...state.payables].reverse().filter(row => matches({ ...row, vendor_name: vendorName(row.vendor_id) }, term));
      const page = pageRows('payables', rows);
      $('#payments-table').innerHTML = rows.length ? `<table><thead><tr><th>Invoice / vendor</th><th>Due date</th><th class="number">Amount</th><th>Status</th><th>Reason / journal</th></tr></thead><tbody>${page.rows.map(p => `<tr><td><span class="table-primary">${escape(vendorName(p.vendor_id))}</span><span class="table-secondary mono">${escape(p.inv_id)}</span></td><td>${escape(date(p.due_date))}</td><td class="number">${escape(money(p.amount_minor, p.currency))}</td><td>${statusPill(p.status)}</td><td><span class="table-secondary">${escape(p.held_reason || '')}</span><button class="row-button mono" data-entry="${escape(p.entry_id)}">${escape(p.entry_id)}</button></td></tr>`).join('')}</tbody></table>${page.footer}` : empty('No matching payables', 'Accepted invoices create actual double-entry payables here.');
    }
    $('#bank-accounts').innerHTML = state.banks.map(a => `<article class="bank-card">${pill('Simulated bank', 'muted')}<h3>${escape(a.currency)} operating account</h3><strong>${escape(money(a.balance_minor, a.currency))}</strong><p class="footnote">${escape(a.bank || 'Test bank')} · No real funds</p></article>`).join('');
  }
  function renderInvoices() {
    const rows = [...state.invoices].reverse().filter(row => matches({ ...row, vendor_name: vendorName(row.vendor_id) }, $('#invoice-search').value));
    const page = pageRows('invoices', rows);
    $('#invoice-count').textContent = `${state.invoices.length} invoices received by the desk`;
    const loaded = state.datasetSummary?.counts?.invoices;
    $('#inbox-dataset-note').textContent = loaded == null ? 'Browse all source invoices in Dataset, including those not processed yet.' : `${loaded} source invoices are loaded in Dataset. This table shows only invoices the desk has received.`;
    $('#invoices-table').innerHTML = rows.length ? `<table><thead><tr><th>Invoice / vendor</th><th>Status</th><th>Rule</th><th>Reason</th><th>Journal</th></tr></thead><tbody>${page.rows.map(row => `<tr><td><span class="table-primary">${escape(row.invoice_number || row.invoice_id)}</span><span class="table-secondary">${escape(vendorName(row.vendor_id))}</span></td><td>${statusPill(row.status)}</td><td class="mono">${escape(row.rule_id || '—')}</td><td><span class="table-secondary">${escape(human(row.reason || ''))}</span></td><td>${row.entry_id ? `<button class="row-button mono" data-entry="${escape(row.entry_id)}">${escape(row.entry_id)}</button>` : '—'}</td></tr>`).join('')}</tbody></table>${page.footer}` : empty('No invoices received yet', 'The loaded source files are in Dataset. Run the desk to process scheduled arrivals, or open Demo to add a test invoice.');
  }
  async function refreshDataset(force = false) {
    if (state.datasetLoading) return;
    const tab = state.datasetTab;
    const jobs = [];
    if (force || !state.datasetSummary) jobs.push(['summary', '/dataset/summary']);
    if (force || !state.datasetRows[tab]) jobs.push([tab, `/dataset/${tab}?limit=1000`]);
    if (!jobs.length) return;
    state.datasetLoading = true;
    const results = await Promise.allSettled(jobs.map(async ([key, path]) => {
      const data = await api(path);
      if (key === 'summary') state.datasetSummary = data;
      else state.datasetRows[key] = data;
      renderDataset();
      if (state.view === 'invoices') renderInvoices();
    }));
    const errors = results.filter(r => r.status === 'rejected').map(r => r.reason.message);
    setNotice('#dataset-error', errors.length ? `Dataset: ${errors.join(' ')} Previously loaded records remain available.` : '', 'error');
    state.datasetLoading = false;
    if (tab !== state.datasetTab && !state.datasetRows[state.datasetTab]) refreshDataset();
  }
  function datasetDocumentLink(row, label = 'Open source') {
    return typeof row.document_url === 'string' && row.document_url.startsWith('/dataset/documents/') ? `<a class="row-button" href="${escape(row.document_url)}" target="_blank" rel="noopener">${escape(label)} ↗</a>` : '<span class="subtle">No source file</span>';
  }
  function renderDataset() {
    const summary = state.datasetSummary;
    if (summary) {
      const counts = summary.counts || {};
      $('#dataset-summary').innerHTML = [['Invoices', counts.invoices], ['Purchase orders', counts.purchase_orders], ['Receipts', counts.receipts], ['Vendors', counts.vendors]].map(([title, count]) => `<div class="dataset-stat"><strong>${count ?? '—'}</strong><span>${title}</span></div>`).join('');
      const formats = Object.entries(summary.formats || {}).filter(([, n]) => n > 0).map(([kind, n]) => `${n} ${kind === 'structured' ? 'JSON' : kind.toUpperCase()}`).join(' · ');
      $('#dataset-notice').textContent = `${formats ? `${formats}. ` : ''}Queued files have not been processed. ${summary.first_invoice_at ? `First scheduled invoice: ${date(summary.first_invoice_at, true)}.` : ''}`;
    }
    $$('#dataset-tabs button').forEach(button => button.classList.toggle('active', button.dataset.datasetTab === state.datasetTab));
    const collection = state.datasetRows[state.datasetTab];
    if (!collection) { $('#dataset-table').innerHTML = empty('Loading source records…'); return; }
    const rows = collection.items.filter(row => matches(row, $('#dataset-search').value));
    const page = pageRows(`dataset-${state.datasetTab}`, rows);
    const source = state.datasetTab === 'invoices';
    let headers, cells;
    if (source) {
      headers = '<th>Invoice / supplier</th><th>Format</th><th>Scheduled arrival · UTC</th><th>Status</th><th>Source document</th>';
      cells = row => `<td><button class="row-button mono" data-dataset-record="${escape(row.id)}">${escape(row.id)}</button><span class="table-secondary">${escape(row.supplier_name || row.supplier_id || row.sender || 'Unknown supplier')}</span></td><td>${escape((row.format || '—').toUpperCase())}</td><td>${escape(date(row.known_at, true))}</td><td>${statusPill(row.status || 'queued')}</td><td>${datasetDocumentLink(row)}</td>`;
    } else if (state.datasetTab === 'purchase-orders') {
      headers = '<th>Purchase order / supplier</th><th>Order date</th><th>Lines</th><th class="number">Net order value</th><th></th>';
      cells = row => `<td><span class="table-primary mono">${escape(row.id)}</span><span class="table-secondary">${escape(row.supplier_name || row.supplier_id)}</span></td><td>${escape(date(row.order_date || row.created_at))}</td><td>${row.lines?.length ?? '—'}</td><td class="number">${escape(money(row.total_minor, row.currency))}</td><td><button class="row-button" data-dataset-record="${escape(row.id)}">Details</button></td>`;
    } else if (state.datasetTab === 'receipts') {
      headers = '<th>Receipt</th><th>Purchase order</th><th>Received</th><th class="number">Quantity</th><th></th>';
      cells = row => `<td class="mono">${escape(row.id)}</td><td>${escape(row.po_id)}</td><td>${escape(date(row.received_at, true))}</td><td class="number">${escape(row.qty)} ${escape(row.uom || '')}</td><td><button class="row-button" data-dataset-record="${escape(row.id)}">Details</button></td>`;
    } else {
      headers = '<th>Supplier</th><th>Country</th><th>Currency</th><th>Terms</th><th></th>';
      cells = row => `<td><span class="table-primary">${escape(row.legal_name || row.name)}</span><span class="table-secondary mono">${escape(row.id)}</span></td><td>${escape(row.country)}</td><td>${escape(row.currency)}</td><td>${escape(row.terms)}</td><td><button class="row-button" data-dataset-record="${escape(row.id)}">Details</button></td>`;
    }
    $('#dataset-table').innerHTML = rows.length ? `<table><thead><tr>${headers}</tr></thead><tbody>${page.rows.map(row => `<tr>${cells(row)}</tr>`).join('')}</tbody></table>${page.footer}` : empty('No matching source records', 'Try a different search or dataset tab.');
  }
  function openDatasetRecord(id) {
    const row = state.datasetRows[state.datasetTab]?.items.find(item => item.id === id);
    if (!row) return;
    const tab = state.datasetTab;
    const fact = (title, value) => `<div class="detail-fact"><span>${escape(title)}</span><strong>${escape(value ?? '—')}</strong></div>`;
    let content = '';
    if (tab === 'invoices') {
      content = `${statusPill(row.status || 'queued')}<p class="detail-context">${row.status === 'queued' ? 'This source file is loaded and waiting for its scheduled arrival.' : 'This invoice has entered the desk. Its current processing status is shown above.'}</p><div class="detail-grid">${fact('Supplier', row.supplier_name || row.supplier_id)}${fact('Scheduled arrival', date(row.known_at, true))}${fact('Document format', String(row.format || '').toUpperCase())}${fact('Received through', row.received_via)}${fact('Sender', row.sender)}${fact('File', row.document_name)}</div><div class="detail-section">${datasetDocumentLink(row, 'Open original document')}</div>`;
    } else if (tab === 'purchase-orders') {
      content = `<div class="detail-grid">${fact('Supplier', row.supplier_name || row.supplier_id)}${fact('Order date', date(row.order_date || row.created_at))}${fact('Net order value', money(row.total_minor, row.currency))}${fact('Buyer', row.buyer)}</div><section class="detail-section"><h3>Ordered items</h3><div class="table-panel"><table><thead><tr><th>Item</th><th class="number">Quantity</th><th class="number">Unit price</th><th>Delivery</th></tr></thead><tbody>${(row.lines || []).map(line => `<tr><td><span class="table-primary">${escape(line.description || line.sku)}</span><span class="table-secondary mono">${escape(line.sku)}</span></td><td class="number">${escape(line.qty)} ${escape(line.uom)}</td><td class="number">${escape(money(line.unit_price_minor, line.currency || row.currency))}</td><td>${escape(date(line.requested_delivery))}</td></tr>`).join('')}</tbody></table></div></section>`;
    } else if (tab === 'receipts') {
      content = `<div class="detail-grid">${fact('Purchase order', row.po_id)}${fact('Supplier', row.supplier_name || row.supplier_id)}${fact('Quantity received', `${row.qty ?? '—'} ${row.uom || ''}`)}${fact('Received at', date(row.received_at, true))}${fact('Booked at', date(row.booked_at, true))}${fact('Recorded by', row.dock_clerk)}</div>${row.note ? `<p class="detail-context">${escape(row.note)}</p>` : ''}`;
    } else {
      content = `<div class="detail-grid">${fact('Legal name', row.legal_name || row.name)}${fact('Country', row.country)}${fact('Currency', row.currency)}${fact('Payment terms', row.terms)}${fact('Email', row.email)}${fact('Account', row.account_masked)}</div>`;
    }
    showDetail('Dataset / Source record', `<h2 class="detail-title">${escape(row.id)}</h2>${content}<details class="supporting-details detail-section"><summary>Source metadata</summary><pre class="json">${escape(JSON.stringify(row, null, 2))}</pre></details>`);
  }
  function renderPackets() {
    const rows = state.packets.filter(p => p.status === state.packetTab);
    $('#packets').innerHTML = rows.length ? rows.map(p => `<article class="packet-card"><div class="packet-top">${statusPill(p.status === 'open' ? p.severity : p.status)}<span class="mono subtle">${escape(p.id)}</span></div><h3>${escape(p.question)}</h3><p>${escape(p.context.length > 220 ? `${p.context.slice(0, 220)}…` : p.context)}</p>${p.materiality?.minor != null ? `<span class="amount">${escape(money(p.materiality.minor, p.materiality.currency))}</span>` : ''}<div class="packet-bottom"><span class="subtle">${p.evidence_rows?.length || 0} evidence items<br>${escape(date(p.created_at))}</span><button class="button ${p.status === 'open' ? 'dark' : ''}" data-packet="${escape(p.id)}">${p.status === 'open' ? 'Review evidence' : 'Read decision'}</button></div></article>`).join('') : empty(`No ${state.packetTab} packets`, state.packetTab === 'open' ? 'The desk will bring decisions here with the supporting evidence.' : 'Completed decisions stay in the audit trail.');
  }
  function renderVendors() {
    const rows = state.vendors.filter(row => matches(row, $('#vendor-search').value));
    const page = pageRows('vendors', rows);
    $('#vendor-count').textContent = `${state.vendors.length} vendor records · ${state.vendors.filter(v => v.on_chip).length} accounts provisioned`;
    $('#vendors-table').innerHTML = rows.length ? `<table><thead><tr><th>Legal entity</th><th>Country / currency</th><th>Status</th><th>Boundary account</th><th></th></tr></thead><tbody>${page.rows.map(v => `<tr><td><span class="table-primary">${escape(v.legal_name)}</span><span class="table-secondary mono">${escape(v.id)}</span></td><td>${escape(v.country)} / ${escape(v.currency)}</td><td>${statusPill(v.status)}</td><td>${v.on_chip ? pill('Provisioned') : pill('Not provisioned', 'warning')}<span class="table-secondary mono">${escape(v.account_hash_prefix || 'No active account')}</span></td><td><button class="row-button" data-vendor="${escape(v.id)}">Account history</button></td></tr>`).join('')}</tbody></table>${page.footer}` : empty('No matching vendors');
    for (const id of ['judge-vendor', 'invoice-judge-vendor']) {
      const select = $(`#${id}`);
      const selected = select.value;
      const options = `<option value="">${id === 'judge-vendor' ? 'Select a vendor' : 'Use a custom supplier name'}</option>` + state.vendors.map(v => `<option value="${escape(v.id)}">${escape(v.legal_name)} (${escape(v.id)})</option>`).join('');
      if (select.innerHTML !== options) { select.innerHTML = options; select.value = selected; }
    }
  }
  function renderLedger() {
    const page = pageRows('ledger', [...state.ledger].reverse());
    $('#ledger-table').innerHTML = state.ledger.length ? `<table><thead><tr><th>Journal / description</th><th>Effective date</th><th>Posted by</th><th>Lines</th><th>Evidence</th><th></th></tr></thead><tbody>${page.rows.map(entry => `<tr><td><span class="table-primary">${escape(entry.description)}</span><span class="table-secondary mono">${escape(entry.id)}</span></td><td>${escape(date(entry.effective_date))}</td><td>${escape(human(entry.posted_by))}</td><td>${entry.lines.length}</td><td>${entry.evidence.length} source${entry.evidence.length === 1 ? '' : 's'}</td><td><button class="row-button" data-entry="${escape(entry.id)}">Inspect</button></td></tr>`).join('')}</tbody></table>${page.footer}` : empty('No journal entries yet');
    const reconcile = state.summary?.reconciliation;
    setNotice('#ledger-reconciliation', reconcile?.ok ? 'Reconciled: open AP items and retention match the ledger control accounts.' : 'AP reconciliation needs attention. Inspect the source entries and the reconciliation evidence.', reconcile?.ok ? 'success' : 'warning');
    const balances = Object.entries(state.balances).flatMap(([account, currencies]) => Object.entries(currencies).map(([currency, amount]) => ({ account, currency, amount })));
    $('#balances-table').innerHTML = balances.length ? `<table><thead><tr><th>Account</th><th>Currency</th><th class="number">Balance</th></tr></thead><tbody>${balances.map(b => `<tr><td>${escape(b.account)}</td><td>${escape(b.currency)}</td><td class="number">${escape(money(b.amount, b.currency))}</td></tr>`).join('')}</tbody></table>` : empty('No balances to show');
    $('#ledger-more').hidden = !state.ledgerHasMore;
  }
  function renderEvents() {
    const term = $('#event-search').value;
    const rows = [...state.events].reverse().filter(e => matches(e, term));
    const page = pageRows('events', rows, 50);
    $('#audit-list').innerHTML = rows.length ? page.rows.map(eventRow).join('') + page.footer : empty('No matching audit events');
  }
  function renderScore() {
    const score = state.summary?.score;
    $('#score').innerHTML = score ? `<div class="score-grid">${[['Caught', score.headline?.caught], ['Missed', score.headline?.missed], ['Pending', score.headline?.pending], ['False positives', score.headline?.fp]].map(([title, n]) => `<div class="score-tile"><span>${title}</span><strong>${n ?? 0}</strong></div>`).join('')}</div><div class="score-source"><strong>${escape(money(score.euros_at_risk_prevented_minor))}</strong> at-risk amount prevented, as scored.<br>${score.entries_without_evidence ?? 0} journal entries without evidence.<br>Score event as of ${escape(date(score.as_of, true))}. Pending cases are not successes.</div>` : empty('No scored result yet', 'Scoring appears when the running world emits an answer-key comparison. Demo fixtures are reported separately.');
  }
  const liveRunStorageKey = id => `payguard.live-run.${id}`;
  function pendingLiveRun(id) {
    try { return sessionStorage.getItem(liveRunStorageKey(id)); } catch { return null; }
  }
  const liveActiveKey = 'payguard.active-live-job';
  const liveRequestKey = id => `payguard.live-body.${id}`;
  state.liveJob = null;
  state.liveRunId = null;
  state.livePolling = false;
  state.liveSubmitting = false;
  state.liveExampleLoading = false;
  state.liveLastPoll = 0;
  state.liveLastPollAttempt = 0;
  state.liveError = '';
  try {
    const saved = JSON.parse(sessionStorage.getItem(liveActiveKey) || 'null');
    if (saved?.run_id) { state.liveRunId = saved.run_id; state.liveJob = { run_id: saved.run_id, kind: saved.kind, job_status: 'reconnecting', evidence_events: [] }; }
  } catch { /* A missing or malformed browser checkpoint cannot authorize anything. */ }
  function liveJobWorking() {
    return state.liveSubmitting || ['queued', 'running', 'submitting'].includes(state.liveJob?.job_status);
  }
  function updateLiveControls() {
    const working = state.busy || liveJobWorking();
    $('#process-live-invoice').disabled = working;
    $('#load-live-example').disabled = working || state.liveExampleLoading;
    $('#process-live-invoice').textContent = liveJobWorking() ? 'Processing…' : pendingLiveRun('invoice') ? 'Retry this invoice' : 'Process live';
    $$('[data-live-scenario]').forEach(button => { button.disabled = working; });
    $$('#invoice-judge-form button[type="submit"]').forEach(button => { button.disabled = working; });
  }
  function liveJobEvents(job) {
    const rows = job?.evidence_events || job?.result?.evidence_events || [];
    return rows.filter(e => e && typeof e === 'object' && e.id).sort((a, b) => (a.seq || 0) - (b.seq || 0));
  }
  function liveActor(event) {
    return event.payload?.agent || event.actor || event.source || 'desk';
  }
  function liveEventMessage(event) {
    const p = event.payload || {};
    if (event.type === 'agent_action') return p.explanation || p.reason || human(p.action_type) || 'Action recorded';
    if (event.type === 'agent_progress') return p.message || human(p.stage || p.action_type);
    if (event.type === 'invoice_received') return `Received ${p.invoice_id || 'invoice'}${p.format ? ` · ${p.format}` : ''}.`;
    if (event.type === 'invoice_extracted') return 'Invoice fields extracted from the submitted document.';
    if (event.type === 'system_error') return p.message || p.error || p.code || 'Processing error recorded';
    return eventDescription(event)[1];
  }
  function liveChipEvent(job) {
    if (job?.chip_event) return job.chip_event;
    const chips = liveJobEvents(job).filter(e => e.type === 'chip_response');
    const event = chips[chips.length - 1];
    if (event) return { ...event, indicator: job.chip_indicator || event.indicator };
    return paymentAuthorization(state.payments.find(p => p.pay_id === (job?.pay_id || job?.result?.pay_id)));
  }
  function renderLiveJob() {
    updateLiveControls();
    const job = state.liveJob;
    $('#live-workspace').hidden = !job;
    const badge = $('#live-job-status');
    if (!job) { badge.textContent = 'Ready'; badge.className = 'pill muted'; return; }
    const status = job.job_status || 'queued';
    const terminal = ['completed', 'failed', 'interrupted'].includes(status);
    const result = job.result || {};
    badge.textContent = ({ queued: 'Queued', running: 'Live · processing', completed: 'Processing complete', failed: 'Needs attention', interrupted: 'Interrupted', submitting: 'Submitting', reconnecting: 'Restoring live run', unknown: 'Check connection' })[status] || human(status);
    badge.className = `pill ${['failed', 'interrupted'].includes(status) ? 'danger' : ['queued', 'running', 'submitting'].includes(status) ? 'warning' : 'muted'}`;
    const events = liveJobEvents(job);
    $('#live-source-record').hidden = !job.source_text;
    $('#live-submitted-text').textContent = job.source_text || '';
    const paymentId = job.pay_id || result.pay_id;
    const invoiceId = job.invoice_id || result.invoice_id;
    $('#live-run-meta').innerHTML = `<div><strong>${escape(invoiceId || 'New incoming invoice')}</strong>${paymentId ? ` · ${escape(paymentId)}` : ''}<br><span class="mono">Run ${escape(job.run_id)}</span></div><span>${job.submitted_at ? `Submitted ${escape(date(job.submitted_at, true))}` : 'Waiting for server receipt'}</span>`;
    const progress = job.progress || [];
    $('#live-agents').innerHTML = [['invoice_agent', 'Invoice agent'], ['vendor_agent', 'Vendor agent'], ['payment_agent', 'Payment agent']].map(([id, name]) => {
      const actions = events.filter(e => liveActor(e) === id && ['agent_action', 'agent_progress'].includes(e.type));
      const updates = progress.filter(p => p.agent === id);
      const latest = actions[actions.length - 1];
      const update = updates[updates.length - 1];
      let tone = update?.status === 'running' ? 'running' : update?.status === 'failed' ? 'failed' : update?.status === 'skipped' ? 'skipped' : latest || update ? 'done' : terminal ? 'skipped' : 'waiting';
      if (terminal && tone === 'running') tone = status === 'failed' || status === 'interrupted' ? 'failed' : 'done';
      const label = ({ waiting: 'Waiting', running: 'Working', failed: 'Needs attention', skipped: 'No action', done: 'Recorded' })[tone];
      const message = latest ? liveEventMessage(latest) : update?.message || (terminal ? 'No action from this agent was recorded for this invoice.' : 'Waiting for this invoice to reach the agent.');
      return `<article class="live-agent ${tone}"><div class="live-agent-header"><h3>${name}</h3>${pill(label, tone === 'failed' ? 'danger' : tone === 'running' ? 'warning' : 'muted')}</div><p>${escape(message)}</p></article>`;
    }).join('');
    const relevant = events.filter(e => ['invoice_received', 'invoice_extracted', 'agent_action', 'agent_progress', 'payment_requested', 'chip_response', 'payment_executed', 'bank_rejected', 'escalation', 'controller_decision', 'system_error', 'vendor_message'].includes(e.type));
    $('#live-event-count').textContent = `${relevant.length} recorded event${relevant.length === 1 ? '' : 's'}`;
    $('#live-timeline').innerHTML = relevant.length ? relevant.map((event, i) => {
      const p = event.payload || {};
      const [title] = eventDescription(event);
      const actor = ({ invoice_agent: 'Invoice agent', vendor_agent: 'Vendor agent', payment_agent: 'Payment agent', demo_operator: 'Incoming information' })[liveActor(event)] || human(liveActor(event));
      const titleText = event.type === 'agent_progress' ? human(p.stage || 'Working') : title;
      const bad = event.type === 'system_error' || event.type === 'bank_rejected' || (event.type === 'chip_response' && p.ok === false);
      return `<article class="live-step ${bad ? 'error' : ''}"><span class="live-step-index">${i + 1}</span><div class="live-step-content"><div class="live-step-meta"><span>${escape(actor)}</span><span>#${event.seq ?? '—'}</span></div><strong>${escape(titleText)}</strong><p>${escape(liveEventMessage(event))}</p>${p.rule_id ? `<div class="event-rule">Rule ${escape(p.rule_id)}</div>` : ''}${eventButton(event.id, 'Inspect evidence')}</div></article>`;
    }).join('') : `<div class="empty"><strong>${status === 'queued' ? 'Waiting for the current desk task' : 'Waiting for the first event'}</strong>${escape(progress[progress.length - 1]?.message || 'The timeline fills as the backend records work. No result is assumed.')}</div>`;
    const chipEvent = liveChipEvent(job);
    $('#live-fpga-result').innerHTML = chipEvent ? authorizationMarkup(chipEvent, { live: true }) : `<div class="authorization-result unknown" role="status"><span class="authorization-lamp" aria-hidden="true"></span><div><span class="eyebrow">This invoice</span><strong>${terminal ? 'NO FPGA REQUEST' : 'WAITING FOR REQUEST'}</strong><p>${escape(terminal ? 'Processing returned without a recorded FPGA response. Review the agent decision or error below.' : 'The light changes only when a verified FPGA response is recorded.')}</p></div></div>`;
    const packetIds = [...new Set([job.packet_id, result.packet_id, ...(result.packet_ids || [])].filter(Boolean))];
    const outcome = job.error || result.reason || result.held_reason || result.outcome || (terminal ? human(result.status || status) : progress[progress.length - 1]?.message || 'Processing the new input.');
    $('#live-next-action').innerHTML = `<div class="notice ${job.error ? 'error' : ''}">${escape(outcome)}</div>${packetIds.map(id => `<button class="button primary" data-packet="${escape(id)}">Review the evidence packet</button>`).join('')}${paymentId ? `<button class="button" data-payment="${escape(paymentId)}">Inspect this payment</button>` : ''}${result.journal_entry_ids?.length || result.journal_entries?.length ? '<a class="button" href="#ledger">Open ledger</a>' : ''}`;
    const stale = state.liveLastPoll && Date.now() - state.liveLastPoll > 15000;
    setNotice('#live-connection-note', state.liveError || (stale ? 'Live updates are delayed. Displaying the last response received; no new outcome is assumed.' : ''), 'warning');
  }
  function renderDemo() {
    const scenarios = state.demo?.scenarios;
    $('#known-purchase-orders').innerHTML = (state.invoiceOptions?.purchase_orders || []).map(po => `<option value="${escape(po.id)}">${escape(vendorName(po.supplier_id))} / ${escape(po.currency)}</option>`).join('');
    $('#demo-scenarios').innerHTML = scenarios?.length ? scenarios.map((s, n) => {
      const result = s.latest_run_id ? `<button class="button" data-live-result="${escape(s.latest_run_id)}">View last result</button>` : s.started ? `<button class="button" data-scenario="${escape(s.id)}">View result</button>` : '';
      return `<article class="scenario"><span class="scenario-no">TEST 0${n + 1}</span><h3>${escape(s.id === 'normal' ? 'Process a matching invoice' : s.title || human(s.id))}</h3><p>${escape(s.description || s.guard || '')}</p><div class="scenario-actions"><button class="button ${s.id === 'attack' ? 'primary' : 'dark'}" data-live-scenario="${escape(s.id)}" ${state.busy || liveJobWorking() ? 'disabled' : ''}>${pendingLiveRun(s.id) ? 'Retry live request' : 'Run new test'}</button>${result}</div></article>`;
    }).join('') : `<article class="scenario"><h3>Tests unavailable</h3><p>The demo routes are not available on this server. You can still submit a vendor message below.</p></article>`;
  }
  function render() {
    renderClock(); renderRail(); renderOverview(); renderProgress();
    if (state.view === 'dataset') renderDataset();
    if (state.view === 'payments') renderPayments();
    if (state.view === 'invoices') renderInvoices();
    if (state.view === 'evidence') renderPackets();
    if (state.view === 'vendors' || state.view === 'demo') renderVendors();
    if (state.view === 'ledger') renderLedger();
    if (state.view === 'events') renderEvents();
    if (state.view === 'demo') { renderDemo(); renderScore(); renderLiveJob(); }
  }
  async function refresh(force = false) {
    if (state.refreshing) return;
    state.refreshing = true;
    const jobs = [
      ['clock', '/state'], ['chip', '/chip/status'], ['summary', '/dashboard/summary'], ['packets', '/packets'], ['payments', '/dashboard/payments?limit=1000'],
    ];
    if (force || !state.vendors.length || ['vendors', 'demo'].includes(state.view)) jobs.push(['vendors', '/dashboard/vendors']);
    if (force || state.view === 'payments') jobs.push(['payables', '/payables'], ['banks', '/bank/accounts']);
    if (force || state.view === 'invoices') jobs.push(['invoices', '/dashboard/invoices']);
    if (force || state.view === 'demo') jobs.push(['demo', '/demo'], ['invoiceOptions', '/judge/invoice/options']);
    if (force || state.view === 'ledger') jobs.push(['ledger', '/dashboard/ledger?limit=1000']);
    const clockRevision = state.clockRevision;
    try {
      const results = await Promise.allSettled(jobs.map(([key, path]) => api(path).then(data => {
        if (key === 'clock' && clockRevision === state.clockRevision) applyClock(data);
        if (key === 'summary') { state.summary = data; state.summaryReceivedAt = Date.now(); renderAuthorization(); renderProgress(); }
        if (key === 'chip') { state.chip = data; state.chipReceivedAt = Date.now(); renderRail(); }
        return data;
      }).catch(error => {
        if (key === 'chip') { state.chip = null; state.chipReceivedAt = 0; renderRail(); }
        throw error;
      })));
      const errors = [];
      results.forEach((result, i) => {
        const [key] = jobs[i];
        if (result.status === 'rejected') {
          if (!(['demo', 'invoiceOptions'].includes(key) && result.reason.status === 404)) errors.push(`${human(key)}: ${result.reason.message}`);
          if (key === 'chip') state.chip = null;
          return;
        }
        const data = result.value;
        if (['clock', 'chip', 'summary'].includes(key)) return;
        if (key === 'ledger') { state.ledger = data.entries; state.balances = data.balances; state.ledgerHasMore = data.has_more; }
        else state[key] = ({ packets: data.packets, payments: data.payments, vendors: data.vendors, invoices: data.invoices, payables: data.payables, banks: data.accounts })[key] ?? data;
      });
      if (results[0].status === 'fulfilled') {
        const latest = state.clock.latest_seq || 0;
        const since = state.eventSeq ? state.eventSeq : Math.max(0, latest - 500);
        if (latest > state.eventSeq) {
          const data = await api(`/events?since_seq=${since}&limit=1000`);
          ingest(data.events || []);
        }
        state.receivedAt = new Date();
        connection(true, errors.length ? 'Connected · partial data' : 'Connected to desk');
      } else connection(false, 'Desk unavailable');
      setNotice('#global-error', errors.length ? `Live data unavailable: ${errors[0]} Last available results and loaded dataset files remain on screen.` : '', 'error');
      render();
    } catch (error) { connection(false, 'Desk unavailable'); setNotice('#global-error', `${error.message} Last available data remains on screen.`, 'error'); }
    finally { state.refreshing = false; }
  }
  function connectLive() {
    const generation = ++state.socketGeneration;
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let socket;
    try { socket = new WebSocket(`${protocol}//${location.host}/live?since_seq=${state.eventSeq}`); } catch { return; }
    state.ws = socket;
    socket.onmessage = message => {
      try {
        const data = JSON.parse(message.data);
        if (data.kind === 'event' && data.event) { ingest([data.event]); renderRail(); renderOverview(); renderProgress(); if (state.view === 'events') renderEvents(); }
      } catch { /* A malformed stream frame must not affect persisted REST state. */ }
    };
    socket.onclose = () => { if (generation === state.socketGeneration) setTimeout(connectLive, 3000); };
    socket.onerror = () => socket.close();
  }
  async function navigate() {
    const requested = location.hash.slice(1);
    const target = ({ judge: 'demo', overview: 'dataset', activity: 'events', controller: 'evidence' })[requested] || requested;
    state.view = pageInfo[target] ? target : 'dataset';
    $$('.view').forEach(view => { view.hidden = view.id !== `view-${state.view}`; });
    $$('[data-nav]').forEach(link => { link.classList.toggle('active', link.dataset.nav === state.view); if (link.dataset.nav === state.view) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current'); });
    const [eyebrow, title, description] = pageInfo[state.view];
    $('#page-eyebrow').textContent = eyebrow; $('#page-title').textContent = title; $('#page-description').textContent = description;
    $('#breadcrumb').textContent = navNames[state.view];
    document.title = `${navNames[state.view]} — PayGuard`;
    render(); refreshDataset(); await refresh();
  }
  function showDetail(kicker, html) {
    $('#dialog-kicker').textContent = kicker;
    $('#dialog-content').innerHTML = html;
    if (!$('#detail-dialog').open) $('#detail-dialog').showModal();
    $('#detail-dialog').scrollTop = 0;
  }
  async function openEvent(id) {
    showDetail('Audit evidence', empty('Loading event…'));
    try {
      const event = state.events.find(e => e.id === id) || await api(`/dashboard/events/${encodeURIComponent(id)}`);
      const [title, description, tone] = eventDescription(event);
      showDetail(`Audit event #${event.seq}`, `${pill(human(event.type), tone)}<h2 class="detail-title">${escape(title)}</h2><p class="detail-context">${escape(description)}</p><div class="detail-grid"><div class="detail-fact"><span>Actor</span><strong>${escape(event.actor || event.source)}</strong></div><div class="detail-fact"><span>Time recorded</span><strong>${escape(date(event.known_at, true))}</strong></div><div class="detail-fact"><span>Event ID</span><strong class="mono">${escape(event.id)}</strong></div><div class="detail-fact"><span>Source</span><strong>${escape(event.source)}</strong></div></div><div class="detail-section"><h3>Recorded payload</h3><pre class="json">${escape(JSON.stringify(event.payload, null, 2))}</pre></div>`);
    } catch (error) { showDetail('Evidence unavailable', `<div class="notice error">${escape(error.message)}</div>`); }
  }
  async function openPacket(id) {
    showDetail('Human review', empty('Loading evidence packet…'));
    try {
      const packet = await api(`/packets/${encodeURIComponent(id)}`);
      const p = packet;
      const payment = state.payments.find(row => row.packet_id === p.id);
      showDetail(`${p.id} / ${human(p.agent)}`, `${statusPill(p.status === 'open' ? p.severity : p.status)}<h2 class="detail-title">${escape(p.question)}</h2><p class="detail-context">${escape(p.context)}</p>${p.materiality?.minor != null ? `<div class="detail-grid"><div class="detail-fact"><span>Amount at issue</span><strong>${escape(money(p.materiality.minor, p.materiality.currency))}</strong></div><div class="detail-fact"><span>Response deadline</span><strong>${escape(p.expires_at ? date(p.expires_at, true) : 'No expiry set')}</strong></div></div>` : ''}<section class="detail-section"><h3>The evidence</h3>${(p.evidence_rows || []).map(e => `<button class="evidence-row" data-event="${escape(e.event_id)}"><span>${escape(human(e.role))}</span><p>${escape(e.summary)}</p><span class="mono">${escape(e.event_id)} · Inspect event</span></button>`).join('') || '<p class="subtle">No evidence rows attached.</p>'}</section><section class="detail-section"><h3>Agent recommendation</h3><div class="recommendation"><strong>${escape(p.recommendation?.label)}</strong><p>${escape(p.recommendation?.reason)}</p></div></section><section class="detail-section"><h3>If nobody answers</h3><p class="detail-context">${escape(p.if_no_answer)}</p></section>${p.status === 'open' ? `<section class="detail-section"><h3>Your decision</h3><label class="subtle" for="decision-note">Decision note</label><textarea id="decision-note" class="dialog-content-input" rows="3" maxlength="2000" placeholder="Add context for the audit trail"></textarea><div class="decision-options">${p.options.map(o => `<button class="button ${o.recommended ? 'dark' : ''}" data-decide="${escape(p.id)}" data-option="${escape(o.id)}">${escape(o.label)}${o.recommended ? ' (recommended)' : ''}</button>`).join('')}</div><p class="footnote">Authenticated controller software decision. Payment authorization remains subject to the boundary rules. This is not a physical button.</p><div id="decision-result" class="notice" role="status" hidden></div></section>` : `<div class="notice success">${p.decision ? `Decision recorded: ${escape(p.decision.label || p.decision.decision)} by ${escape(p.decision.decided_by)} at ${escape(date(p.decision.decided_at, true))}.${p.decision.note ? `<br>${escape(p.decision.note)}` : ''}` : 'This packet has expired. Its configured default action applies.'}</div>${approvalControl(payment)}`}`);
    } catch (error) { showDetail('Packet unavailable', `<div class="notice error">${escape(error.message)}</div>`); }
  }
  async function decidePacket(button) {
    if (!ensureController()) return;
    const id = button.dataset.decide;
    const note = $('#decision-note')?.value.trim() || null;
    const buttons = $$('[data-decide]'); buttons.forEach(b => { b.disabled = true; });
    setNotice('#decision-result', 'Recording your decision…');
    try {
      await api(`/packets/${encodeURIComponent(id)}/decide`, { method: 'POST', controller: true, body: { decision: button.dataset.option, note, decided_by: state.controllerName } });
      await refresh(true);
      await openPacket(id);
    } catch (error) { setNotice('#decision-result', `${error.message} ${error.hint || ''}`, 'error'); buttons.forEach(b => { b.disabled = false; }); }
  }
  async function openEntry(id) {
    showDetail('Double-entry ledger', empty('Loading journal entry…'));
    try {
      const e = await api(`/dashboard/ledger/${encodeURIComponent(id)}`);
      const debits = e.lines.filter(l => l.debit_minor).reduce((sum, l) => sum + l.eur_equiv_minor, 0);
      const credits = e.lines.filter(l => l.credit_minor).reduce((sum, l) => sum + l.eur_equiv_minor, 0);
      showDetail(`${e.id} / ${human(e.cause_type)}`, `${pill(debits === credits ? 'Balanced in EUR' : 'Balance mismatch', debits === credits ? '' : 'danger')}<h2 class="detail-title">${escape(e.description)}</h2><p class="detail-context">Effective ${escape(date(e.effective_date))}. Posted by ${escape(e.posted_by)} at ${escape(date(e.posted_at, true))}.</p><div class="table-panel"><table><thead><tr><th>Account</th><th>Currency</th><th class="number">Debit</th><th class="number">Credit</th></tr></thead><tbody>${e.lines.map(l => `<tr><td>${escape(l.account)}</td><td>${escape(l.currency)}</td><td class="number">${l.debit_minor ? escape(money(l.debit_minor, l.currency)) : '—'}</td><td class="number">${l.credit_minor ? escape(money(l.credit_minor, l.currency)) : '—'}</td></tr>`).join('')}</tbody></table></div><section class="detail-section"><h3>Source evidence</h3>${e.evidence.map(ref => `<button class="evidence-row" data-event="${escape(ref.event_id)}"><span>${escape(human(ref.role))}</span><p class="mono">${escape(ref.event_id)}</p></button>`).join('')}</section><section class="detail-section"><h3>Content hash</h3><p class="mono subtle">${escape(e.content_hash)}</p>${e.reverses ? `<p class="footnote">Reverses ${escape(e.reverses)}</p>` : ''}</section>`);
    } catch (error) { showDetail('Entry unavailable', `<div class="notice error">${escape(error.message)}</div>`); }
  }
  async function openVendor(id) {
    showDetail('Vendor record', empty('Loading account history…'));
    try {
      const [v, history] = await Promise.all([api(`/vendors/${encodeURIComponent(id)}`), api(`/vendors/${encodeURIComponent(id)}/accounts/history`)]);
      showDetail(`${id} / Account history`, `${statusPill(v.vendor.status)}<h2 class="detail-title">${escape(v.vendor.legal_name)}</h2><p class="detail-context">${escape(v.vendor.country)} · ${escape(v.vendor.currency)} · VAT ${escape(v.vendor.vat_id)}<br>${escape(v.vendor.terms)}</p><section class="detail-section"><h3>Bank-account history</h3>${history.accounts.map(a => `<article class="history-item"><h3>${escape(a.iban_masked)} ${statusPill(a.status)}</h3><p class="mono">${escape(a.account_hash_prefix)}</p><p>Requested via ${escape(a.requested_via)} at ${escape(date(a.requested_at, true))}</p><p>Verification: ${escape(a.verified_via || 'Not verified')}${a.verified_at ? ` at ${escape(date(a.verified_at, true))}` : ''}</p><p>Approval: ${escape(a.approved_by || 'Not approved')}${a.approved_at ? ` at ${escape(date(a.approved_at, true))}` : ''}</p><p>Hold until ${escape(date(a.hold_until, true))}</p><p>Boundary provisioning: ${a.on_chip ? 'Provisioned' : 'Not provisioned'}</p></article>`).join('') || empty('No account history')}</section>`);
    } catch (error) { showDetail('Vendor unavailable', `<div class="notice error">${escape(error.message)}</div>`); }
  }
  function openPayment(id) {
    const p = state.payments.find(p => p.pay_id === id);
    if (!p) return;
    showDetail(`${p.pay_id} / Payment evidence`, `${authorizationMarkup(paymentAuthorization(p), { historical: true })}${statusPill(p.status)}<h2 class="detail-title">${escape(money(p.amount_minor, p.currency))} to ${escape(vendorName(p.vendor_id))}</h2><p class="detail-context">Destination ${escape(p.account_masked || 'unavailable')}. The bank and all funds are simulated.</p><div class="detail-grid"><div class="detail-fact"><span>Boundary result</span><strong>${escape(p.chip?.ok ? 'Authorized' : (p.chip?.reasons || []).map(human).join(', ') || 'Awaiting response')}</strong></div><div class="detail-fact"><span>Signer</span><strong>${p.chip?.signer === 'hw' ? 'REAL FPGA' : p.chip?.signer === 'stub' ? 'SOFTWARE SIMULATION' : 'No response yet'}</strong></div><div class="detail-fact"><span>Authorization latency</span><strong>${p.chip?.latency_ms != null ? `${p.chip.latency_ms} ms` : '—'}</strong></div><div class="detail-fact"><span>Simulated bank reference</span><strong class="mono">${escape(p.bank_ref || 'No settlement')}</strong></div></div>${approvalControl(p)}<section class="detail-section"><h3>Evidence chain</h3>${p.events.map(id => `<button class="evidence-row" data-event="${escape(id)}"><p class="mono">${escape(id)}</p></button>`).join('')}</section>`);
  }
  function approvalControl(payment) {
    if (!payment || payment.workflow_status !== 'awaiting_button') return '';
    const hardware = state.chip?.kind === 'hw';
    return `<section class="detail-section"><h3>Complete the reviewed approval</h3><p class="detail-context">${hardware ? 'The human review is recorded. Authorize only this exact pending batch through the trusted controller. All FPGA rules still apply.' : 'The human review is recorded. Simulate the approval nonce and retry approved payments waiting in this software demonstration. This does not press a physical button.'}</p><button class="button primary" data-authorize="${escape(payment.pay_id)}">${hardware ? 'Authorize this exact payment' : 'Simulate approval nonce'}</button><div id="authorize-result" class="notice" role="status" hidden></div></section>`;
  }
  async function authorizePayment(button) {
    if (!ensureController()) return;
    button.disabled = true;
    const payId = button.dataset.authorize;
    if (!state.chip) { setNotice('#authorize-result', 'Boundary status is unavailable. Reconnect the signer and refresh before authorizing.', 'error'); return; }
    const hardware = state.chip?.kind === 'hw';
    setNotice('#authorize-result', 'Submitting the reviewed authorization…');
    try {
      await api(hardware ? `/controller/payments/${encodeURIComponent(payId)}/authorize` : '/stub/button', { method: 'POST', body: {}, controller: true });
      await refresh(true);
      if (state.liveRunId && (state.liveJob?.pay_id === payId || state.liveJob?.result?.pay_id === payId)) { await pollLiveJob(); $('#detail-dialog').close(); } else openPayment(payId);
    } catch (error) { setNotice('#authorize-result', error.message, 'error'); button.disabled = false; }
  }
  function hardwareDetails() {
    const c = state.chip;
    if (!c) { showDetail('Authorization boundary', empty('Boundary unavailable', 'Check the server and hardware connection. No status has been verified.')); return; }
    const isHardware = c.kind === 'hw';
    showDetail('Authorization boundary', `${pill(isHardware ? 'REAL FPGA' : 'SOFTWARE SIMULATION', isHardware ? '' : 'warning')}<h2 class="detail-title">${isHardware ? 'Connected hardware signer.' : 'Software signer simulation.'}</h2><p class="detail-context">${isHardware ? 'Payment decisions are reported by the connected FPGA transport.' : 'The software signer exercises the protocol and policy checks. It does not demonstrate a physical hardware boundary.'} The bank remains simulated in both modes.</p><div class="detail-grid">${[['Approved account slots', c.slots_used], ['Sequence', c.seq], ['FPGA clock · UTC', c.hardware?.hardware_time != null ? date(Number(c.hardware.hardware_time) * 1000, true) : 'Not reported'], ['Dataset clock · UTC', date(state.clock?.sim_now, true)], ['Hardware day counter', c.day], ['Daily spending cap', money(c.daily_cap_minor)], ['Spent in current day', money(c.daily_spent_minor)], ['Vendor daily approval threshold', money(c.button_threshold_minor)], ['Signing key loaded', c.key_loaded ? 'Yes' : 'No'], ['Approval nonce available', c.nonce_valid ? 'Yes' : 'No']].map(([title, value]) => `<div class="detail-fact"><span>${title}</span><strong>${escape(value ?? '—')}</strong></div>`).join('')}</div><p class="footnote">The dataset clock controls invoice arrivals. Hardware caps and waiting periods use the FPGA clock. The approval threshold also applies to cumulative spending for each vendor that day.</p><section class="detail-section"><h3>Human authorization</h3><div class="notice warning">Dashboard decisions are authenticated software approvals. They do not establish independent physical presence. The desk must never present a screen click as a physical button.</div><p class="subtle">Use the evidence packet attached to a pending payment to record a controller decision. The configured approval mechanism and remaining checks still apply.</p></section><section class="detail-section"><h3>Provisioned accounts</h3><div class="table-panel"><table><thead><tr><th>Slot</th><th>Account hash prefix</th><th class="number">Vendor cap · EUR</th><th>Hold through day</th></tr></thead><tbody>${(c.slots || []).map(s => `<tr><td>${s.slot}</td><td class="mono">${escape(s.account_hash_prefix)}</td><td class="number">${escape(money(s.cap_minor))}</td><td>${s.hold_until_day}</td></tr>`).join('')}</tbody></table></div></section><details class="supporting-details detail-section"><summary>Raw hardware status</summary><pre class="json">${escape(JSON.stringify(c, null, 2))}</pre></details>`);
  }
  function demoReceipt(receipt, { fresh = false } = {}) {
    const data = receipt;
    const refs = data.evidence_event_ids || [];
    const payment = state.payments.find(row => row.pay_id === data.pay_id);
    const chipEvent = paymentAuthorization(payment) || (data.evidence_events || []).filter(event => event.type === 'chip_response').slice(-1)[0] || null;
    showDetail(fresh ? 'Live test / Request result' : 'Demo / Recorded result', `${authorizationMarkup(chipEvent, { historical: !fresh, live: fresh })}${statusPill(data.status || 'pending')}<h2 class="detail-title">${escape(data.title || human(data.id))}</h2><p class="detail-context">${escape(data.outcome || data.description || 'Inspect the recorded evidence for the result.')}</p><div class="detail-grid">${data.run_id ? `<div class="detail-fact"><span>Run ID</span><strong class="mono">${escape(data.run_id)}</strong></div><div class="detail-fact"><span>Payment ID</span><strong class="mono">${escape(data.pay_id || 'No payment request yet')}</strong></div>${data.run_started_at ? `<div class="detail-fact"><span>Submitted · real UTC</span><strong>${escape(date(data.run_started_at, true))}</strong></div>` : ''}` : ''}<div class="detail-fact"><span>Amount</span><strong>${escape(money(data.amount_minor, data.currency))}</strong></div><div class="detail-fact"><span>Recorded signer / bank</span><strong>${chipEvent?.payload?.signer === 'hw' ? 'REAL FPGA' : chipEvent?.payload?.signer === 'stub' ? 'SOFTWARE SIMULATION' : 'No signer response'} / simulated</strong></div></div>${data.guard ? `<section class="detail-section"><h3>Guard being exercised</h3><p class="detail-context">${escape(data.guard)}</p></section>` : ''}${data.next_action ? `<div class="notice">${escape(typeof data.next_action === 'string' ? data.next_action : JSON.stringify(data.next_action))}</div>` : ''}${data.packet_id ? `<button class="button primary" data-packet="${escape(data.packet_id)}">Review the evidence packet</button>` : ''}<section class="detail-section"><h3>Evidence chain</h3>${refs.map(id => `<button class="evidence-row" data-event="${escape(id)}"><p class="mono">${escape(id)}</p></button>`).join('') || '<p class="subtle">No evidence events returned yet.</p>'}</section>${data.journal_entry_ids?.length ? `<section class="detail-section"><h3>Journal entries</h3>${data.journal_entry_ids.map(id => `<button class="button" data-entry="${escape(id)}">${escape(id)}</button>`).join(' ')}</section>` : ''}<p class="footnote">Reproducible demonstration fixture. No real bank payment is attempted. The running backend determines this result.</p>`);
  }
  async function runScenario(id) {
    const existing = state.demo?.scenarios?.find(s => s.id === id);
    if (existing?.started) { demoReceipt(existing); return; }
    if (!ensureController()) return;
    try {
      const receipt = await act(() => api(`/demo/scenarios/${encodeURIComponent(id)}`, { method: 'POST', body: {}, controller: true }), result => `Test recorded: ${result.title || id}. Inspect the receipt for its actual outcome.`);
      if (receipt) demoReceipt(receipt);
    } catch { /* act displayed the failure. */ }
  }
  async function loadDemoPayment(receipt) {
    if (!receipt.pay_id) return;
    try {
      const data = await api('/dashboard/payments?limit=1000');
      state.payments = data.payments;
    } catch { /* Missing verified evidence stays gray; never manufacture a verdict. */ }
  }
  async function openLiveResult(runId) {
    try {
      const receipt = await api(`/demo/runs/${encodeURIComponent(runId)}`);
      await loadDemoPayment(receipt);
      demoReceipt(receipt);
    } catch (error) { setNotice('#action-notice', error.message, 'error'); }
  }
  async function pollLiveJob() {
    if (!state.liveRunId || state.livePolling) return;
    state.livePolling = true;
    state.liveLastPollAttempt = Date.now();
    const runId = state.liveRunId;
    try {
      const job = await api(`/demo/jobs/${encodeURIComponent(runId)}`);
      if (runId !== state.liveRunId) return;
      if (job.run_id !== runId) throw new Error('The server returned a different request ID. Keeping the last recorded result.');
      const changed = state.liveJob?.updated_at !== job.updated_at || state.liveJob?.job_status !== job.job_status;
      state.liveJob = job;
      state.liveLastPoll = Date.now();
      state.liveError = '';
      if (job.run_id === runId) {
        try { sessionStorage.removeItem(liveRunStorageKey(job.kind)); sessionStorage.removeItem(liveRequestKey(job.kind)); } catch { /* Read-only updates still work. */ }
      }
      ingest(liveJobEvents(job));
      renderLiveJob();
      if (changed && job.pay_id && !liveJobWorking()) loadDemoPayment({ pay_id: job.pay_id }).then(renderLiveJob);
    } catch (error) {
      if (runId !== state.liveRunId) return;
      state.liveError = error.status === 404 ? 'No receipt for this request yet. Retry the same request below; it will keep its original ID.' : `Live updates unavailable: ${error.message}`;
      if (state.liveJob?.job_status === 'reconnecting') state.liveJob.job_status = 'unknown';
      renderLiveJob();
    } finally { state.livePolling = false; }
  }
  async function startLiveJob(kind, invoice = null) {
    if (state.busy || liveJobWorking() || !ensureController()) return;
    let runId;
    try {
      const retryId = pendingLiveRun(kind);
      runId = retryId || crypto.randomUUID();
      let payload;
      try { payload = JSON.parse(sessionStorage.getItem(liveRequestKey(kind)) || 'null'); } catch { payload = null; }
      if (!payload || payload.run_id !== runId) payload = { run_id: runId, ...(kind === 'invoice' ? { invoice } : {}) };
      sessionStorage.setItem(liveRunStorageKey(kind), runId);
      sessionStorage.setItem(liveRequestKey(kind), JSON.stringify(payload));
      sessionStorage.setItem(liveActiveKey, JSON.stringify({ run_id: runId, kind }));
      state.liveRunId = runId;
      state.liveSubmitting = true;
      state.liveError = '';
      state.liveJob = { run_id: runId, kind, job_status: 'submitting', source_text: payload.invoice?.document_text, evidence_events: [] };
      renderDemo(); renderLiveJob();
      let job;
      if (retryId) {
        try { job = await api(`/demo/jobs/${encodeURIComponent(runId)}`); }
        catch (error) { if (error.status !== 404) throw error; }
      }
      if (!job) job = await api(`/demo/jobs/${encodeURIComponent(kind)}`, { method: 'POST', body: payload, controller: true, idempotencyKey: `demo-job-${kind}-${runId}` });
      if (job.run_id !== runId) throw new Error('The server returned a different request ID. Retry to inspect the original request.');
      sessionStorage.removeItem(liveRunStorageKey(kind));
      sessionStorage.removeItem(liveRequestKey(kind));
      state.liveJob = job;
      state.liveLastPoll = Date.now();
      ingest(liveJobEvents(job));
      setNotice('#action-notice', 'New information received. Follow the live processing below.');
    } catch (error) {
      state.liveError = `${error.message} The original request is kept for retry; it will not submit a second payment.`;
      if (state.liveJob?.job_status === 'submitting') state.liveJob.job_status = 'unknown';
      setNotice('#live-intake-error', state.liveError, 'error');
    } finally {
      state.liveSubmitting = false;
      renderDemo(); renderLiveJob();
      $('#live-workspace').scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
    }
    if (state.liveJob?.job_status !== 'unknown') await pollLiveJob();
  }
  async function runLiveScenario(id) { return startLiveJob(id); }
  async function loadLiveExample() {
    if (state.liveExampleLoading || liveJobWorking()) return;
    state.liveExampleLoading = true; updateLiveControls();
    setNotice('#live-intake-error', '');
    try {
      const example = await api('/judge/invoice/example');
      if (!example.document_text) throw new Error('No example document was returned.');
      $('#live-invoice-text').value = example.document_text;
      $('#live-example-note').textContent = example.notice || `New invoice using ${example.purchase_order_id || 'a recorded purchase order'} and existing receipts. You can edit the text before processing.`;
    } catch (error) { setNotice('#live-intake-error', error.message, 'error'); }
    finally { state.liveExampleLoading = false; updateLiveControls(); }
  }
  async function processLiveInvoice() {
    setNotice('#live-intake-error', '');
    const documentText = $('#live-invoice-text').value.trim();
    if (!documentText && !pendingLiveRun('invoice')) { setNotice('#live-intake-error', 'Load an example invoice or paste invoice text first.', 'error'); return; }
    return startLiveJob('invoice', { document_text: documentText });
  }
  document.addEventListener('click', async event => {
    const button = event.target.closest('button, a[data-view]');
    if (!button || button.disabled) return;
    if (button.dataset.view) { location.hash = button.dataset.view; return; }
    if (button.dataset.event) return openEvent(button.dataset.event);
    if (button.dataset.packet) return openPacket(button.dataset.packet);
    if (button.dataset.entry) return openEntry(button.dataset.entry);
    if (button.dataset.vendor) return openVendor(button.dataset.vendor);
    if (button.dataset.payment) return openPayment(button.dataset.payment);
    if (button.dataset.decide) return decidePacket(button);
    if (button.dataset.authorize) return authorizePayment(button);
    if (button.dataset.scenario) return runScenario(button.dataset.scenario);
    if (button.dataset.liveScenario) return runLiveScenario(button.dataset.liveScenario);
    if (button.dataset.liveResult) return openLiveResult(button.dataset.liveResult);
    if (button.dataset.datasetTab) { state.datasetTab = button.dataset.datasetTab; renderDataset(); refreshDataset(); return; }
    if (button.dataset.datasetRecord) return openDatasetRecord(button.dataset.datasetRecord);
    if (button.dataset.page) { state.pages[button.dataset.page] = (state.pages[button.dataset.page] || 0) + Number(button.dataset.delta); render(); return; }
    if (button.dataset.packetTab) { state.packetTab = button.dataset.packetTab; $$('#packet-tabs button').forEach(b => b.classList.toggle('active', b === button)); renderPackets(); }
    if (button.dataset.paymentTab) { state.paymentTab = button.dataset.paymentTab; $$('#payment-tabs button').forEach(b => b.classList.toggle('active', b === button)); renderPayments(); }
  });
  $('#run-button').addEventListener('click', async () => {
    if (state.busy || state.clock?.running || !ensureController()) return;
    const revision = ++state.clockRevision;
    try { await act(async () => { const result = await api('/clock/resume', { method: 'POST', controller: true }); if (revision === state.clockRevision) applyClock(result); return result; }, 'Desk running. Scheduled arrivals and decisions appear in Activity.', { label: 'Starting the desk' }); } catch { /* shown */ }
  });
  $('#pause-button').addEventListener('click', pauseDesk);
  $('#step-button').addEventListener('click', async () => {
    if (!ensureController()) return;
    try { await act(() => api('/clock/step', { method: 'POST', controller: true }), data => `Advanced to ${date(data.to, true)}. ${data.inserted} scheduled events entered the desk.`); } catch { /* shown */ }
  });
  $('#refresh-button').addEventListener('click', () => { refreshDataset(true); refresh(true); });
  $('#hardware-details').addEventListener('click', hardwareDetails);
  $('#close-dialog').addEventListener('click', () => $('#detail-dialog').close());
  $('#close-operator').addEventListener('click', () => $('#operator-dialog').close());
  $('#load-live-example').addEventListener('click', loadLiveExample);
  $('#process-live-invoice').addEventListener('click', processLiveInvoice);
  $('#live-unlock-controls').addEventListener('click', () => $('#operator-dialog').showModal());
  $('#operator-button').addEventListener('click', () => $('#operator-dialog').showModal());
  $('#operator-mobile-button').addEventListener('click', () => $('#operator-dialog').showModal());
  async function unlockControls(event) {
    event?.preventDefault();
    const password = $('#operator-token').value.trim();
    if (!password) { setNotice('#operator-error', 'Paste the local control password first.', 'error'); return; }
    const previous = state.controllerToken;
    state.controllerToken = password;
    setNotice('#operator-error', 'Checking the control password…');
    try {
      const result = await api('/controller/check', { method: 'POST', controller: true, body: {} });
      if (result.unlocked !== true) throw new Error('The desk did not confirm access.');
      state.controllerName = $('#operator-name').value.trim() || 'controller';
      $('#operator-token').value = '';
      $('#operator-label').textContent = 'Controls unlocked';
      $('#operator-dot').className = 'dot green';
      $('#operator-dialog').close();
      setNotice('#operator-error', '');
      setNotice('#action-notice', 'Controls unlocked for this tab. Every payment still goes through the FPGA rules.');
    } catch (error) {
      state.controllerToken = previous;
      setNotice('#operator-error', `Could not unlock: ${error.message}`, 'error');
    }
  }
  $('#operator-form').addEventListener('submit', unlockControls);
  $('#lock-operator').addEventListener('click', () => {
    state.controllerToken = ''; $('#operator-token').value = '';
    $('#operator-label').textContent = 'Settings'; $('#operator-dot').className = 'dot';
    $('#operator-dialog').close(); setNotice('#action-notice', 'Controls locked.');
  });
  $('#dataset-search').addEventListener('input', () => { state.pages[`dataset-${state.datasetTab}`] = 0; renderDataset(); });
  for (const id of ['payment', 'invoice', 'vendor', 'event']) $(`#${id}-search`).addEventListener('input', () => { state.pages = {}; render(); });
  $('#judge-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!ensureController()) return;
    const form = new FormData(event.currentTarget);
    const body = Object.fromEntries(form.entries());
    body.iban = body.iban.trim() || null;
    body.submitted_by = state.controllerName;
    setNotice('#judge-error', '');
    try {
      const result = await act(() => api('/judge/message', { method: 'POST', body, controller: true }), 'Test message recorded. Open Activity to inspect the decisions.');
      if (result) await openEvent(result.vendor_message_id || result.judge_injection_id);
    } catch (error) { setNotice('#judge-error', error.message, 'error'); }
  });
  $('#invoice-judge-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!ensureController()) return;
    const data = Object.fromEntries(new FormData(event.currentTarget).entries());
    setNotice('#invoice-judge-error', '');
    const minor = (value, optional = false) => {
      if (!value && optional) return undefined;
      if (!/^\d+(?:\.\d{1,2})?$/.test(value)) throw new Error('Use an unsigned amount with at most two decimal places.');
      const [whole, decimals = ''] = value.split('.');
      if (data.currency === 'JPY' && /[1-9]/.test(decimals)) throw new Error('JPY amounts must be whole yen.');
      const result = data.currency === 'JPY' ? Number(whole) : Number(whole) * 100 + Number(decimals.padEnd(2, '0'));
      if (!Number.isSafeInteger(result)) throw new Error('The amount is too large.');
      return result;
    };
    try {
      if (!data.supplier_id && !data.supplier_name.trim()) throw new Error('Select a vendor or enter a custom supplier name.');
      if (Number(data.quantity) > 1 && !data.unit_amount) throw new Error('Enter the unit price when quantity is above one.');
      const body = {
        supplier_id: data.supplier_id || undefined,
        supplier_name: data.supplier_name.trim() || undefined,
        invoice_number: data.invoice_number.trim(),
        po_reference: data.po_reference.trim() || undefined,
        currency: data.currency,
        net_minor: minor(data.net_amount),
        vat_minor: minor(data.vat_amount),
        gross_minor: minor(data.gross_amount, true),
        sku: data.sku.trim() || undefined,
        quantity: Number(data.quantity),
        unit_price_minor: minor(data.unit_amount, true),
        bank_account: data.bank_account.trim() || undefined,
        body: data.body.trim() || undefined,
        submitted_by: state.controllerName
      };
      await startLiveJob('invoice', body);
      if (state.liveJob?.job_status === 'unknown') setNotice('#invoice-judge-error', state.liveError, 'error');
      else { setNotice('#invoice-progress', 'Invoice submitted. Follow the live agent timeline above.'); $('#live-workspace').scrollIntoView?.({ behavior: 'smooth', block: 'start' }); }
    } catch (error) { setNotice('#invoice-judge-error', error.message, 'error'); }
  });
  $('#ledger-more').addEventListener('click', async () => {
    const button = $('#ledger-more'); button.disabled = true;
    try {
      const seq = Math.max(0, ...state.ledger.map(e => e.seq));
      const result = await api(`/dashboard/ledger?since_seq=${seq}&limit=1000`);
      state.ledger.push(...result.entries); state.ledgerHasMore = result.has_more; renderLedger();
    } catch (error) { setNotice('#global-error', error.message, 'error'); }
    finally { button.disabled = false; }
  });
  window.addEventListener('hashchange', navigate);
  window.addEventListener('beforeunload', () => { state.controllerToken = ''; state.socketGeneration++; state.ws?.close(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  navigate().then(connectLive);
  setInterval(() => { if (!document.hidden) { refresh(); if (state.view === 'dataset') refreshDataset(true); } }, 4000);
  setInterval(() => {
    if (!document.hidden && state.liveRunId && (state.view === 'demo' || liveJobWorking()) && Date.now() - state.liveLastPollAttempt >= (liveJobWorking() ? 1000 : 4000)) pollLiveJob();
    renderClock(); renderProgress(); renderAuthorization();
    if (state.chip && state.chipReceivedAt && Date.now() - state.chipReceivedAt > 15000) {
      state.chip = null; state.chipReceivedAt = 0; renderRail();
    }
  }, 1000);
})();
