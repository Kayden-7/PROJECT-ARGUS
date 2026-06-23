// ARGUS frontend — main orchestration. Screen content (login form markup,
// workbench panels, audit table, trust graph) is filled in by later tasks;
// this file currently wires page routing + the state renderer hookup only.

import { getState, setState, setRenderer } from './state.js';
import {
  fetchInbox, runAgent, confirmAgent, fetchQueue, fetchQueueItem, approveQueue, rejectQueue,
  fetchAudit, fetchAuditSummary, fetchTrustHistory, fetchGmailStatus, fetchTrustSnapshot,
  fetchTemplates, saveTemplate, deleteTemplate, matchTemplate, fetchExecutions, tickExecutions,
  verifyAuditChain, fetchAuditReplay, resetDemo, gmailTest,
} from './api.js';

// Mirrors config.py ALL_ACTIONS — kept in sync manually, same approach used
// throughout this build (no shared-schema endpoint exists to derive this from).
const ALL_ACTIONS = [
  'email.compose', 'email.archive', 'email.mark_read', 'email.star', 'email.move',
  'calendar.accept', 'calendar.label', 'calendar.color', 'label.apply',
  'email.send.external', 'email.send.internal', 'email.reply', 'email.forward',
  'email.delete', 'calendar.create', 'calendar.modify', 'calendar.delete',
  'calendar.reschedule', 'calendar.invite', 'calendar.decline',
];

let countdownHandle = null;

const PAGES = ['login', 'workbench', 'audit', 'trust', 'templates', 'executions', 'settings'];

// ── emergency stop (visual/local-only — POST /api/emergency/* does not exist
// on the backend; mirrors the same honesty convention as the old dark-theme
// build, which never pretended these routes were real) ──────────────────────
function isEstopActive() {
  return localStorage.getItem('argus.estop') === '1';
}

function applyEstopUI() {
  const active = isEstopActive();
  const banner = document.getElementById('estop-banner');
  if (banner) banner.hidden = !active;
  const genBtn = document.getElementById('generate-proposal-btn');
  if (genBtn) genBtn.disabled = active;
  document.querySelectorAll('#approve-btn, #reject-btn, #confirm-reject-btn').forEach((b) => { b.disabled = active; });
  const btn = document.getElementById('estop-btn');
  const status = document.getElementById('estop-status');
  if (btn) {
    btn.classList.toggle('is-active', active);
    btn.textContent = active ? 'Disengage emergency stop' : 'Engage emergency stop';
  }
  if (status) {
    status.classList.toggle('alert', active);
    status.innerHTML = active
      ? '<span class="dot dot-red"></span> Emergency stop engaged'
      : '<span class="dot dot-green"></span> System armed';
  }
}

function initEstop() {
  const btn = document.getElementById('estop-btn');
  if (btn) {
    btn.addEventListener('click', () => {
      localStorage.setItem('argus.estop', isEstopActive() ? '0' : '1');
      applyEstopUI();
    });
  }
  applyEstopUI();
}

// ── profile switcher (visual/local-only — POST /api/profile does not exist) ─
function applyProfileUI() {
  const profile = localStorage.getItem('argus.profile') || 'Balanced';
  document.querySelectorAll('.profile-opt').forEach((b) => {
    b.classList.toggle('active', b.getAttribute('data-profile') === profile);
  });
}

function initProfileSwitcher() {
  const switcher = document.getElementById('profile-switcher');
  if (!switcher) return;
  switcher.addEventListener('click', (e) => {
    const btn = e.target.closest('.profile-opt');
    if (!btn) return;
    localStorage.setItem('argus.profile', btn.getAttribute('data-profile'));
    applyProfileUI();
  });
  applyProfileUI();
}

function currentPageFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const page = params.get('page');
  return PAGES.includes(page) ? page : 'workbench';
}

function render() {
  const state = getState();
  const activePage = state.isAuthenticated ? state.currentPage : 'login';
  PAGES.forEach((name) => {
    const el = document.getElementById(`${name}-page`);
    if (el) el.classList.toggle('active', name === activePage);
  });
}

setRenderer(render);

// ── login ────────────────────────────────────────────────────────────────
function initLogin() {
  document.getElementById('login-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;
    const errorEl = document.getElementById('login-error');

    if (username === 'PROJECT_ARGUS' && password === 'ARGUS_DEMO') {
      errorEl.textContent = '';
      sessionStorage.setItem('argus_session', 'authenticated');
      setState({ isAuthenticated: true, currentPage: 'workbench' });
      window.history.replaceState({}, '', '?page=workbench');
      initWorkbench();
    } else {
      errorEl.textContent = 'Invalid credentials.';
    }
  });
}

// ── workbench: nav ──────────────────────────────────────────────────────
const PAGE_LOADERS = {}; // populated below as each page's loader is defined

function initGlobalNav() {
  document.querySelectorAll('.nav-link').forEach((link) => {
    link.addEventListener('click', () => {
      const page = link.getAttribute('data-page');
      setState({ currentPage: page });
      window.history.replaceState({}, '', `?page=${page}`);
      if (PAGE_LOADERS[page]) PAGE_LOADERS[page]();
    });
  });
}

function showBanner(message, { retryFn } = {}) {
  const slot = document.getElementById('workbench-banner-slot');
  slot.innerHTML = '';
  const banner = document.createElement('div');
  banner.className = 'banner banner-error';
  const msg = document.createElement('p');
  msg.className = 'banner-message';
  msg.textContent = message;
  banner.appendChild(msg);
  if (retryFn) {
    const btn = document.createElement('button');
    btn.className = 'banner-action';
    btn.textContent = 'Retry';
    btn.addEventListener('click', () => { slot.innerHTML = ''; retryFn(); });
    banner.appendChild(btn);
  }
  slot.appendChild(banner);
}

function clearBanner() {
  document.getElementById('workbench-banner-slot').innerHTML = '';
}

// ── workbench: inbox ─────────────────────────────────────────────────────
function renderInboxItems(messages) {
  const container = document.getElementById('inbox-items');
  container.innerHTML = '';
  document.getElementById('inbox-count').textContent = messages.length;
  messages.forEach((msg) => {
    const item = document.createElement('div');
    item.className = 'inbox-item';
    item.setAttribute('data-email-id', msg.id);

    const head = document.createElement('div');
    head.className = 'inbox-item-head';
    const sender = document.createElement('span');
    sender.className = 'inbox-item-sender';
    sender.textContent = (msg.sender || '').split('<')[0].trim() || msg.sender;
    const time = document.createElement('span');
    time.className = 'inbox-item-time';
    time.textContent = msg.receivedAt ? new Date(msg.receivedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
    head.appendChild(sender);
    head.appendChild(time);

    const subject = document.createElement('div');
    subject.className = 'inbox-item-subject';
    subject.textContent = msg.subject || '(no subject)';

    const snippet = document.createElement('div');
    snippet.className = 'inbox-item-snippet';
    snippet.textContent = msg.snippet || '';

    item.appendChild(head);
    item.appendChild(subject);
    item.appendChild(snippet);

    item.addEventListener('click', () => selectEmail(msg));
    container.appendChild(item);
  });
}

function selectEmail(msg) {
  setState({ selectedEmailId: msg.id });
  document.querySelectorAll('.inbox-item').forEach((el) => {
    el.classList.toggle('selected', el.getAttribute('data-email-id') === msg.id);
  });
  document.getElementById('command-email-context').textContent = `on: ${msg.subject || '(no subject)'}`;
}

async function loadInbox() {
  document.getElementById('inbox-loader').hidden = false;
  const result = await fetchInbox(20);
  document.getElementById('inbox-loader').hidden = true;

  if (!result.ok || !result.body.success) {
    const code = result.body && result.body.error_code;
    if (code === 'GMAIL_NOT_CONNECTED') {
      showBanner("Gmail isn't connected yet. Connect Gmail to load your inbox.", { retryFn: loadInbox });
    } else if (code === 'TIMEOUT' || code === 'NETWORK_ERROR') {
      showBanner("Gmail connection failed. We couldn't reach Google's servers.", { retryFn: loadInbox });
    } else {
      showBanner('Failed to load inbox.', { retryFn: loadInbox });
    }
    setState({ inboxEmails: [], inboxError: code || 'UNKNOWN' });
    return;
  }
  clearBanner();
  setState({ inboxEmails: result.body.messages, inboxError: null });
  renderInboxItems(result.body.messages);
}

// ── workbench: command composer + agent run/confirm ─────────────────────
function setProposalStatus(text) {
  document.getElementById('proposal-status').textContent = text;
}

function renderProposal(proposal) {
  const block = document.getElementById('proposal-block');
  block.hidden = false;
  document.getElementById('proposal-action').textContent = proposal.action_type;
  const entities = proposal.entities || {};

  const toRow = document.getElementById('proposal-to-row');
  if (entities.recipient) {
    toRow.hidden = false;
    document.getElementById('proposal-to').textContent = entities.recipient;
  } else {
    toRow.hidden = true;
  }

  const subjectRow = document.getElementById('proposal-subject-row');
  if (entities.subject) {
    subjectRow.hidden = false;
    document.getElementById('proposal-subject').textContent = entities.subject;
  } else {
    subjectRow.hidden = true;
  }
}

function renderDecision(decision, decisionDict, queue, trust) {
  const card = document.getElementById('decision-card');
  card.hidden = false;
  const badge = document.getElementById('decision-outcome');
  badge.className = `decision-badge ${decision.toLowerCase()}`;
  badge.textContent = decision;
  const dd = decisionDict || {};
  document.getElementById('decision-reason').textContent = dd.narrative || '';

  const deltaEl = document.getElementById('decision-delta');
  if (trust && trust.trust_before != null && trust.trust_after != null) {
    const sign = trust.actual_delta >= 0 ? '+' : '';
    deltaEl.textContent = `${trust.trust_before.toFixed(1)} → ${trust.trust_after.toFixed(1)} (${sign}${trust.actual_delta.toFixed(1)})`;
  } else if (decision === 'GATED' && dd.trust_delta_preview != null) {
    deltaEl.textContent = `${dd.trust_delta_preview} (preview, pending approval)`;
  } else {
    deltaEl.textContent = 'No trust impact';
  }

  document.getElementById('containment-msg').hidden = decision !== 'BLOCK';
  document.getElementById('decision-trace').textContent = JSON.stringify(dd.trace || [], null, 2);

  if (decision === 'GATED' && queue) {
    renderAuthorisation(queue);
  } else {
    document.getElementById('authorisation-slot').innerHTML = '';
    if (countdownHandle) { clearInterval(countdownHandle); countdownHandle = null; }
  }
  loadQueue();

  const proposal = getState().proposal;
  if (proposal && proposal.action_type) refreshGauge(proposal.action_type);
}

// ── authorisation: approve / reject + countdown ──────────────────────────
function formatCountdown(seconds) {
  if (seconds <= 0) return '00:00';
  const m = Math.floor(seconds / 60), s = seconds % 60;
  return `${m < 10 ? '0' : ''}${m}:${s < 10 ? '0' : ''}${s}`;
}

function renderAuthorisation(queueItem) {
  const slot = document.getElementById('authorisation-slot');
  slot.innerHTML = `
    <div id="authorisation" class="card">
      <div class="auth-header">
        <h4>Authorisation</h4>
        <span id="approval-countdown" class="auth-countdown"></span>
      </div>
      <button id="approve-btn" class="btn-primary">Approve &amp; Execute</button>
      <button id="reject-btn" class="btn-secondary">Reject</button>
      <div id="reject-reason-form" hidden>
        <textarea id="reject-reason" placeholder="Reason for rejection (required)" maxlength="500"></textarea>
        <p class="banner-message" id="reject-reason-error" hidden style="color: var(--color-oxblood);">A reason is required to reject.</p>
        <button id="confirm-reject-btn" class="btn-secondary">Confirm Rejection</button>
      </div>
    </div>`;

  if (countdownHandle) clearInterval(countdownHandle);
  const tick = () => {
    const remaining = queueItem.expires_at - Math.floor(Date.now() / 1000);
    const el = document.getElementById('approval-countdown');
    if (!el) { clearInterval(countdownHandle); return; }
    el.textContent = `Expires in ${formatCountdown(remaining)}`;
    el.classList.toggle('is-urgent', remaining <= 30);
    if (remaining <= 0) {
      clearInterval(countdownHandle);
      document.getElementById('approve-btn').disabled = true;
      document.getElementById('reject-btn').disabled = true;
      el.textContent = 'Approval expired';
    }
  };
  tick();
  countdownHandle = setInterval(tick, 1000);

  document.getElementById('approve-btn').addEventListener('click', async () => {
    const btn = document.getElementById('approve-btn');
    btn.disabled = true;
    const result = await approveQueue(queueItem.id);
    if (result.ok && result.body.success) {
      setProposalStatus('Approved. Checking execution outcome…');
      pollQueueItem(queueItem.id);
    } else {
      setProposalStatus(result.body.detail || 'Approval failed.');
      btn.disabled = false;
    }
  });

  document.getElementById('reject-btn').addEventListener('click', () => {
    document.getElementById('reject-reason-form').hidden = false;
  });

  document.getElementById('confirm-reject-btn').addEventListener('click', async () => {
    const reason = document.getElementById('reject-reason').value.trim();
    const errorEl = document.getElementById('reject-reason-error');
    if (!reason) { errorEl.hidden = false; return; }
    errorEl.hidden = true;
    const result = await rejectQueue(queueItem.id, reason);
    if (result.ok && result.body.success) {
      clearInterval(countdownHandle);
      document.getElementById('authorisation-slot').innerHTML = '';
      setProposalStatus('Rejected.');
      loadQueue();
    } else {
      setProposalStatus(result.body.detail || 'Rejection failed.');
    }
  });

  applyEstopUI();
}

// Per FRONTEND_HANDOFF.md: after approve, poll GET /api/queue/<id> until status
// changes away from APPROVED (max 10 polls, 1s interval), since execution is
// reconciled asynchronously (argus/executor.py), not synchronously on approve.
async function pollQueueItem(queueId, attempt = 0) {
  const result = await fetchQueueItem(queueId);
  if (!result.ok) { setProposalStatus('Could not check execution status.'); loadQueue(); return; }
  if (result.body.status !== 'APPROVED' || attempt >= 10) {
    setProposalStatus(`Status: ${result.body.status}`);
    loadQueue();
    return;
  }
  setTimeout(() => pollQueueItem(queueId, attempt + 1), 1000);
}

// ── queue panel ───────────────────────────────────────────────────────────
function statusBadgeClass(status) {
  if (status === 'PENDING' || status === 'MANUAL_REVIEW') return 'gated';
  if (status === 'APPROVED' || status === 'EXECUTED') return 'allow';
  if (status === 'REJECTED' || status === 'EXPIRED' || status === 'CANCELLED') return 'block';
  return 'config';
}

function renderQueueItems(items) {
  const container = document.getElementById('queue-items');
  container.innerHTML = '';
  if (!items.length) {
    container.innerHTML = '<p class="proposal-hint">No queue activity yet.</p>';
    return;
  }
  items.forEach((item) => {
    let actionType = '(unknown)';
    try { actionType = JSON.parse(item.proposal_json).action_type; } catch (e) {}
    const row = document.createElement('div');
    row.className = 'queue-item';

    const badge = document.createElement('span');
    badge.className = `decision-badge ${statusBadgeClass(item.status)}`;
    badge.textContent = item.status;

    const action = document.createElement('span');
    action.className = 'queue-item-action';
    action.textContent = actionType;

    row.appendChild(badge);
    row.appendChild(action);

    if (item.status === 'PENDING') {
      const expiry = document.createElement('span');
      expiry.className = 'queue-item-expiry';
      const remaining = item.expires_at - Math.floor(Date.now() / 1000);
      expiry.textContent = `Expires in ${formatCountdown(Math.max(0, remaining))}`;
      row.appendChild(expiry);
      row.addEventListener('click', () => renderAuthorisation(item));
    }

    container.appendChild(row);
  });
}

async function loadQueue() {
  const result = await fetchQueue();
  if (!result.ok) return;
  setState({ queueItems: result.body });
  renderQueueItems(result.body);
}

async function handleGenerateProposal() {
  if (isEstopActive()) {
    setProposalStatus('Emergency stop is engaged. Disengage it from Settings to continue.');
    return;
  }
  const command = document.getElementById('command-input').value.trim();
  if (!command) {
    setProposalStatus('Type a command first.');
    return;
  }
  const state = getState();
  const btn = document.getElementById('generate-proposal-btn');
  btn.disabled = true;
  setProposalStatus('Interpreting command…');
  document.getElementById('proposal-block').hidden = true;
  document.getElementById('decision-card').hidden = true;

  const runResult = await runAgent(command, state.selectedEmailId);
  if (!runResult.ok && runResult.status !== 200) {
    setProposalStatus('Agent is unavailable right now. Try again.');
    btn.disabled = false;
    return;
  }

  const agentBody = runResult.body;
  if (agentBody.agent_status === 'AGENT_NEEDS_CLARIFICATION') {
    setProposalStatus((agentBody.uncertainties || []).join(' '));
    btn.disabled = false;
    return;
  }
  if (agentBody.agent_status !== 'PROPOSAL') {
    setProposalStatus('Could not interpret that command. Try rephrasing.');
    btn.disabled = false;
    return;
  }

  setState({ proposal: agentBody.proposal, agentProposalId: agentBody.agent_proposal_id });
  renderProposal(agentBody.proposal);
  setProposalStatus('Checking policy decision…');

  const confirmResult = await confirmAgent(agentBody.agent_proposal_id);
  btn.disabled = false;
  if (!confirmResult.ok) {
    setProposalStatus(confirmResult.body.detail || 'Could not get a policy decision.');
    return;
  }
  setProposalStatus('');
  renderDecision(confirmResult.body.decision, confirmResult.body.decision_dict, confirmResult.body.queue, confirmResult.body.trust);
}

function initComposer() {
  document.getElementById('generate-proposal-btn').addEventListener('click', handleGenerateProposal);
}

// ── trust gauge widget (workbench "trust moment") ────────────────────────
// Semicircle arc gauge ported from the old dark-theme build, restyled with
// the current design tokens' hex values (evergreen/amber/oxblood) in place
// of the old build's RED/AMBER/GREEN.
const GAUGE_CENTER = { x: 110, y: 120 };
const GAUGE_RADIUS = 90;
const GAUGE_ARC_LENGTH = Math.PI * GAUGE_RADIUS;

function hexToRgb(hex) {
  const n = parseInt(hex.replace('#', ''), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function interpolateColor(hex1, hex2, t) {
  const a = hexToRgb(hex1), b = hexToRgb(hex2);
  const r = Math.round(a[0] + (b[0] - a[0]) * t);
  const g = Math.round(a[1] + (b[1] - a[1]) * t);
  const bl = Math.round(a[2] + (b[2] - a[2]) * t);
  return `rgb(${r},${g},${bl})`;
}

function trustColor(value) {
  const OXBLOOD = '#7D2D32', AMBER = '#98520A', EVERGREEN = '#0E6254';
  if (value <= 50) return interpolateColor(OXBLOOD, AMBER, value / 50);
  return interpolateColor(AMBER, EVERGREEN, (value - 50) / 50);
}

function setGaugeValue(value) {
  const clamped = Math.max(0, Math.min(100, value));
  const fill = document.getElementById('gauge-fill');
  fill.style.strokeDasharray = GAUGE_ARC_LENGTH;
  fill.style.strokeDashoffset = GAUGE_ARC_LENGTH * (1 - clamped / 100);
  fill.style.stroke = trustColor(clamped);
}

function setCeilingMarker(ceiling) {
  const theta = ((180 - (Math.max(0, Math.min(100, ceiling)) / 100) * 180) * Math.PI) / 180;
  const innerR = GAUGE_RADIUS - 17, outerR = GAUGE_RADIUS + 17;
  const line = document.getElementById('gauge-ceiling');
  line.setAttribute('x1', GAUGE_CENTER.x + innerR * Math.cos(theta));
  line.setAttribute('y1', GAUGE_CENTER.y - innerR * Math.sin(theta));
  line.setAttribute('x2', GAUGE_CENTER.x + outerR * Math.cos(theta));
  line.setAttribute('y2', GAUGE_CENTER.y - outerR * Math.sin(theta));
}

async function refreshGauge(actionType) {
  const result = await fetchTrustSnapshot(actionType);
  const widget = document.getElementById('trust-widget');
  if (!result.ok || !result.body.success) { widget.hidden = true; return; }
  const data = result.body;
  widget.hidden = false;
  setGaugeValue(data.trust);
  setCeilingMarker(data.ceiling);
  document.getElementById('gauge-value').textContent = Math.round(data.trust);
  document.getElementById('gauge-label').textContent = data.label;
  document.getElementById('trust-widget-action').textContent = data.action_type;
  document.getElementById('meta-ceiling').textContent = `${data.ceiling} (${data.profile})`;
  document.getElementById('meta-modifier').textContent = `${data.overall_modifier.toFixed(2)}×`;

  const recoveryRow = document.getElementById('meta-recovery');
  if (data.damping_active) {
    recoveryRow.hidden = false;
    document.getElementById('recovery-detail').textContent = `${data.damping_remaining} events remaining in recovery window`;
  } else {
    recoveryRow.hidden = true;
  }
}

let workbenchInitialized = false;
function initWorkbench() {
  // Guard against double-binding: initWorkbench() is called both from a fresh
  // login success AND on page load when already authenticated. Without this,
  // either path re-running would stack duplicate listeners on the same
  // buttons/links (the exact bug class found earlier in the old dark-theme
  // build, where a duplicate listener caused double POST /api/propose calls).
  if (workbenchInitialized) return;
  workbenchInitialized = true;
  initComposer();
  applyEstopUI(); // estop-btn listener itself is attached once, from Settings (initSettingsPage)
  loadInbox();
  loadQueue();
  refreshGauge('email.reply');
}

// ── audit trail ──────────────────────────────────────────────────────────
// Audit entries have no explicit "actor" field — derived here from event_type
// + outcome, a best-effort label for display only, not a claim the backend
// provides it. ACTION column uses the entry's own `reason` text (real field,
// already human-readable per the verified API), not a fabricated sentence.
function deriveActor(entry) {
  if (entry.event_type === 'QUEUE_TRANSITIONED' && (entry.outcome === 'APPROVED' || entry.outcome === 'REJECTED')) return 'You';
  if (entry.event_type === 'DECISION_EVALUATED') return 'Policy engine';
  if (entry.event_type && entry.event_type.startsWith('EXECUTION_')) return 'Agent';
  if (entry.event_type === 'INBOX_READ' || entry.event_type === 'AGENT_PROPOSAL') return 'Agent';
  if (entry.event_type === 'POLICY_UPDATED' || entry.event_type === 'CONFIG_CHANGED') return 'Admin';
  return 'System';
}

function outcomeBadgeClass(outcome) {
  if (outcome === 'ALLOW' || outcome === 'APPROVED') return 'allow';
  if (outcome === 'GATED' || outcome === 'PENDING') return 'gated';
  if (outcome === 'BLOCK' || outcome === 'REJECTED') return 'block';
  return 'config';
}

function renderAuditRows(entries) {
  const tbody = document.getElementById('audit-tbody');
  tbody.innerHTML = '';
  if (!entries.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="proposal-hint">No audit entries yet.</td></tr>';
    return;
  }
  entries.forEach((entry) => {
    const row = document.createElement('tr');

    const ts = document.createElement('td');
    ts.className = 'timestamp-cell';
    ts.textContent = new Date(entry.timestamp * 1000).toISOString().slice(0, 19).replace('T', ' ');

    const action = document.createElement('td');
    action.textContent = entry.reason || entry.event_type;

    const outcomeCell = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = `decision-badge ${outcomeBadgeClass(entry.outcome)}`;
    badge.textContent = entry.outcome || entry.event_type;
    outcomeCell.appendChild(badge);

    const actor = document.createElement('td');
    actor.textContent = deriveActor(entry);

    row.appendChild(ts);
    row.appendChild(action);
    row.appendChild(outcomeCell);
    row.appendChild(actor);
    if (entry.correlation_id) {
      row.addEventListener('click', () => loadAuditReplay(entry.correlation_id));
    }
    tbody.appendChild(row);
  });
}

// ── audit: chain verification + replay (real backend, previously unwired) ──
async function handleVerifyChain() {
  const result = await verifyAuditChain();
  const el = document.getElementById('audit-verify-result');
  if (!result.ok) { el.textContent = 'Verification request failed.'; return; }
  const { valid, checked, note } = result.body;
  el.textContent = `${valid ? 'Valid' : 'BROKEN'} — ${checked} entries checked. ${note}`;
  el.style.color = valid ? '' : 'var(--color-oxblood)';
}

async function loadAuditReplay(correlationId) {
  const panel = document.getElementById('audit-replay-panel');
  const result = await fetchAuditReplay(correlationId);
  if (!result.ok) return;
  panel.hidden = false;
  document.getElementById('audit-replay-label').textContent =
    `${result.body.label} (correlation: ${correlationId}, ${result.body.events.length} event(s))`;
  document.getElementById('audit-replay-events').textContent = JSON.stringify(result.body.events, null, 2);
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderAuditSummary(summary) {
  const d = summary.decisions || {};
  const rate = summary.human_oversight ? Math.round(summary.human_oversight.approval_rate * 100) : null;
  document.getElementById('audit-summary').textContent =
    `${d.total || 0} total · ${d.ALLOW || 0} ALLOW · ${d.GATED || 0} GATED · ${d.BLOCK || 0} BLOCK` +
    (rate !== null ? ` · Approval rate: ${rate}%` : '');
}

let auditPageInitialized = false;
function initAuditPage() {
  if (auditPageInitialized) return;
  auditPageInitialized = true;
  document.getElementById('audit-verify-btn').addEventListener('click', handleVerifyChain);
}

async function loadAudit() {
  initAuditPage();
  const [auditResult, summaryResult] = await Promise.all([fetchAudit(100), fetchAuditSummary()]);
  if (auditResult.ok) renderAuditRows(auditResult.body);
  if (summaryResult.ok) renderAuditSummary(summaryResult.body);
}

PAGE_LOADERS.audit = loadAudit;
PAGE_LOADERS.workbench = () => {};

// ── trust history ─────────────────────────────────────────────────────────
let trustSelectPopulated = false;
function populateTrustSelect() {
  if (trustSelectPopulated) return;
  trustSelectPopulated = true;
  const select = document.getElementById('trust-action-type');
  ALL_ACTIONS.forEach((action) => select.appendChild(new Option(action, action)));
  select.value = 'email.reply';
  select.addEventListener('change', () => loadTrust(select.value));
}

function drawTrustGraph(points) {
  const svg = document.getElementById('trust-graph');
  svg.innerHTML = '';
  document.getElementById('trust-tooltip').textContent = '';
  if (!points.length) {
    svg.innerHTML = '<text x="400" y="140" text-anchor="middle" fill="#667177" font-size="14">No history yet for this action type.</text>';
    return;
  }

  const W = 800, H = 280, pad = 40;
  const minT = points[0].timestamp, maxT = points[points.length - 1].timestamp;
  const spanT = Math.max(1, maxT - minT);
  const x = (t) => pad + ((t - minT) / spanT) * (W - pad * 2);
  const y = (v) => H - pad - (v / 100) * (H - pad * 2);

  // axis
  const axis = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  axis.setAttribute('x1', pad); axis.setAttribute('y1', H - pad);
  axis.setAttribute('x2', W - pad); axis.setAttribute('y2', H - pad);
  axis.setAttribute('stroke', '#D9DEDA');
  svg.appendChild(axis);

  const pathD = points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(p.timestamp)} ${y(p.resulting_trust)}`).join(' ');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', pathD);
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', '#0E6254');
  path.setAttribute('stroke-width', '2');
  svg.appendChild(path);

  points.forEach((p) => {
    const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    dot.setAttribute('cx', x(p.timestamp));
    dot.setAttribute('cy', y(p.resulting_trust));
    dot.setAttribute('r', 4);
    dot.setAttribute('fill', '#0E6254');
    dot.style.cursor = 'pointer';
    dot.addEventListener('mouseenter', () => {
      document.getElementById('trust-tooltip').textContent =
        `${new Date(p.timestamp * 1000).toLocaleString()} — ${p.reason} (trust → ${p.resulting_trust})`;
    });
    svg.appendChild(dot);
  });
}

async function loadTrust(actionType) {
  populateTrustSelect();
  const type = actionType || document.getElementById('trust-action-type').value;
  const result = await fetchTrustHistory(type);
  if (!result.ok) { document.getElementById('trust-tooltip').textContent = 'Failed to load trust history.'; return; }
  drawTrustGraph(result.body.points || []);
}

PAGE_LOADERS.trust = () => loadTrust();

// ── templates ("communication boundaries") ───────────────────────────────
// Real backend, ported from the old dark-theme build: GET/POST/DELETE
// /api/templates, GET /api/templates/match. Structured allowlisted fields
// only — no free-form instruction field (argus/templates.py: that's an
// injection surface). avoid_phrases is validator-only, never sent to GPT-4o.
let templatesPagePopulated = false;
function populateTemplateActionSelects() {
  if (templatesPagePopulated) return;
  templatesPagePopulated = true;
  [document.getElementById('tpl-action-type'), document.getElementById('tpl-match-action')].forEach((select) => {
    ALL_ACTIONS.forEach((a) => select.appendChild(new Option(a, a)));
  });
}

function scopeLabel(tpl) {
  if (tpl.contact && tpl.action_type) return 'Contact + Action';
  if (tpl.contact) return 'Contact-wide';
  if (tpl.action_type) return 'Action-wide';
  return 'Global';
}

function renderTemplates(list) {
  const container = document.getElementById('template-list');
  container.innerHTML = '';
  if (!list.length) {
    container.innerHTML = '<p class="empty-state">No boundaries defined yet.</p>';
    return;
  }
  list.forEach((tpl) => {
    const card = document.createElement('div');
    card.className = 'template-card';

    const head = document.createElement('div');
    head.className = 'template-card-head';
    const contact = document.createElement('span');
    contact.className = 'template-contact';
    contact.textContent = (tpl.contact || '(any contact)') + (tpl.action_type ? ` · ${tpl.action_type}` : '');
    const scope = document.createElement('span');
    scope.className = 'template-scope-badge';
    scope.textContent = scopeLabel(tpl);
    const badge = document.createElement('span');
    badge.className = 'template-tone-badge';
    badge.textContent = `${tpl.tone} / ${tpl.formality}`;
    const removeBtn = document.createElement('button');
    removeBtn.className = 'template-remove-btn';
    removeBtn.textContent = 'Remove';
    removeBtn.addEventListener('click', async () => { await deleteTemplate(tpl.id); refreshTemplates(); });
    head.appendChild(contact);
    head.appendChild(scope);
    head.appendChild(badge);
    head.appendChild(removeBtn);
    card.appendChild(head);

    const rules = document.createElement('div');
    rules.className = 'template-rules';
    const parts = [
      `${tpl.length_class} length`,
      `max ${tpl.max_words} words / ${tpl.max_sentences} sentences / ${tpl.max_paragraphs} paragraphs`,
      `greeting: ${tpl.greeting_style}`,
      `sign-off: ${tpl.signoff_style}`,
    ];
    if (tpl.avoid_phrases && tpl.avoid_phrases.length) parts.push(`avoid: ${tpl.avoid_phrases.join(', ')}`);
    rules.textContent = parts.join(' · ');
    card.appendChild(rules);

    container.appendChild(card);
  });
}

async function refreshTemplates() {
  const result = await fetchTemplates();
  if (result.ok) renderTemplates(result.body);
}

let templatesInitialized = false;
function initTemplatesPage() {
  populateTemplateActionSelects();
  if (templatesInitialized) return;
  templatesInitialized = true;

  document.getElementById('tpl-add-btn').addEventListener('click', async () => {
    const errorEl = document.getElementById('tpl-form-error');
    errorEl.hidden = true;

    const avoidRaw = document.getElementById('tpl-avoid').value.trim();
    const avoid_phrases = avoidRaw ? avoidRaw.split(',').map((s) => s.trim()).filter(Boolean) : [];

    const payload = {
      contact: document.getElementById('tpl-contact').value.trim() || null,
      action_type: document.getElementById('tpl-action-type').value || null,
      settings: {
        tone: document.getElementById('tpl-tone').value,
        formality: document.getElementById('tpl-formality').value,
        length_class: document.getElementById('tpl-length').value,
        greeting_style: document.getElementById('tpl-greeting').value,
        signoff_style: document.getElementById('tpl-signoff').value,
        max_words: parseInt(document.getElementById('tpl-max-words').value, 10),
        max_sentences: parseInt(document.getElementById('tpl-max-sentences').value, 10),
        max_paragraphs: parseInt(document.getElementById('tpl-max-paragraphs').value, 10),
        avoid_phrases,
      },
    };

    const result = await saveTemplate(payload);
    if (!result.ok || !result.body.success) {
      errorEl.hidden = false;
      errorEl.textContent = (result.body.errors || [result.body.detail || 'Save failed']).join(', ');
      return;
    }
    document.getElementById('tpl-contact').value = '';
    document.getElementById('tpl-avoid').value = '';
    refreshTemplates();
  });

  document.getElementById('tpl-match-btn').addEventListener('click', async () => {
    const contact = document.getElementById('tpl-match-contact').value.trim();
    const actionType = document.getElementById('tpl-match-action').value;
    const result = await matchTemplate(contact, actionType);
    const el = document.getElementById('tpl-match-result');
    el.className = 'tpl-match-result';
    if (!result.ok) { el.textContent = 'Request failed.'; return; }
    const snap = result.body;
    if (snap.status === 'DEFAULT') {
      el.classList.add('is-default');
      el.textContent = `No saved boundary matched. Conservative default applied (${snap.settings.tone}, ${snap.settings.length_class}).`;
    } else if (snap.status === 'MANUAL_REVIEW') {
      el.classList.add('is-review');
      el.textContent = `Ambiguous match at scope "${snap.scope}" — would route to manual review, not auto-apply.`;
    } else {
      el.textContent = `Matched at scope "${snap.scope}": ${snap.settings.tone}, ${snap.settings.formality}, ${snap.settings.length_class} length.`;
    }
  });
}

PAGE_LOADERS.templates = () => { initTemplatesPage(); refreshTemplates(); };

// ── execution status ──────────────────────────────────────────────────────
// GET /api/executions lists pending_executions rows. Pipeline:
//   DRAFT_PENDING -> DRAFT_READY -> SENDING -> COMPLETED, or MANUAL_REVIEW
// (a fail-closed sink on any uncertainty — never auto-resumed by design).
const PIPELINE_STEPS = ['DRAFT_PENDING', 'DRAFT_READY', 'SENDING', 'COMPLETED'];
const PIPELINE_LABELS = { DRAFT_PENDING: 'Draft', DRAFT_READY: 'Ready', SENDING: 'Sending', COMPLETED: 'Sent' };

function renderPipeline(status) {
  const wrap = document.createElement('div');
  wrap.className = 'execution-pipeline';
  if (status === 'MANUAL_REVIEW' || status === 'FAILED') {
    const step = document.createElement('span');
    step.className = 'pipeline-step';
    step.textContent = status === 'MANUAL_REVIEW' ? 'Paused for review' : 'Failed';
    wrap.appendChild(step);
    return wrap;
  }
  const currentIdx = PIPELINE_STEPS.indexOf(status);
  PIPELINE_STEPS.forEach((s, i) => {
    if (i > 0) {
      const arrow = document.createElement('span');
      arrow.className = 'pipeline-arrow';
      arrow.textContent = '→';
      wrap.appendChild(arrow);
    }
    const step = document.createElement('span');
    step.className = `pipeline-step${i === currentIdx ? ' is-current' : i < currentIdx ? ' is-done' : ''}`;
    step.textContent = PIPELINE_LABELS[s];
    wrap.appendChild(step);
  });
  return wrap;
}

function renderExecutions(list) {
  const container = document.getElementById('execution-list');
  container.innerHTML = '';
  if (!list.length) {
    container.innerHTML = '<p class="empty-state">No executions yet.</p>';
    return;
  }
  list.forEach((ex) => {
    const card = document.createElement('div');
    card.className = `execution-card${ex.status === 'MANUAL_REVIEW' ? ' is-review' : ''}`;

    const head = document.createElement('div');
    head.className = 'execution-head';
    const action = document.createElement('span');
    action.className = 'execution-action';
    action.textContent = ex.action_type;
    head.appendChild(action);

    const badge = document.createElement('span');
    badge.className = `execution-status-badge status-${ex.status.toLowerCase()}`;
    badge.textContent = ex.status.replace('_', ' ');
    head.appendChild(badge);
    head.appendChild(renderPipeline(ex.status));
    card.appendChild(head);

    if (ex.status === 'MANUAL_REVIEW') {
      const reassure = document.createElement('p');
      reassure.className = 'execution-reassurance';
      reassure.textContent = `${ex.status_reason || 'Paused on uncertainty.'} ARGUS stops and asks rather than risk a silent double-send or a lost email — this execution is waiting on a human decision, nothing has been lost.`;
      card.appendChild(reassure);
    }

    const meta = document.createElement('div');
    meta.className = 'execution-meta';
    meta.textContent = `attempt ${ex.attempt_count}` + (ex.message_id ? ` · message ${ex.message_id}` : ex.draft_id ? ` · draft ${ex.draft_id}` : '');
    card.appendChild(meta);

    container.appendChild(card);
  });
}

async function refreshExecutions() {
  const result = await fetchExecutions();
  if (result.ok) renderExecutions(result.body);
}

let executionsInitialized = false;
function initExecutionsPage() {
  if (executionsInitialized) return;
  executionsInitialized = true;
  document.getElementById('executions-tick-btn').addEventListener('click', async () => {
    await tickExecutions();
    refreshExecutions();
  });
}

PAGE_LOADERS.executions = () => { initExecutionsPage(); refreshExecutions(); };

// ── private contacts (local-only — no backend route exists for this yet) ──
// ARGUS should never process or decide on emails involving these people.
// Real enforcement (blocking before the AI sees email content) requires
// Phase 8 — this panel only maintains the list client-side for now.
const CONTACTS_KEY = 'argus.privateContacts';

function loadContacts() {
  try { return JSON.parse(localStorage.getItem(CONTACTS_KEY)) || []; }
  catch (e) { return []; }
}

function saveContacts(list) {
  localStorage.setItem(CONTACTS_KEY, JSON.stringify(list));
}

function renderContacts() {
  const list = loadContacts();
  const container = document.getElementById('contact-list');
  container.innerHTML = '';
  if (!list.length) {
    container.innerHTML = '<p class="empty-state">No private contacts added.</p>';
    return;
  }
  list.forEach((name) => {
    const item = document.createElement('div');
    item.className = 'contact-item';
    const label = document.createElement('span');
    label.className = 'contact-name';
    label.textContent = name;
    const removeBtn = document.createElement('button');
    removeBtn.className = 'contact-remove-btn';
    removeBtn.textContent = 'Remove';
    removeBtn.addEventListener('click', () => {
      saveContacts(loadContacts().filter((c) => c !== name));
      renderContacts();
    });
    item.appendChild(label);
    item.appendChild(removeBtn);
    container.appendChild(item);
  });
}

function initContacts() {
  document.getElementById('contact-add-btn').addEventListener('click', () => {
    const input = document.getElementById('contact-add-input');
    const name = input.value.trim();
    if (!name) { input.focus(); return; }
    const list = loadContacts();
    if (list.indexOf(name) === -1) {
      list.push(name);
      saveContacts(list);
      renderContacts();
    }
    input.value = '';
  });
  renderContacts();
}

// ── settings (profile switcher + emergency stop + integrations + contacts) ─
async function refreshGmailStatus() {
  const result = await fetchGmailStatus();
  const statusEl = document.getElementById('gmail-status-text');
  const accountEl = document.getElementById('gmail-account-text');
  const connectLink = document.getElementById('gmail-connect-link');
  const testRow = document.getElementById('gmail-test-row');
  if (result.ok && result.body.connected) {
    statusEl.innerHTML = '<span class="dot dot-green"></span> Connected via Google OAuth';
    accountEl.textContent = result.body.email || '(connected)';
    connectLink.hidden = true;
    testRow.hidden = false;
  } else {
    statusEl.innerHTML = '<span class="dot dot-red"></span> Not connected';
    accountEl.textContent = 'No Gmail account connected';
    connectLink.hidden = false;
    testRow.hidden = true;
  }
}

async function handleDemoReset() {
  const btn = document.getElementById('demo-reset-btn');
  const el = document.getElementById('demo-reset-result');
  btn.disabled = true;
  const result = await resetDemo();
  btn.disabled = false;
  if (result.ok && result.body.success) {
    el.style.color = '';
    el.textContent = `Reset complete (run ${result.body.demo_run_id}).`;
  } else if (result.body && result.body.error_code === 'DEMO_MODE_DISABLED') {
    el.style.color = 'var(--color-oxblood)';
    el.textContent = 'Server is not running with ARGUS_DEMO_MODE=1 — reset is disabled.';
  } else {
    el.style.color = 'var(--color-oxblood)';
    el.textContent = (result.body && result.body.detail) || 'Reset failed.';
  }
}

async function handleGmailTest() {
  const to = document.getElementById('gmail-test-to').value.trim();
  const el = document.getElementById('gmail-test-result');
  if (!to) { el.textContent = 'Enter a recipient address first.'; return; }
  const btn = document.getElementById('gmail-test-btn');
  btn.disabled = true;
  const result = await gmailTest(to);
  btn.disabled = false;
  if (result.ok && result.body.success) {
    el.style.color = '';
    el.textContent = `Test email sent to ${to}.`;
  } else {
    el.style.color = 'var(--color-oxblood)';
    el.textContent = (result.body && result.body.detail) || 'Send failed.';
  }
}

let settingsInitialized = false;
function initSettingsPage() {
  if (settingsInitialized) return;
  settingsInitialized = true;
  initProfileSwitcher();
  initEstop();
  initContacts();
  document.getElementById('demo-reset-btn').addEventListener('click', handleDemoReset);
  document.getElementById('gmail-test-btn').addEventListener('click', handleGmailTest);
}

async function loadSettings() {
  initSettingsPage();
  applyProfileUI();
  applyEstopUI();
  renderContacts();
  await refreshGmailStatus();
}

PAGE_LOADERS.settings = loadSettings;

function init() {
  const isAuthenticated = sessionStorage.getItem('argus_session') === 'authenticated';
  setState({ isAuthenticated, currentPage: currentPageFromUrl() });
  initLogin();
  initGlobalNav();
  if (isAuthenticated) {
    initWorkbench();
    const page = getState().currentPage;
    if (page !== 'workbench' && PAGE_LOADERS[page]) PAGE_LOADERS[page]();
  }
}

document.addEventListener('DOMContentLoaded', init);
