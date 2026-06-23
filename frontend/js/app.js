// ARGUS frontend — main orchestration. Screen content (login form markup,
// workbench panels, audit table, trust graph) is filled in by later tasks;
// this file currently wires page routing + the state renderer hookup only.

import { getState, setState, setRenderer } from './state.js';
import {
  fetchInbox, runAgent, confirmAgent, fetchQueue, fetchQueueItem, approveQueue, rejectQueue,
  fetchAudit, fetchAuditSummary, fetchTrustHistory, fetchGmailStatus, fetchTrustSnapshot,
  fetchTemplates, saveTemplate, deleteTemplate, matchTemplate, fetchExecutions, tickExecutions,
  verifyAuditChain, fetchAuditReplay, resetDemo, gmailTest,
  setEmergencyStop, savePrivateContacts,
} from './api.js';

// Mirrors config.py ALL_ACTIONS — kept in sync manually, same approach used
// throughout this build (no shared-schema endpoint exists to derive this from).
// calendar.* is intentionally omitted from the UI (frontend-only decision —
// the backend still supports it); this surfaces email.* / label.apply only.
const ALL_ACTIONS = [
  'email.compose', 'email.archive', 'email.mark_read', 'email.star', 'email.move',
  'label.apply',
  'email.send.external', 'email.send.internal', 'email.reply', 'email.forward',
  'email.delete',
];

let countdownHandle = null;

const PAGES = ['login', 'workbench', 'audit', 'trust', 'templates', 'executions', 'settings'];

// ── emergency stop ──────────────────────────────────────────────────────────
// Local-first today: localStorage is the immediate UI source of truth, and the
// toggle also fires setEmergencyStop() — a no-op (NOT_WIRED) until ESTOP_ENDPOINT
// is set in api.js. Phase 8: fill that constant to go server-authoritative; the
// only further change is to stop trusting localStorage as the canonical state.
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
      // Phase 8: flips to server-authoritative once ESTOP_ENDPOINT is set (no-op until then).
      setEmergencyStop(isEstopActive());
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
    // These are hrefless anchors — make them real keyboard-operable controls.
    link.setAttribute('role', 'link');
    if (!link.hasAttribute('tabindex')) link.setAttribute('tabindex', '0');
    const go = () => {
      const page = link.getAttribute('data-page');
      setState({ currentPage: page });
      window.history.replaceState({}, '', `?page=${page}`);
      if (PAGE_LOADERS[page]) PAGE_LOADERS[page]();
    };
    link.addEventListener('click', go);
    link.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
    });
  });
}

// ── account menu (avatar dropdown → log out) ─────────────────────────────────
// The avatar appears in every page's nav; only the active page's is visible.
// Each gets its own dropdown so log out works from any screen.
function closeAllUserMenus() {
  document.querySelectorAll('.avatar-menu').forEach((m) => { m.hidden = true; });
  document.querySelectorAll('.avatar[aria-expanded]').forEach((a) => a.setAttribute('aria-expanded', 'false'));
}

function logout() {
  closeAllUserMenus();
  sessionStorage.removeItem('argus_session');
  setState({ isAuthenticated: false });
  window.history.replaceState({}, '', '?page=login');
  // Clear the login form so the next sign-in starts blank.
  ['username', 'password'].forEach((id) => { const el = document.getElementById(id); if (el) el.value = ''; });
  const err = document.getElementById('login-error');
  if (err) err.textContent = '';
}

function initUserMenu() {
  document.querySelectorAll('.avatar').forEach((avatar) => {
    avatar.setAttribute('role', 'button');
    avatar.setAttribute('tabindex', '0');
    avatar.setAttribute('aria-haspopup', 'menu');
    avatar.setAttribute('aria-expanded', 'false');
    avatar.setAttribute('aria-label', 'Account menu');

    const wrap = document.createElement('div');
    wrap.className = 'avatar-wrap';
    avatar.parentNode.insertBefore(wrap, avatar);
    wrap.appendChild(avatar);

    const menu = document.createElement('div');
    menu.className = 'avatar-menu';
    menu.setAttribute('role', 'menu');
    menu.hidden = true;
    menu.innerHTML =
      '<div class="avatar-menu-head"><div class="avatar-menu-name">PROJECT_ARGUS</div>' +
      '<div class="avatar-menu-sub">Demo session</div></div>' +
      '<button class="avatar-menu-item" role="menuitem" data-action="logout">Log out</button>';
    wrap.appendChild(menu);

    const toggle = (e) => {
      e.stopPropagation();
      const willOpen = menu.hidden;
      closeAllUserMenus();
      menu.hidden = !willOpen;
      avatar.setAttribute('aria-expanded', String(willOpen));
    };
    avatar.addEventListener('click', toggle);
    avatar.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(e); }
      else if (e.key === 'Escape') closeAllUserMenus();
    });
    menu.querySelector('[data-action="logout"]').addEventListener('click', logout);
  });

  // Close on any outside click. (Avatar/menu clicks stop propagation above.)
  document.addEventListener('click', closeAllUserMenus);
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
// Pagination (Task 4) + All/Unread filter (Task 5). Note: the Gmail backend
// (argus/gmail_client.py:list_messages) returns id/subject/sender/receivedAt/
// snippet only — there is NO read/unread flag — so the Unread filter degrades
// to an honest empty state rather than silently hiding everything.
const INBOX_ITEMS_PER_PAGE = 20;
let inboxCurrentPage = 1;
let inboxFilterMode = 'all';

function getFilteredInbox() {
  const all = getState().inboxEmails || [];
  if (inboxFilterMode === 'unread') return all.filter((e) => e.isUnread === true);
  return all;
}

function renderInbox() {
  const messages = getFilteredInbox();
  const container = document.getElementById('inbox-items');
  const pager = document.getElementById('inbox-pagination');
  container.innerHTML = '';
  pager.innerHTML = '';
  document.getElementById('inbox-count').textContent = messages.length;

  if (!messages.length) {
    container.innerHTML = inboxFilterMode === 'unread'
      ? '<p class="empty-state">Read/unread status isn’t available from Gmail metadata, so no messages can be marked unread here.</p>'
      : '<p class="empty-state">No messages.</p>';
    return;
  }

  const totalPages = Math.ceil(messages.length / INBOX_ITEMS_PER_PAGE);
  if (inboxCurrentPage > totalPages) inboxCurrentPage = 1;
  const start = (inboxCurrentPage - 1) * INBOX_ITEMS_PER_PAGE;
  const pageMessages = messages.slice(start, start + INBOX_ITEMS_PER_PAGE);
  const selectedId = getState().selectedEmailId;

  pageMessages.forEach((msg) => {
    const item = document.createElement('div');
    item.className = 'inbox-item';
    if (msg.id === selectedId) item.classList.add('selected');
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

  if (totalPages > 1) {
    for (let page = 1; page <= totalPages; page++) {
      const btn = document.createElement('button');
      btn.className = `pagination-btn${page === inboxCurrentPage ? ' active' : ''}`;
      btn.textContent = String(page);
      btn.addEventListener('click', () => { inboxCurrentPage = page; renderInbox(); });
      pager.appendChild(btn);
    }
  }
}

// Back-compat shim: loadInbox calls renderInboxItems(messages) after setState.
function renderInboxItems() { inboxCurrentPage = 1; renderInbox(); }

function setInboxFilter(mode) {
  inboxFilterMode = mode;
  document.querySelectorAll('#inbox-filter-toggle .filter-btn').forEach((b) => {
    b.classList.toggle('active', b.getAttribute('data-filter') === mode);
  });
  inboxCurrentPage = 1;
  renderInbox();
}

function selectEmail(msg) {
  setState({ selectedEmailId: msg.id });
  document.querySelectorAll('.inbox-item').forEach((el) => {
    el.classList.toggle('selected', el.getAttribute('data-email-id') === msg.id);
  });
  document.getElementById('command-email-context').textContent = `on: ${msg.subject || '(no subject)'}`;

  // Preview card (Task 6)
  const preview = document.getElementById('selected-email-preview');
  preview.hidden = false;
  document.getElementById('preview-sender').textContent = `From: ${msg.sender || 'Unknown'}`;
  document.getElementById('preview-subject').textContent = `Subject: ${msg.subject || '(no subject)'}`;
  document.getElementById('preview-snippet').textContent = msg.snippet || '(no preview)';
}

function clearEmailSelection() {
  setState({ selectedEmailId: null });
  document.querySelectorAll('.inbox-item').forEach((el) => el.classList.remove('selected'));
  document.getElementById('command-email-context').textContent = '';
  document.getElementById('selected-email-preview').hidden = true;
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

// AGENT_NEEDS_CLARIFICATION (Task 11): turn the bare uncertainty tokens into a
// calm, human request for input (not an error). Known tokens get friendly
// labels; unknowns fall back to the raw token so we never hide what's missing.
const CLARIFY_LABELS = {
  recipient: 'a recipient',
  action_type: 'what action to take',
  action: 'what action to take',
  subject: 'a subject',
  event: 'an event or time',
  time: 'a time',
  date: 'a date',
  body: 'the message content',
};

function renderClarification(tokens) {
  const el = document.getElementById('proposal-status');
  const parts = (tokens || [])
    .filter(Boolean)
    .map((t) => CLARIFY_LABELS[String(t).toLowerCase().trim()] || String(t));
  let missing = '';
  if (parts.length === 1) missing = parts[0];
  else if (parts.length > 1) missing = `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`;
  const sentence = missing
    ? `ARGUS needs more detail before it can propose this — missing: ${missing}.`
    : 'ARGUS needs more detail before it can propose this.';
  el.innerHTML = '<div class="clarify-note"><span class="clarify-mark"></span><span></span></div>';
  el.querySelector('.clarify-note span:last-child').textContent = sentence;
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

// ── toast: queued-action notification (Task 3) ───────────────────────────
// The View button routes through the app's real page router (setState +
// PAGE_LOADERS), not a full page reload — app.js is a module and the page
// is a single-document SPA, so location.href tricks would needlessly reload.
function navigateToPage(page) {
  setState({ currentPage: page });
  window.history.replaceState({}, '', `?page=${page}`);
  if (PAGE_LOADERS[page]) PAGE_LOADERS[page]();
}

function showQueueToast(actionType, expiresAt) {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    document.body.appendChild(container);
  }

  const remaining = (expiresAt || 0) - Math.floor(Date.now() / 1000);
  const timeStr = formatCountdown(Math.max(0, remaining));

  const toast = document.createElement('div');
  toast.className = 'toast toast-success';
  const msg = document.createElement('span');
  msg.textContent = `✓ ${actionType} queued — cancels in ${timeStr}`;
  const viewBtn = document.createElement('button');
  viewBtn.className = 'toast-action';
  viewBtn.textContent = 'View';
  viewBtn.addEventListener('click', () => { navigateToPage('executions'); toast.remove(); });
  toast.appendChild(msg);
  toast.appendChild(viewBtn);
  container.appendChild(toast);

  setTimeout(() => { if (toast.parentElement) toast.remove(); }, 5000);
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
  const breakdown = dd.modifier_breakdown || {};
  if (trust && trust.trust_before != null && trust.trust_after != null) {
    const sign = trust.actual_delta >= 0 ? '+' : '';
    deltaEl.textContent = `${trust.trust_before.toFixed(1)} → ${trust.trust_after.toFixed(1)} (${sign}${trust.actual_delta.toFixed(1)})`;
  } else if (decision === 'GATED' && dd.trust_impact !== 'none'
             && breakdown.success_delta != null && breakdown.failure_delta != null) {
    // decision_dict.trust_delta_preview is always null on the backend (a dead field) —
    // modifier_breakdown's raw severity-tier deltas are the real preview data available
    // pre-inertia/damping, so use those instead of silently claiming "no trust impact."
    const succ = breakdown.success_delta >= 0 ? `+${breakdown.success_delta}` : breakdown.success_delta;
    deltaEl.textContent = `Pending — est. ${succ} if approved & it succeeds, ${breakdown.failure_delta} if it fails (before inertia/damping)`;
  } else if (decision === 'GATED') {
    deltaEl.textContent = 'Trust impact pending — held for review, no impact recorded yet.';
  } else {
    deltaEl.textContent = 'No trust impact';
  }

  document.getElementById('containment-msg').hidden = decision !== 'BLOCK';
  document.getElementById('decision-trace').textContent = JSON.stringify(dd.trace || [], null, 2);

  if (decision === 'GATED' && queue) {
    renderAuthorisation(queue);
    const proposalForToast = getState().proposal;
    showQueueToast((proposalForToast && proposalForToast.action_type) || 'Action', queue.expires_at);
  } else {
    document.getElementById('authorisation-slot').innerHTML = '';
    if (countdownHandle) { clearInterval(countdownHandle); countdownHandle = null; }
  }
  loadQueue();

  const proposal = getState().proposal;
  // Keep the trust data fresh after a decision, but do NOT switch the gauge —
  // the headline only changes when the user clicks a card/chip.
  if (proposal && proposal.action_type) refreshTrustForAction(proposal.action_type);
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
    el.textContent = `Cancels in ${formatCountdown(remaining)}`;
    el.classList.toggle('is-urgent', remaining <= 30);
    if (remaining <= 0) {
      clearInterval(countdownHandle);
      const approveBtn = document.getElementById('approve-btn');
      const rejectBtn = document.getElementById('reject-btn');
      [approveBtn, rejectBtn].forEach((b) => { if (b) { b.disabled = true; b.classList.add('is-disabled'); } });
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

// ── approval queue (lives on the Executions page; Task 1 consolidation) ─────
// Each PENDING item gets inline Approve / Reject controls + a live countdown.
// Inline onclick can't be used here: app.js is an ES module, so handlers are
// module-scoped and not reachable from HTML attribute strings — every control
// is wired with addEventListener instead.
let queueCountdownHandle = null;

function statusBadgeClass(status) {
  if (status === 'PENDING' || status === 'MANUAL_REVIEW') return 'gated';
  if (status === 'APPROVED' || status === 'EXECUTED') return 'allow';
  if (status === 'REJECTED' || status === 'EXPIRED' || status === 'CANCELLED') return 'block';
  return 'config';
}

function renderQueueItems(items) {
  const container = document.getElementById('queue-items');
  if (queueCountdownHandle) { clearInterval(queueCountdownHandle); queueCountdownHandle = null; }
  container.innerHTML = '';
  if (!items.length) {
    container.innerHTML = '<p class="proposal-hint">No pending approvals.</p>';
    return;
  }
  items.forEach((item) => {
    let actionType = '(unknown)';
    try { actionType = JSON.parse(item.proposal_json).action_type; } catch (e) {}
    const row = document.createElement('div');
    row.className = 'queue-item';

    const head = document.createElement('div');
    head.className = 'queue-item-head';

    const badge = document.createElement('span');
    badge.className = `decision-badge ${statusBadgeClass(item.status)}`;
    badge.textContent = item.status;

    const action = document.createElement('span');
    action.className = 'queue-item-action';
    action.textContent = actionType;

    head.appendChild(badge);
    head.appendChild(action);

    if (item.status === 'PENDING') {
      const countdown = document.createElement('span');
      countdown.className = 'queue-item-expiry queue-countdown';
      countdown.setAttribute('data-expires', String(item.expires_at));
      head.appendChild(countdown);
    }
    row.appendChild(head);

    if (item.status === 'PENDING') {
      const controls = document.createElement('div');
      controls.className = 'queue-item-controls';

      const approveBtn = document.createElement('button');
      approveBtn.className = 'btn-primary';
      approveBtn.textContent = 'Approve & Execute';

      const rejectBtn = document.createElement('button');
      rejectBtn.className = 'btn-secondary';
      rejectBtn.textContent = 'Reject';

      const rejectForm = document.createElement('div');
      rejectForm.className = 'queue-reject-form';
      rejectForm.hidden = true;
      const reasonInput = document.createElement('textarea');
      reasonInput.placeholder = 'Reason for rejection (required)';
      reasonInput.maxLength = 500;
      const reasonError = document.createElement('p');
      reasonError.className = 'banner-message';
      reasonError.style.color = 'var(--color-oxblood)';
      reasonError.hidden = true;
      reasonError.textContent = 'A reason is required to reject.';
      const confirmReject = document.createElement('button');
      confirmReject.className = 'btn-secondary';
      confirmReject.textContent = 'Confirm Rejection';
      rejectForm.appendChild(reasonInput);
      rejectForm.appendChild(reasonError);
      rejectForm.appendChild(confirmReject);

      const statusLine = document.createElement('p');
      statusLine.className = 'queue-item-status';

      approveBtn.addEventListener('click', async () => {
        approveBtn.disabled = true; rejectBtn.disabled = true;
        statusLine.textContent = 'Approved. Checking execution outcome…';
        const result = await approveQueue(item.id);
        if (!(result.ok && result.body.success)) {
          statusLine.textContent = (result.body && result.body.detail) || 'Approval failed.';
          approveBtn.disabled = false; rejectBtn.disabled = false;
          return;
        }
        await tickExecutions();
        loadQueue();
        refreshExecutions();
      });

      rejectBtn.addEventListener('click', () => { rejectForm.hidden = false; reasonInput.focus(); });

      confirmReject.addEventListener('click', async () => {
        const reason = reasonInput.value.trim();
        if (!reason) { reasonError.hidden = false; return; }
        reasonError.hidden = true;
        confirmReject.disabled = true;
        const result = await rejectQueue(item.id, reason);
        if (result.ok && result.body.success) { loadQueue(); }
        else { statusLine.textContent = (result.body && result.body.detail) || 'Rejection failed.'; confirmReject.disabled = false; }
      });

      controls.appendChild(approveBtn);
      controls.appendChild(rejectBtn);
      row.appendChild(controls);
      row.appendChild(rejectForm);
      row.appendChild(statusLine);
    }

    container.appendChild(row);
  });

  // One interval ticks every visible countdown; disables controls on expiry.
  const tickAll = () => {
    const spans = document.querySelectorAll('#queue-items .queue-countdown');
    if (!spans.length) { clearInterval(queueCountdownHandle); queueCountdownHandle = null; return; }
    spans.forEach((span) => {
      const expiresAt = Number(span.getAttribute('data-expires'));
      const remaining = expiresAt - Math.floor(Date.now() / 1000);
      if (remaining <= 0) {
        span.textContent = 'Approval expired';
        span.classList.add('is-expired');
        const row = span.closest('.queue-item');
        if (row) row.querySelectorAll('button').forEach((b) => { b.disabled = true; b.classList.add('is-disabled'); });
      } else {
        span.textContent = `Cancels in ${formatCountdown(remaining)}`;
        span.classList.toggle('is-urgent', remaining <= 30);
      }
    });
  };
  tickAll();
  queueCountdownHandle = setInterval(tickAll, 1000);
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
    // Two backend shapes: free-form `uncertainties` (pass 1) or a draft-fit
    // `detail`/`failures` (drafting actions). Prefer the token list; fall back
    // to the detail string so the user always gets a readable reason.
    const tokens = agentBody.uncertainties && agentBody.uncertainties.length
      ? agentBody.uncertainties
      : (agentBody.detail ? [agentBody.detail] : []);
    renderClarification(tokens);
    btn.disabled = false;
    return;
  }
  if (agentBody.agent_status === 'AGENT_UNAVAILABLE') {
    setProposalStatus('Agent is unavailable — the server is missing its OPENAI_API_KEY configuration. This is a server setup issue, not a problem with your command.');
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
// Semicircle arc gauge. Flat instrument per the Visual Direction: a single
// accent-navy arc (stroke set in CSS, no per-value color interpolation/glow),
// the fill position carries the score, and the band LABEL — not the number —
// is the readout. The numeric trust value is intentionally never displayed.
const GAUGE_CENTER = { x: 110, y: 120 };
const GAUGE_RADIUS = 90;
const GAUGE_ARC_LENGTH = Math.PI * GAUGE_RADIUS;

function setGaugeValue(value) {
  const clamped = Math.max(0, Math.min(100, value));
  const fill = document.getElementById('gauge-fill');
  fill.style.strokeDasharray = GAUGE_ARC_LENGTH;
  fill.style.strokeDashoffset = GAUGE_ARC_LENGTH * (1 - clamped / 100);
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

// Client-side trust → band label. Mirrors argus/trust_ledger.py TRUST_LABELS /
// _trust_label (read-only reference, not imported): score <= threshold wins.
// Used for the client-computed TOTAL TRUST headline, which has no backend label.
function trustBandLabel(score) {
  if (score <= 20) return 'Untrusted';
  if (score <= 40) return 'Low Trust';
  if (score <= 60) return 'Developing';
  if (score <= 80) return 'Trusted';
  return 'Highly Reliable';
}

// ── trust gauge: TOTAL TRUST default + per-action drill-down ─────────────────
// The headline gauge defaults to TOTAL TRUST = the mean of every action type's
// current trust (computed client-side from the same snapshots the strip uses —
// no backend total endpoint). Clicking a strip card drills into that action's
// contextual trust; the Total chip (or re-clicking the active card) returns.
const trustSnapshots = {};   // action_type -> latest snapshot body
let gaugeMode = 'total';      // 'total' | <action_type>
let trustStripBuilt = false;

function buildTrustStrip() {
  const container = document.getElementById('trust-strip');
  if (!container || trustStripBuilt) return;
  container.innerHTML = '';
  ALL_ACTIONS.forEach((action) => {
    const item = document.createElement('div');
    item.className = 'trust-strip-item';
    item.setAttribute('data-action', action);
    item.setAttribute('role', 'button');
    item.setAttribute('tabindex', '0');
    item.innerHTML =
      '<div class="ts-top"><span class="ts-action"></span><span class="ts-band">—</span></div>' +
      '<div class="ts-bar"><span style="width:0%"></span></div>';
    item.querySelector('.ts-action').textContent = action;
    item.addEventListener('click', () => onStripCardClick(action));
    item.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onStripCardClick(action); }
    });
    container.appendChild(item);
  });
  const chip = document.getElementById('total-trust-chip');
  if (chip) chip.addEventListener('click', showTotalTrust);
  trustStripBuilt = true;
}

// Click the active card again → deselect back to Total; otherwise drill in.
function onStripCardClick(action) {
  if (gaugeMode === action) showTotalTrust();
  else showActionTrust(action);
}

function highlightTrustStrip(actionType) {
  document.querySelectorAll('#trust-strip .trust-strip-item').forEach((el) => {
    el.classList.toggle('is-current', el.getAttribute('data-action') === actionType);
  });
}

function setTotalChipActive(active) {
  const chip = document.getElementById('total-trust-chip');
  if (chip) chip.classList.toggle('is-active', active);
}

function updateTrustStripItem(actionType, data) {
  const item = document.querySelector(`#trust-strip [data-action="${actionType}"]`);
  if (!item) return;
  item.querySelector('.ts-band').textContent = data.label || '—';
  const fill = item.querySelector('.ts-bar > span');
  if (fill) fill.style.width = `${Math.max(0, Math.min(100, data.trust))}%`;
}

// Headline = mean of every action's current trust. Pure client-side; no fetch.
function showTotalTrust() {
  const vals = Object.values(trustSnapshots);
  if (!vals.length) return;
  gaugeMode = 'total';
  const mean = vals.reduce((s, d) => s + (d.trust || 0), 0) / vals.length;
  const widget = document.getElementById('trust-widget');
  widget.hidden = false;
  setGaugeValue(mean);
  document.getElementById('gauge-ceiling').setAttribute('visibility', 'hidden');
  const modeEl = document.getElementById('gauge-mode');
  modeEl.hidden = false;
  modeEl.textContent = 'TOTAL TRUST';
  document.getElementById('gauge-label').textContent = trustBandLabel(mean);
  document.getElementById('trust-widget-action').textContent = 'Total';
  document.getElementById('gauge-meta').hidden = true;
  const band = document.getElementById('total-chip-band');
  if (band) band.textContent = trustBandLabel(mean);
  highlightTrustStrip(null);
  setTotalChipActive(true);
}

// Drill into one action's contextual trust (the per-action view). Only ever
// triggered by an explicit card/chip click — never automatically.
function showActionTrust(actionType) {
  gaugeMode = actionType;
  setTotalChipActive(false);
  document.getElementById('gauge-mode').hidden = true;
  document.getElementById('gauge-ceiling').setAttribute('visibility', 'visible');
  document.getElementById('gauge-meta').hidden = false;
  refreshGauge(actionType);
}

// Refresh one action's trust data (cache + strip cell) after a decision WITHOUT
// changing what the gauge is showing — the headline only switches on user click.
// Re-renders whichever view is currently active so it reflects the new value.
async function refreshTrustForAction(actionType) {
  const result = await fetchTrustSnapshot(actionType);
  if (!result.ok || !result.body.success) return;
  trustSnapshots[actionType] = result.body;
  updateTrustStripItem(actionType, result.body);
  if (gaugeMode === 'total') showTotalTrust();
  else if (gaugeMode === actionType) refreshGauge(actionType);
}

async function loadTrustStrip() {
  buildTrustStrip();
  const results = await Promise.all(ALL_ACTIONS.map((a) => fetchTrustSnapshot(a)));
  results.forEach((res, i) => {
    if (res.ok && res.body && res.body.success) {
      trustSnapshots[ALL_ACTIONS[i]] = res.body;
      updateTrustStripItem(ALL_ACTIONS[i], res.body);
    }
  });
  // Default headline = Total Trust, unless the user already drilled into an action.
  if (gaugeMode === 'total') showTotalTrust();
}

async function refreshGauge(actionType) {
  const result = await fetchTrustSnapshot(actionType);
  const widget = document.getElementById('trust-widget');
  if (!result.ok || !result.body.success) { widget.hidden = true; return; }
  const data = result.body;
  trustSnapshots[actionType] = data;   // keep the cache (and Total) fresh
  widget.hidden = false;
  setGaugeValue(data.trust);
  setCeilingMarker(data.ceiling);
  document.getElementById('gauge-label').textContent = data.label;
  // Keep the strip's cell for this action fresh + highlighted (reuse this snapshot).
  updateTrustStripItem(actionType, data);
  highlightTrustStrip(actionType);
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

  // Inbox filter toggle (Task 5) + preview-card close (Task 6) — wired once.
  document.querySelectorAll('#inbox-filter-toggle .filter-btn').forEach((btn) => {
    btn.addEventListener('click', () => setInboxFilter(btn.getAttribute('data-filter')));
  });
  const previewClose = document.getElementById('preview-close');
  if (previewClose) previewClose.addEventListener('click', clearEmailSelection);

  loadInbox();
  loadQueue();
  loadTrustStrip(); // fetches all per-action trusts, then shows TOTAL TRUST by default
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

// Audit events are cached so the filter dropdown can re-render without refetching.
let auditEventsCache = [];

function auditLabel(entry) { return entry.reason || entry.event_type || '(event)'; }

function populateAuditFilter(events) {
  const select = document.getElementById('audit-action-filter');
  const current = select.value;
  const labels = Array.from(new Set(events.map(auditLabel))).sort();
  select.innerHTML = '<option value="">All</option>';
  labels.forEach((l) => select.appendChild(new Option(l, l)));
  if (labels.includes(current)) select.value = current;
}

function renderAuditGroups(events) {
  const container = document.getElementById('audit-groups');
  container.innerHTML = '';
  if (!events.length) {
    container.innerHTML = '<p class="empty-state">No audit entries match.</p>';
    return;
  }

  // Group by calendar date (newest day first; events already arrive newest-first).
  const grouped = {};
  events.forEach((evt) => {
    const date = new Date(evt.timestamp * 1000).toLocaleDateString();
    (grouped[date] = grouped[date] || []).push(evt);
  });

  Object.keys(grouped)
    .sort((a, b) => new Date(b) - new Date(a))
    .forEach((date) => {
      const group = document.createElement('div');
      group.className = 'audit-day-group';

      const body = document.createElement('div');
      body.className = 'audit-day-body';

      const header = document.createElement('button');
      header.className = 'audit-day-header';
      const count = grouped[date].length;
      header.textContent = `${date} ▼ (${count} event${count === 1 ? '' : 's'})`;
      header.addEventListener('click', () => {
        const collapsed = !body.hidden;
        body.hidden = collapsed;
        header.textContent = `${date} ${collapsed ? '▶' : '▼'} (${count} event${count === 1 ? '' : 's'})`;
      });

      grouped[date].forEach((evt) => {
        const row = document.createElement('div');
        row.className = 'audit-row';

        const time = document.createElement('span');
        time.className = 'audit-timestamp';
        time.textContent = new Date(evt.timestamp * 1000).toLocaleTimeString();

        const action = document.createElement('span');
        action.className = 'audit-action';
        action.textContent = auditLabel(evt);

        const outcome = document.createElement('span');
        outcome.className = `decision-badge ${outcomeBadgeClass(evt.outcome)}`;
        outcome.textContent = evt.outcome || evt.event_type;

        const actor = document.createElement('span');
        actor.className = 'audit-actor';
        actor.textContent = deriveActor(evt);

        row.appendChild(time);
        row.appendChild(action);
        row.appendChild(outcome);
        row.appendChild(actor);
        row.addEventListener('click', () => showAuditReplay(evt));
        body.appendChild(row);
      });

      group.appendChild(header);
      group.appendChild(body);
      container.appendChild(group);
    });
}

function applyAuditFilter() {
  const value = document.getElementById('audit-action-filter').value;
  const filtered = value ? auditEventsCache.filter((e) => auditLabel(e) === value) : auditEventsCache;
  renderAuditGroups(filtered);
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

// Human-readable replay modal (Task 7c). Shows plain-language fields for the
// clicked event, then fetches the full correlation replay (the real ARGUS
// /api/audit/replay feature) for the collapsible JSON detail — so we keep the
// correlation-grouped replay capability instead of dumping a single raw row.
function buildReplayField(label, value, mono) {
  const field = document.createElement('div');
  field.className = 'replay-field';
  const l = document.createElement('label');
  l.textContent = label;
  const v = document.createElement('span');
  if (mono) v.className = 'monospace';
  v.textContent = value;
  field.appendChild(l);
  field.appendChild(v);
  return field;
}

async function showAuditReplay(event) {
  let modal = document.getElementById('audit-replay-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'audit-replay-modal';
    modal.className = 'modal';
    modal.hidden = true;
    modal.addEventListener('click', (e) => { if (e.target === modal) modal.hidden = true; });
    document.body.appendChild(modal);
  }

  const payload = event.payload || {};
  const outcome = event.outcome || event.event_type || 'UNKNOWN';

  const content = document.createElement('div');
  content.className = 'modal-content';

  const close = document.createElement('button');
  close.className = 'modal-close';
  close.setAttribute('aria-label', 'Close');
  close.textContent = '×';
  close.addEventListener('click', () => { modal.hidden = true; });

  const title = document.createElement('h2');
  title.textContent = 'Event Details — Replay';

  const summary = document.createElement('div');
  summary.className = 'replay-summary';
  summary.appendChild(buildReplayField('Event type', event.event_type || 'N/A'));
  summary.appendChild(buildReplayField('Action type', payload.action_type || event.action_type || 'N/A'));
  const outcomeField = buildReplayField('Outcome', outcome);
  outcomeField.querySelector('span').className = `decision-badge ${outcomeBadgeClass(event.outcome)}`;
  summary.appendChild(outcomeField);
  summary.appendChild(buildReplayField('Evaluated', new Date(event.timestamp * 1000).toLocaleString()));
  summary.appendChild(buildReplayField('Reason', event.reason || 'No reason recorded'));
  summary.appendChild(buildReplayField('Actor', deriveActor(event)));
  summary.appendChild(buildReplayField('Correlation ID', event.correlation_id || 'N/A', true));

  const details = document.createElement('details');
  const dsum = document.createElement('summary');
  dsum.textContent = 'Full correlation replay (JSON)';
  const pre = document.createElement('pre');
  pre.textContent = 'Loading…';
  details.appendChild(dsum);
  details.appendChild(pre);

  content.appendChild(close);
  content.appendChild(title);
  content.appendChild(summary);
  content.appendChild(details);
  modal.innerHTML = '';
  modal.appendChild(content);
  modal.hidden = false;

  if (event.correlation_id) {
    const result = await fetchAuditReplay(event.correlation_id);
    pre.textContent = result.ok
      ? JSON.stringify(result.body.events, null, 2)
      : 'Could not load full replay.';
  } else {
    pre.textContent = JSON.stringify(event, null, 2);
  }
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
  document.getElementById('audit-action-filter').addEventListener('change', applyAuditFilter);
}

async function loadAudit() {
  initAuditPage();
  const [auditResult, summaryResult] = await Promise.all([fetchAudit(100), fetchAuditSummary()]);
  if (auditResult.ok) {
    auditEventsCache = auditResult.body;
    populateAuditFilter(auditEventsCache);
    applyAuditFilter();
  }
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
    svg.innerHTML = '<text x="400" y="140" text-anchor="middle" fill="#5B6B80" font-size="14">No history yet for this action type.</text>';
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
  axis.setAttribute('stroke', '#D9DBE0');
  svg.appendChild(axis);

  const pathD = points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(p.timestamp)} ${y(p.resulting_trust)}`).join(' ');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', pathD);
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', '#1D3557');
  path.setAttribute('stroke-width', '2');
  svg.appendChild(path);

  points.forEach((p) => {
    const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    dot.setAttribute('cx', x(p.timestamp));
    dot.setAttribute('cy', y(p.resulting_trust));
    dot.setAttribute('r', 4);
    dot.setAttribute('fill', '#1D3557');
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
    removeBtn.addEventListener('click', async () => {
      removeBtn.disabled = true;
      const result = await deleteTemplate(tpl.id);
      if (!result.ok) {
        removeBtn.disabled = false;
        document.getElementById('tpl-form-error').hidden = false;
        document.getElementById('tpl-form-error').textContent = (result.body && result.body.detail) || 'Could not remove boundary.';
        return;
      }
      refreshTemplates();
    });
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

// Executions page now hosts both sections (Task 1): the approval queue and the
// live execution pipeline. Load both whenever the page is opened.
PAGE_LOADERS.executions = () => { initExecutionsPage(); loadQueue(); refreshExecutions(); };

// ── private contacts ─────────────────────────────────────────────────────────
// ARGUS should never process or decide on emails involving these people. Local-
// first today (localStorage); every save also calls savePrivateContacts() — a
// no-op (NOT_WIRED) until PRIVATE_CONTACTS_ENDPOINT is set in api.js. Phase 8
// (server-side enforcement before the AI sees email content): fill that constant.
const CONTACTS_KEY = 'argus.privateContacts';

function loadContacts() {
  try { return JSON.parse(localStorage.getItem(CONTACTS_KEY)) || []; }
  catch (e) { return []; }
}

function saveContacts(list) {
  localStorage.setItem(CONTACTS_KEY, JSON.stringify(list));
  // Phase 8: flips to server-authoritative once PRIVATE_CONTACTS_ENDPOINT is set (no-op until then).
  savePrivateContacts(list);
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
  initExecutionDelay();
  document.getElementById('demo-reset-btn').addEventListener('click', handleDemoReset);
  document.getElementById('gmail-test-btn').addEventListener('click', handleGmailTest);
}

// Execution delay slider (Task 9a) — frontend-only preference persisted to
// localStorage. Backend enforcement of the delay lands in Phase 8 Part 5.
function setExecutionDelayDisplay(minutes) {
  document.getElementById('execution-delay-display').textContent =
    `${minutes} minute${String(minutes) === '1' ? '' : 's'}`;
}

function initExecutionDelay() {
  const slider = document.getElementById('execution-delay-slider');
  if (!slider) return;
  const saved = localStorage.getItem('argus.execution_delay') || '4';
  slider.value = saved;
  setExecutionDelayDisplay(saved);
  slider.addEventListener('input', () => {
    localStorage.setItem('argus.execution_delay', slider.value);
    setExecutionDelayDisplay(slider.value);
  });
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
  initUserMenu();
  if (isAuthenticated) {
    initWorkbench();
    const page = getState().currentPage;
    if (page !== 'workbench' && PAGE_LOADERS[page]) PAGE_LOADERS[page]();
  }
}

document.addEventListener('DOMContentLoaded', init);
