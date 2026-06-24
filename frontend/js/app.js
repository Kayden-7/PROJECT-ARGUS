// ARGUS frontend — main orchestration. Screen content (login form markup,
// workbench panels, audit table, trust graph) is filled in by later tasks;
// this file currently wires page routing + the state renderer hookup only.

import { getState, setState, setRenderer } from './state.js';
import {
  fetchInbox, runAgent, confirmAgent, fetchQueue, approveQueue, rejectQueue, cancelQueue,
  fetchAudit, fetchAuditSummary, fetchTrustHistory, fetchGmailStatus, fetchTrustSnapshot,
  fetchTemplates, saveTemplate, deleteTemplate, matchTemplate, fetchExecutions, tickExecutions,
  verifyAuditChain, fetchAuditReplay, resetDemo, gmailTest,
  fetchEmergencyStopStatus, setEmergencyStop,
  fetchExecutionDelay, setExecutionDelay,
  fetchActiveProfile, setActiveProfile,
  fetchPrivateContacts, addPrivateContact, removePrivateContact,
  reopenQueue,
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
// Server-authoritative: the real flag lives in system_state (SYSTEM_HARD_STOP)
// and the executor's preflight actually checks it before any Gmail work, so the
// frontend has to reflect the real value, not a local guess. estopEngaged is a
// cache of the last known server state — synced via syncEstopUI(), read
// synchronously everywhere else via isEstopActive() so existing call sites
// (which just want "is it on right now") don't need to become async.
let estopEngaged = false;

function isEstopActive() {
  return estopEngaged;
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

// Pulls the real state from the server and re-renders. Call this whenever the
// page loads or after any action that might have changed it elsewhere (another
// tab, another operator) — never just trust the last-known cache for long.
async function syncEstopUI() {
  const result = await fetchEmergencyStopStatus();
  estopEngaged = result.ok && result.body.success ? !!result.body.engaged : estopEngaged;
  applyEstopUI();
}

function initEstop() {
  const btn = document.getElementById('estop-btn');
  const status = document.getElementById('estop-status');
  if (btn) {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      const result = await setEmergencyStop(!estopEngaged);
      btn.disabled = false;
      if (result.ok && result.body.success) {
        estopEngaged = result.body.engaged;
        applyEstopUI();
      } else if (status) {
        status.classList.add('alert');
        status.textContent = (result.body && result.body.detail) || 'Could not change emergency stop — try again.';
      }
    });
  }
  syncEstopUI();
}

// ── profile switcher (server-authoritative: POST /api/system/profile) ───────
// ACTIVE_PROFILE drives the policy threshold AND the trust ceiling shown on the
// workbench, so the switch must hit the backend — localStorage alone left the
// two out of sync (workbench always read the server's Balanced ceiling).
function applyProfileUI(profile) {
  const active = profile || 'Balanced';
  document.querySelectorAll('.profile-opt').forEach((b) => {
    b.classList.toggle('active', b.getAttribute('data-profile') === active);
  });
}

// Reflect the real backend profile (re-run on every settings visit).
async function syncProfileUI() {
  const cur = await fetchActiveProfile();
  applyProfileUI(cur.ok && cur.body.success ? cur.body.profile : 'Balanced');
}

async function initProfileSwitcher() {
  const switcher = document.getElementById('profile-switcher');
  if (!switcher) return;

  await syncProfileUI();

  switcher.addEventListener('click', async (e) => {
    const btn = e.target.closest('.profile-opt');
    if (!btn) return;
    const profile = btn.getAttribute('data-profile');
    const prev = document.querySelector('.profile-opt.active')?.getAttribute('data-profile');
    applyProfileUI(profile);                       // optimistic
    const res = await setActiveProfile(profile);
    if (!res.ok || !res.body.success) {
      applyProfileUI(prev);                        // revert on failure
      return;
    }
    // Keep the workbench gauge/ceiling in sync with the new profile.
    const proposal = getState().proposal;
    if (proposal && proposal.action_type) refreshTrustForAction(proposal.action_type);
  });
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

// Single funnel for "a page is about to load" so page-scoped background work
// (right now: the executions auto-poll interval) always gets torn down when
// navigating away, no matter which of the three entry points triggered it.
function dispatchPageLoad(page) {
  stopExecutionsPolling();
  if (PAGE_LOADERS[page]) PAGE_LOADERS[page]();
}

function initGlobalNav() {
  document.querySelectorAll('.nav-link').forEach((link) => {
    // These are hrefless anchors — make them real keyboard-operable controls.
    link.setAttribute('role', 'link');
    if (!link.hasAttribute('tabindex')) link.setAttribute('tabindex', '0');
    const go = () => {
      const page = link.getAttribute('data-page');
      setState({ currentPage: page });
      window.history.replaceState({}, '', `?page=${page}`);
      dispatchPageLoad(page);
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
// Pagination (Task 4) + All/Unread filter (Task 5). Gmail's metadata API
// (argus/gmail_client.py:list_messages) never returns a read/unread flag, so
// "read" is tracked app-side instead: a message counts as read the moment
// it's clicked (selectEmail), not when it's replied to. Persisted to
// localStorage, so it survives a refresh but is scoped to this browser.
const INBOX_ITEMS_PER_PAGE = 20;
const READ_IDS_KEY = 'argus.read_message_ids';
let inboxCurrentPage = 1;
let inboxFilterMode = 'all';

function loadReadIds() {
  try {
    return new Set(JSON.parse(localStorage.getItem(READ_IDS_KEY) || '[]'));
  } catch (e) {
    return new Set();
  }
}
const readMessageIds = loadReadIds();

function markMessageRead(id) {
  if (readMessageIds.has(id)) return false;
  readMessageIds.add(id);
  localStorage.setItem(READ_IDS_KEY, JSON.stringify([...readMessageIds]));
  return true;
}

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
      ? '<p class="empty-state">No unread messages.</p>'
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
    item.className = `inbox-item${msg.isUnread ? ' is-unread' : ''}`;
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
  // Clicking is what makes it read — no reply required. msg is the same
  // object reference held in state.inboxEmails (not a copy), so mutating
  // isUnread here updates state directly; re-rendering then reflects it
  // immediately, including dropping out of the Unread filter if that's active.
  const justRead = markMessageRead(msg.id);
  if (justRead) msg.isUnread = false;
  setState({ selectedEmailId: msg.id });
  renderInbox();
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
  // A message is unread unless it's been clicked before (persisted in
  // readMessageIds) — a brand-new message has never been clicked, so it
  // starts unread by definition, in both "All" and "Unread".
  const messages = result.body.messages.map((msg) => ({
    ...msg, isUnread: !readMessageIds.has(msg.id),
  }));
  setState({ inboxEmails: messages, inboxError: null });
  renderInboxItems(messages);
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

// templates.validate_body()'s exact failure strings (argus/templates.py) — a
// drafted body that misses the template isn't the same as a missing field,
// so it gets its own labels instead of falling back to the raw backend string.
const TEMPLATE_FAILURE_LABELS = {
  'exceeds max_words': 'the draft came out longer than the template allows',
  'exceeds max_sentences': 'the draft has more sentences than the template allows',
  'exceeds max_paragraphs': 'the draft has more paragraphs than the template allows',
  'contains structural header / metadata': 'the draft accidentally included email-header-like text',
};

function humanizeClarifyToken(t) {
  const lower = String(t).toLowerCase().trim();
  if (CLARIFY_LABELS[lower]) return CLARIFY_LABELS[lower];
  if (TEMPLATE_FAILURE_LABELS[t]) return TEMPLATE_FAILURE_LABELS[t];
  if (typeof t === 'string' && t.startsWith('contains avoided phrase:')) {
    return `the draft used a phrase you've asked it to avoid (${t.split(':').slice(1).join(':').trim()})`;
  }
  return String(t);
}

function renderClarification(tokens) {
  const el = document.getElementById('proposal-status');
  const parts = (tokens || []).filter(Boolean).map(humanizeClarifyToken);
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

  const bodyRow = document.getElementById('proposal-body-row');
  if (entities.body) {
    bodyRow.hidden = false;
    document.getElementById('proposal-body').textContent = entities.body;
  } else {
    bodyRow.hidden = true;
  }
}

// ── toast: queued-action notification (Task 3) ───────────────────────────
// The View button routes through the app's real page router (setState +
// PAGE_LOADERS), not a full page reload — app.js is a module and the page
// is a single-document SPA, so location.href tricks would needlessly reload.
function navigateToPage(page) {
  setState({ currentPage: page });
  window.history.replaceState({}, '', `?page=${page}`);
  dispatchPageLoad(page);
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

// ── plain-English trace ──────────────────────────────────────────────────
// argus/policy_engine.py emits a trace of {step, result, reason, before,
// after} objects — readable to an engineer, not to the person approving the
// action. This turns each step into one short sentence instead of dumping
// the raw JSON, using regex against the existing `reason` text rather than
// duplicating the backend's numbers (so it can never drift out of sync with
// what the policy engine actually decided).
const TRACE_ICON = {
  PASS: '✓', ALLOW: '✓', OK: '✓',
  BLOCK: '✗', FAIL: '✗', DB_FAIL: '✗', EXCEPTION: '✗',
  GATED: '⏸', BUMPED: '⏸',
  SKIPPED: '–', RELAXED: 'ℹ',
};
function traceIconClass(result) {
  if (['BLOCK', 'FAIL', 'DB_FAIL', 'EXCEPTION'].includes(result)) return 'is-block';
  if (['GATED', 'BUMPED'].includes(result)) return 'is-gated';
  if (result === 'SKIPPED') return 'is-skip';
  return 'is-pass';
}
function num(re, str) {
  const m = re.exec(str || '');
  return m ? m[1] : null;
}
function humanizeTraceStep(item) {
  if (typeof item === 'string') return item;
  const { step, result, reason = '' } = item;
  switch (step) {
    case 'SYSTEM_HARD_STOP':
      return 'Emergency stop was not engaged, so the check continued.';
    case 'PRIME_RULES':
      return 'Could not check the prime-rule list (database error) — held for review.';
    case 'PRIME_RULE': {
      const action = reason.replace(/ is a Prime Rule$/, '');
      return `“${action}” matches a hard-blocked rule, so it's blocked outright — no exceptions.`;
    }
    case 'PRIME_RULE_CHECK':
      return 'No hard-blocked rule applied to this action.';
    case 'FREE_ACTION_CHECK':
      return result === 'ALLOW'
        ? `This is a low-risk action type, so it ran automatically with no approval needed.`
        : `This action type always needs a human's approval before it can run.`;
    case 'POLICY_GATE': {
      if (result === 'BLOCK') return 'No policy exists for this action type, so it was blocked by default (fail-closed).';
      const minT = num(/min=([\d.]+)/, reason);
      const profT = num(/profile_threshold=([\d.]+)/, reason);
      return `Checked against the active profile — needs at least ${minT} trust, and the current profile requires ${profT}.`;
    }
    case 'CONTACT_PERMISSION':
      if (result === 'RELAXED') {
        const by = num(/relaxed by ([\d.-]+)/, reason);
        return `This contact has extra permission on file, which lowered the trust requirement by ${by} points.`;
      }
      return 'No special permission on file for this contact — the standard requirement applies.';
    case 'IMPORTANCE_CHECK': {
      const m = /importance=(\w+).*?severity (\w+)→(\w+)/.exec(reason);
      if (!m) return reason;
      const [, importance, from, to] = m;
      return result === 'BUMPED'
        ? `Marked “${importance}” importance, which raised the risk level from ${from} to ${to}.`
        : `Importance was “${importance}” — risk level stayed at ${to}.`;
    }
    case 'TRUST_READ': {
      const trust = num(/trust=([\d.]+)/, reason);
      const count = num(/count=(\d+)/, reason);
      return `Looked up current trust for this action: ${trust} out of 100, built from ${count} past action(s).`;
    }
    case 'TRUST_CHECK': {
      const trust = num(/trust ([\d.]+)/, reason);
      const threshold = num(/threshold ([\d.]+)/, reason);
      return result === 'ALLOW'
        ? `Trust (${trust}) meets the threshold this profile requires (${threshold}) — approved automatically.`
        : `Trust (${trust}) is below the threshold this profile requires (${threshold}) — needs your approval.`;
    }
    case 'DB_CONNECT':
      return 'Could not reach the database — held for review rather than guessing.';
    case 'EVALUATE':
      return 'An unexpected error happened while evaluating this action — held for review.';
    case 'VALIDATION':
      return 'The request was missing required information or had an invalid format, so it was rejected before any policy check ran.';
    default:
      // Unknown future step — still better than raw JSON: drop the braces/quotes.
      return reason || `${step}: ${result}`;
  }
}
function renderTrace(trace) {
  const list = document.getElementById('decision-trace');
  list.innerHTML = '';
  (trace || []).forEach((item) => {
    const li = document.createElement('li');
    const result = typeof item === 'string' ? 'PASS' : item.result;
    const icon = document.createElement('span');
    icon.className = `trace-icon ${traceIconClass(result)}`;
    icon.textContent = TRACE_ICON[result] || '•';
    const text = document.createElement('span');
    text.textContent = humanizeTraceStep(item);
    li.appendChild(icon);
    li.appendChild(text);
    list.appendChild(li);
  });
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
  renderTrace(dd.trace);

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
      <button id="approve-btn" class="btn-primary">Approve</button>
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
      // Approved — this card's job is done. Stop the countdown, clear the
      // policy decision and authorisation widgets (their job was deciding
      // whether to ask; that's settled now), and keep only the AI Proposal
      // visible. What happens next (the countdown to send, cancelling, the
      // actual execution) lives on the Executions page from here on.
      if (countdownHandle) { clearInterval(countdownHandle); countdownHandle = null; }
      document.getElementById('authorisation-slot').innerHTML = '';
      document.getElementById('decision-card').hidden = true;
      setProposalStatus('Approved — see the Execution Queue page for its countdown and status.');
      refreshExecutionsPage();
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

// ── approval queue (lives on the Executions page; Task 1 consolidation) ─────
// Each PENDING item gets inline Approve / Reject controls + a live countdown.
// Inline onclick can't be used here: app.js is an ES module, so handlers are
// module-scoped and not reachable from HTML attribute strings — every control
// is wired with addEventListener instead.
let queueCountdownHandle = null;

// Why each stuck state happened, in plain English — item.status_reason often
// already has specifics (e.g. the exact auto-lock count), shown as a detail
// line underneath this framing rather than replacing it.
const STUCK_STATE_COPY = {
  HELD: "Held before reaching Gmail — almost always because Emergency Stop was engaged, or the system's hard-stop epoch changed while this was in flight. Nothing was sent. Reopen to send it back for a fresh approval.",
  MANUAL_REVIEW_TIMEOUT: "This sat in manual review for too long without a decision (10 minutes) and timed out. Reopen to put it back in front of you for approval or rejection.",
  TRANSITION_LOCKED: "Locked after several invalid attempts to change its status in a short window — a safety measure against rapid retries, not anything you did wrong with this specific click. Reopen to unlock it.",
};
const REOPENABLE_STATES = ['HELD', 'MANUAL_REVIEW_TIMEOUT', 'TRANSITION_LOCKED'];

function statusBadgeClass(status) {
  if (status === 'PENDING' || status === 'MANUAL_REVIEW' || REOPENABLE_STATES.includes(status)) return 'gated';
  if (status === 'APPROVED' || status === 'EXECUTED') return 'allow';
  if (status === 'REJECTED' || status === 'EXPIRED' || status === 'CANCELLED') return 'block';
  return 'config';
}

function renderQueueItems(items, scheduledItems = []) {
  const container = document.getElementById('queue-items');
  if (queueCountdownHandle) { clearInterval(queueCountdownHandle); queueCountdownHandle = null; }
  container.innerHTML = '';
  if (!items.length && !scheduledItems.length) {
    container.innerHTML = '<p class="proposal-hint">Nothing waiting right now.</p>';
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

    if (item.status === 'MANUAL_REVIEW') {
      // Queue-level MANUAL_REVIEW (a proposal awaiting a decision under extra
      // scrutiny) — distinct from an execution-level MANUAL_REVIEW shown in
      // Live Execution (something already approved that hit trouble mid-send).
      // This one is still approve/reject-able, same as PENDING.
      const explain = document.createElement('p');
      explain.className = 'queue-item-status';
      explain.textContent = item.status_reason || 'Flagged for extra scrutiny before a decision — still yours to approve or reject.';
      row.appendChild(explain);
    }

    if (item.status === 'PENDING' || item.status === 'MANUAL_REVIEW') {
      const controls = document.createElement('div');
      controls.className = 'queue-item-controls';

      const approveBtn = document.createElement('button');
      approveBtn.className = 'btn-primary';
      approveBtn.textContent = 'Approve';

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
        statusLine.textContent = 'Approving…';
        const result = await approveQueue(item.id);
        if (!(result.ok && result.body.success)) {
          statusLine.textContent = (result.body && result.body.detail) || 'Approval failed.';
          approveBtn.disabled = false; rejectBtn.disabled = false;
          return;
        }
        // Approval does NOT execute immediately — it queues for execution after
        // the configured delay (see Settings > Execution Delay). The Live
        // Execution section below picks it up once that delay elapses.
        statusLine.textContent = 'Approved — will execute after the delay window.';
        refreshExecutionsPage();
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
    } else if (REOPENABLE_STATES.includes(item.status)) {
      // HELD / MANUAL_REVIEW_TIMEOUT / TRANSITION_LOCKED: the backend's only
      // way forward is reopen() — there is no approve/reject from here, and a
      // reason is mandatory (the backend rejects an empty one outright).
      const explain = document.createElement('p');
      explain.className = 'queue-item-status';
      explain.textContent = STUCK_STATE_COPY[item.status] || 'This item needs to be reopened before any further action.';
      row.appendChild(explain);

      // TRANSITION_LOCKED's specific reason lands in transition_lock_reason,
      // not status_reason (see argus/queue.py:_handle_invalid) — check both.
      const detailText = item.status_reason || item.transition_lock_reason;
      if (detailText) {
        const detail = document.createElement('p');
        detail.className = 'proposal-hint';
        detail.textContent = detailText;
        row.appendChild(detail);
      }

      const reopenForm = document.createElement('div');
      reopenForm.className = 'queue-reject-form';
      const reasonInput = document.createElement('textarea');
      reasonInput.placeholder = 'Reason for reopening (required)';
      reasonInput.maxLength = 500;
      const reasonError = document.createElement('p');
      reasonError.className = 'banner-message';
      reasonError.style.color = 'var(--color-oxblood)';
      reasonError.hidden = true;
      reasonError.textContent = 'A reason is required to reopen.';
      const reopenBtn = document.createElement('button');
      reopenBtn.className = 'btn-primary';
      reopenBtn.textContent = 'Reopen';
      const statusLine = document.createElement('p');
      statusLine.className = 'queue-item-status';

      reopenBtn.addEventListener('click', async () => {
        const reason = reasonInput.value.trim();
        if (!reason) { reasonError.hidden = false; return; }
        reasonError.hidden = true;
        reopenBtn.disabled = true;
        const result = await reopenQueue(item.id, reason);
        if (result.ok && result.body.success) {
          statusLine.textContent = 'Reopened — back to PENDING for a fresh decision.';
          refreshExecutionsPage();
        } else {
          statusLine.textContent = (result.body && result.body.detail) || 'Could not reopen — try again.';
          reopenBtn.disabled = false;
        }
      });

      reopenForm.appendChild(reasonInput);
      reopenForm.appendChild(reasonError);
      reopenForm.appendChild(reopenBtn);
      row.appendChild(reopenForm);
      row.appendChild(statusLine);
    }

    container.appendChild(row);
  });

  // Approved-but-not-yet-promoted items: still nothing has actually happened,
  // so they belong here (not in Live Execution) — with a real countdown and,
  // critically, a Cancel button. This is the only point before the delay
  // elapses where the action can still be reversed.
  scheduledItems.forEach((ex) => {
    const row = document.createElement('div');
    row.className = 'queue-item';

    const head = document.createElement('div');
    head.className = 'queue-item-head';

    const badge = document.createElement('span');
    badge.className = 'decision-badge config';
    badge.textContent = 'SCHEDULED';

    const action = document.createElement('span');
    action.className = 'queue-item-action';
    action.textContent = ex.action_type;

    const countdown = document.createElement('span');
    countdown.className = 'queue-item-expiry queue-schedule-countdown';
    countdown.setAttribute('data-execute-at', String(ex.execute_at));

    head.appendChild(badge);
    head.appendChild(action);
    head.appendChild(countdown);
    row.appendChild(head);

    const reassure = document.createElement('p');
    reassure.className = 'queue-item-status';
    reassure.textContent = 'Approved. Sends automatically when the countdown ends, unless you cancel it first.';
    row.appendChild(reassure);

    const controls = document.createElement('div');
    controls.className = 'queue-item-controls';
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn-secondary';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.addEventListener('click', async () => {
      cancelBtn.disabled = true;
      const result = await cancelQueue(ex.approval_id);
      if (result.ok && result.body.success) {
        refreshExecutionsPage();
      } else {
        reassure.textContent = (result.body && result.body.detail) || 'Could not cancel — try again.';
        cancelBtn.disabled = false;
      }
    });
    controls.appendChild(cancelBtn);
    row.appendChild(controls);

    container.appendChild(row);
  });

  // One interval ticks every visible countdown; disables controls on expiry.
  const tickAll = () => {
    const pendingSpans = document.querySelectorAll('#queue-items .queue-countdown');
    const scheduleSpans = document.querySelectorAll('#queue-items .queue-schedule-countdown');
    if (!pendingSpans.length && !scheduleSpans.length) {
      clearInterval(queueCountdownHandle); queueCountdownHandle = null; return;
    }
    pendingSpans.forEach((span) => {
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
    scheduleSpans.forEach((span) => {
      const executeAt = Number(span.getAttribute('data-execute-at'));
      const remaining = executeAt - Math.floor(Date.now() / 1000);
      if (remaining <= 0) {
        span.textContent = 'Sending now…';
        const row = span.closest('.queue-item');
        if (row) row.querySelectorAll('button').forEach((b) => { b.disabled = true; b.classList.add('is-disabled'); });
      } else {
        span.textContent = `Sends in ${formatCountdown(remaining)}`;
        span.classList.toggle('is-urgent', remaining <= 10);
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
    // Three backend shapes: free-form `uncertainties` (pass 1), a draft-fit
    // `failures` array (drafting actions — the real specific reasons, e.g.
    // "exceeds max_paragraphs"), or just `detail` as a last resort. Previously
    // this only ever read `detail`, so a draft-fit failure always showed the
    // generic "draft did not fit the template" instead of the real reason.
    const tokens = agentBody.uncertainties && agentBody.uncertainties.length
      ? agentBody.uncertainties
      : (agentBody.failures && agentBody.failures.length
          ? agentBody.failures
          : (agentBody.detail ? [agentBody.detail] : []));
    renderClarification(tokens);
    btn.disabled = false;
    return;
  }
  if (agentBody.agent_status === 'AGENT_UNAVAILABLE') {
    setProposalStatus('Agent is unavailable — the server is missing its OPENAI_API_KEY configuration. This is a server setup issue, not a problem with your command.');
    btn.disabled = false;
    return;
  }
  if (agentBody.agent_status === 'AGENT_PRIVATE_CONTACT_PROTECTED') {
    // Phase 8 Part 4 — this contact is on the Private Contacts list (Settings).
    // The command WAS understood; it's refused on purpose, not a parsing failure.
    setProposalStatus(agentBody.detail || 'This contact is protected — ARGUS will not act on or message them.');
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

  // Auto-grow with content instead of letting the user drag-resize it (CSS
  // already sets resize:none + max-height so this can never grow past the
  // card or the page). Reset to 'auto' first so it can shrink back down
  // when text is deleted, not just keep growing.
  const input = document.getElementById('command-input');
  const autoGrow = () => {
    input.style.height = 'auto';
    input.style.height = `${input.scrollHeight}px`;
  };
  input.addEventListener('input', autoGrow);
  autoGrow();
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
  syncEstopUI(); // estop-btn listener itself is attached once, from Settings (initSettingsPage)

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
  if (['QUEUE_REOPENED', 'SYSTEM_HARD_STOP_ENABLED', 'SYSTEM_HARD_STOP_DISABLED',
       'POLICY_PROFILE_CHANGED', 'PRIVATE_CONTACT_ADDED', 'PRIVATE_CONTACT_REMOVED',
       'DEMO_RESET_COMPLETED'].includes(entry.event_type)) return 'You';
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

// `reason` is sometimes a real human-written note (a rejection reason, an
// emergency-stop justification someone typed) and sometimes one of the
// backend's internal failure_reason_code constants (SYSTEM_HARD_STOP,
// SAFETY_DOWNGRADE_UNRECOGNIZED_DOMAIN, etc.) — those are codes, not
// sentences, so translate the known ones instead of showing them raw.
const REASON_CODE_LABELS = {
  SYSTEM_HARD_STOP: 'Emergency stop was engaged',
  TRUST_BELOW_THRESHOLD: 'Trust was below the required threshold',
  INTERNAL_ERROR: 'An internal error occurred',
  MISSING_BODY: 'The request body was missing',
  QUEUE_FAILURE: 'Could not write to the approval queue',
  PRIVATE_CONTACT_PROTECTED: 'Recipient is a protected contact',
  EXECUTOR_BLOCKED_HARD_STOP: 'Execution held — emergency stop engaged',
  EXECUTOR_BLOCKED_PRIVATE_CONTACT: 'Execution blocked — protected contact',
  INVALID_TRANSITION_RATE_LIMITED: 'Too many invalid status-change attempts — auto-locked',
  SAFETY_DOWNGRADE_DELETE: 'Downgraded to approval — deletions are never auto-allowed',
  SAFETY_DOWNGRADE_MALFORMED_RECIPIENT: 'Downgraded to approval — recipient address looked malformed',
  SAFETY_DOWNGRADE_EXTERNAL_FORWARD: 'Downgraded to approval — forwarding outside the organisation',
  SAFETY_DOWNGRADE_UNRECOGNIZED_DOMAIN: 'Downgraded to approval — recipient domain is not pre-trusted',
  SAFETY_DOWNGRADE_BCC: 'Downgraded to approval — a Bcc recipient was present',
  SAFETY_DOWNGRADE_NEW_RECIPIENTS: 'Downgraded to approval — new recipients added',
  UNKNOWN_ACTION_TYPE: 'Unrecognised action type',
  MISSING_REQUIRED_FIELD: 'A required field was missing',
  EMPTY_REQUIRED_FIELD: 'A required field was empty',
  INVALID_FIELD_TYPE: 'A field had the wrong type',
  INVALID_ACTION_EXPIRY: 'Invalid expiry value',
  MISSING_ACTION_TYPE: 'No action type was given',
};

// event_type fallback when there's no reason at all (e.g. a clean
// PRIVATE_CONTACT_ADDED with no operator note) — same plain-English standard.
const EVENT_TYPE_LABELS = {
  PRIVATE_CONTACT_ADDED: 'Private contact added',
  PRIVATE_CONTACT_REMOVED: 'Private contact removed',
  PRIVATE_CONTACT_PROTECTED: 'Blocked — protected contact',
  SYSTEM_HARD_STOP_ENABLED: 'Emergency stop engaged',
  SYSTEM_HARD_STOP_DISABLED: 'Emergency stop disengaged',
  QUEUE_TRANSITIONED: 'Queue status changed',
  QUEUE_TRANSITION_LOCKED: 'Queue item auto-locked',
  QUEUE_REOPENED: 'Queue item reopened',
  MANUAL_REVIEW_TIMEOUT: 'Manual review timed out',
  HELD_STALE_EPOCH: 'Execution held (emergency stop)',
  POLICY_PROFILE_CHANGED: 'Policy profile changed',
  DEMO_RESET_COMPLETED: 'Demo data reset',
};

function auditLabel(entry) {
  if (entry.reason && REASON_CODE_LABELS[entry.reason]) return REASON_CODE_LABELS[entry.reason];
  if (entry.reason) return entry.reason; // a genuine human-written note — show as-is
  if (entry.event_type && EVENT_TYPE_LABELS[entry.event_type]) return EVENT_TYPE_LABELS[entry.event_type];
  return entry.event_type || '(event)';
}

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

const TERMINAL_PIPELINE_LABELS = {
  MANUAL_REVIEW: 'Paused for review',
  FAILED: 'Failed',
  HELD: 'Held — emergency stop',
  SUPERSEDED: 'Superseded',
};
function renderPipeline(status) {
  const wrap = document.createElement('div');
  wrap.className = 'execution-pipeline';
  if (TERMINAL_PIPELINE_LABELS[status]) {
    const step = document.createElement('span');
    step.className = 'pipeline-step';
    step.textContent = TERMINAL_PIPELINE_LABELS[status];
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
    card.className = `execution-card${(ex.status === 'MANUAL_REVIEW' || ex.status === 'HELD') ? ' is-review' : ''}`;

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

    if (ex.status === 'HELD') {
      const reassure = document.createElement('p');
      reassure.className = 'execution-reassurance';
      // Honest about a known limitation (see DEFERRED.md, "Fence B"): nothing
      // was sent, but there's no automatic resume once the hold clears — no
      // Reopen button here would actually do anything, so this doesn't offer one.
      reassure.textContent = `${ex.status_reason || 'Held before reaching Gmail — most likely Emergency Stop was engaged.'} Nothing was sent. This is a held state without an automatic resume yet — if it stays stuck after disengaging Emergency Stop, that's expected today, not a sign something went wrong.`;
      card.appendChild(reassure);
    }

    if (ex.status === 'SUPERSEDED') {
      const reassure = document.createElement('p');
      reassure.className = 'execution-reassurance';
      reassure.style.color = 'var(--color-secondary-ink)';
      reassure.textContent = 'Replaced by a newer approval of the same request before this one reached Gmail. Nothing was sent from this attempt — purely historical.';
      card.appendChild(reassure);
    }

    const meta = document.createElement('div');
    meta.className = 'execution-meta';
    meta.textContent = `attempt ${ex.attempt_count}` + (ex.message_id ? ` · message ${ex.message_id}` : ex.draft_id ? ` · draft ${ex.draft_id}` : '');
    card.appendChild(meta);

    container.appendChild(card);
  });
}

// Single coordinating refresh for both halves of the Executions page. Fetches
// the queue and the execution pipeline together and renders them as one
// picture — SCHEDULED items (approved, counting down, still cancellable)
// render into the Execution Queue, not Live Execution, since nothing has
// actually happened to them yet. Calling loadQueue()/renderExecutions()
// separately would race: whichever finished last would wipe out the other's
// DOM (both write to overlapping containers), so every call site that needs
// either uses this instead.
async function refreshExecutionsPage() {
  const [queueResult, execResult] = await Promise.all([fetchQueue(), fetchExecutions()]);
  const pendingItems = queueResult.ok ? queueResult.body : [];
  const allExecutions = execResult.ok ? execResult.body : [];
  const scheduled = allExecutions.filter((ex) => ex.status === 'SCHEDULED');
  const inProgress = allExecutions.filter((ex) => ex.status !== 'SCHEDULED');
  setState({ queueItems: pendingItems });
  renderQueueItems(pendingItems, scheduled);
  renderExecutions(inProgress);
}

// Auto-advance the pipeline without the user clicking "Check for updates":
// while the Executions page is open, poll the server every few seconds so a
// SCHEDULED item's delay elapsing, a draft being created, and the actual send
// all happen on their own. Started by PAGE_LOADERS.executions, stopped by
// dispatchPageLoad() the moment the user navigates anywhere else.
let executionsPollHandle = null;
function startExecutionsPolling() {
  stopExecutionsPolling();
  executionsPollHandle = setInterval(() => { refreshExecutionsPage(); }, 4000);
}
function stopExecutionsPolling() {
  if (executionsPollHandle) { clearInterval(executionsPollHandle); executionsPollHandle = null; }
}

let executionsInitialized = false;
function initExecutionsPage() {
  if (executionsInitialized) return;
  executionsInitialized = true;
  document.getElementById('executions-tick-btn').addEventListener('click', async () => {
    const status = document.getElementById('executions-tick-status');
    if (status) status.textContent = 'Checking…';
    const result = await tickExecutions();
    if (status) {
      if (result.ok && result.body.success) {
        const counts = result.body.counts || {};
        const parts = Object.entries(counts).map(([k, v]) => `${v} ${k.toLowerCase()}`);
        status.textContent = parts.length ? `Now: ${parts.join(', ')}.` : 'Nothing in the execution pipeline right now.';
      } else {
        status.textContent = (result.body && result.body.detail) || 'Could not check for updates — try again.';
      }
    }
    refreshExecutionsPage();
  });
}

// Executions page now hosts both sections (Task 1): the approval queue and the
// live execution pipeline. Load both whenever the page is opened, and keep
// polling automatically (see startExecutionsPolling) for as long as the user
// stays on this page.
PAGE_LOADERS.executions = () => {
  initExecutionsPage();
  refreshExecutionsPage();
  startExecutionsPolling();
};

// ── private contacts ─────────────────────────────────────────────────────────
// Server-authoritative (argus/private_contacts.py via /api/private-contacts).
// Enforced at TWO points on the backend: before the agent ever interprets a
// selected email from this address, and again at the executor right before any
// send — so adding someone after a proposal is already approved still blocks
// it. Matching is an EXACT normalized email address only, never a name, so the
// add form validates for a real address instead of accepting free text.
let privateContactsCache = [];

function renderContacts() {
  const container = document.getElementById('contact-list');
  container.innerHTML = '';
  if (!privateContactsCache.length) {
    container.innerHTML = '<p class="empty-state">No private contacts added.</p>';
    return;
  }
  privateContactsCache.forEach((c) => {
    const item = document.createElement('div');
    item.className = 'contact-item';
    const label = document.createElement('span');
    label.className = 'contact-name';
    label.textContent = c.display_label ? `${c.display_label} <${c.normalized_email}>` : c.normalized_email;
    const removeBtn = document.createElement('button');
    removeBtn.className = 'contact-remove-btn';
    removeBtn.textContent = 'Remove';
    removeBtn.addEventListener('click', async () => {
      removeBtn.disabled = true;
      const result = await removePrivateContact(c.normalized_email);
      if (result.ok && result.body.success) {
        refreshPrivateContacts();
      } else {
        removeBtn.disabled = false;
      }
    });
    item.appendChild(label);
    item.appendChild(removeBtn);
    container.appendChild(item);
  });
}

async function refreshPrivateContacts() {
  const result = await fetchPrivateContacts();
  privateContactsCache = (result.ok && result.body.success) ? result.body.contacts : [];
  renderContacts();
}

const EMAIL_PATTERN = /^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/;

function initContacts() {
  const input = document.getElementById('contact-add-input');
  const errorEl = document.getElementById('contact-add-error');
  const btn = document.getElementById('contact-add-btn');
  btn.addEventListener('click', async () => {
    const value = input.value.trim();
    if (!value) { input.focus(); return; }
    if (!EMAIL_PATTERN.test(value)) {
      if (errorEl) {
        errorEl.hidden = false;
        errorEl.textContent = "Enter a full email address — this only matches exact addresses, never names.";
      }
      return;
    }
    if (errorEl) errorEl.hidden = true;
    btn.disabled = true;
    const result = await addPrivateContact(value);
    btn.disabled = false;
    if (result.ok && result.body.success) {
      input.value = '';
      refreshPrivateContacts();
    } else if (errorEl) {
      errorEl.hidden = false;
      errorEl.textContent = (result.body && result.body.detail) || 'Could not add contact — try again.';
    }
  });
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

// Execution delay slider — backend-enforced (server clamps to a 1-minute
// floor regardless of what's sent; see kernel.MIN_EXECUTION_DELAY_SECONDS).
// The same window the executor waits out before sending IS the cancel/undo
// window shown on queue items ("Cancels in…") — one number, two effects.
function setExecutionDelayDisplay(minutes) {
  document.getElementById('execution-delay-display').textContent =
    `${minutes} minute${String(minutes) === '1' ? '' : 's'}`;
}

async function initExecutionDelay() {
  const slider = document.getElementById('execution-delay-slider');
  const status = document.getElementById('execution-delay-status');
  if (!slider) return;

  // Seed from localStorage instantly so the slider isn't blank while the
  // network call resolves, then reconcile with the server's real value.
  const cached = localStorage.getItem('argus.execution_delay') || '4';
  slider.value = cached;
  setExecutionDelayDisplay(cached);

  const result = await fetchExecutionDelay();
  if (result.ok && result.body.success) {
    const minutes = Math.max(1, Math.round(result.body.seconds / 60));
    slider.value = String(minutes);
    setExecutionDelayDisplay(minutes);
    localStorage.setItem('argus.execution_delay', String(minutes));
  }

  slider.addEventListener('input', () => setExecutionDelayDisplay(slider.value));

  // Write on release (not every 'input' tick) — one request per adjustment,
  // not one per pixel of drag.
  slider.addEventListener('change', async () => {
    const minutes = Number(slider.value);
    localStorage.setItem('argus.execution_delay', String(minutes));
    if (status) status.textContent = 'Saving…';
    const res = await setExecutionDelay(minutes * 60);
    if (!res.ok || !res.body.success) {
      if (status) status.textContent = (res.body && res.body.detail) || 'Could not save — try again.';
      return;
    }
    if (res.body.clamped) {
      const clampedMinutes = Math.max(1, Math.round(res.body.seconds / 60));
      slider.value = String(clampedMinutes);
      setExecutionDelayDisplay(clampedMinutes);
      localStorage.setItem('argus.execution_delay', String(clampedMinutes));
      if (status) status.textContent = `Delay can't go below 1 minute — set to ${clampedMinutes} minute(s).`;
    } else if (status) {
      status.textContent = 'Saved.';
      setTimeout(() => { if (status.textContent === 'Saved.') status.textContent = ''; }, 2000);
    }
  });
}

async function loadSettings() {
  initSettingsPage();
  syncProfileUI();
  syncEstopUI();
  refreshPrivateContacts();
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
    if (page !== 'workbench') dispatchPageLoad(page);
  }
}

document.addEventListener('DOMContentLoaded', init);
