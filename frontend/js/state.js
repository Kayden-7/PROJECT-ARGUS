// ARGUS frontend — state management (per FRONTEND_HANDOFF.md spec).
// Single source of truth for app state; setState() merges + triggers a re-render.

const appState = {
  // Auth
  isAuthenticated: false,

  // Current page: 'workbench' | 'audit' | 'trust' | 'settings'
  currentPage: 'workbench',

  // Inbox
  inboxEmails: [],
  selectedEmailId: null,
  inboxLoading: false,
  inboxError: null,

  // Proposal flow
  command: '',
  proposal: null,
  agentProposalId: null,
  proposalLoading: false,
  proposalError: null,
  uncertainties: null,

  // Decision (decision_dict from /api/propose-shaped response)
  decision: null,
  decisionQueue: null,
  decisionTrust: null,
  decisionLoading: false,

  // Queue
  queueItems: [],
  queueLoading: false,

  // Audit
  auditEvents: [],
  auditSummary: null,
  auditLoading: false,

  // Trust history
  trustPoints: [],
  selectedTrustActionType: null,
  trustLoading: false,
};

// renderFn is injected by app.js (avoids a circular import between state/app).
let renderFn = function () {};

export function setRenderer(fn) {
  renderFn = fn;
}

export function setState(updates) {
  Object.assign(appState, updates);
  renderFn();
}

export function getState() {
  return appState;
}
