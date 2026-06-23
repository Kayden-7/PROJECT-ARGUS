// ARGUS frontend — backend API layer.
//
// IMPORTANT: field names below were verified directly against the real backend
// code (app.py / argus/*.py on origin/main), not against FRONTEND_HANDOFF.md's
// prose, which has several factual errors against the actual implementation:
//   - GET /api/queue and GET /api/audit return BARE ARRAYS, not {success, queue:[]}
//     or {success, audit:[]} as the doc claims.
//   - approve/reject responses use the field `id`, not `queue_id`.
//   - /api/trust/<action_type>/history returns `points` (with previous_trust,
//     delta, resulting_trust, reason), not `history` (with trust_score, event).
//   - /api/agent/confirm returns the SAME shape as /api/propose
//     (success/decision/decision_dict/queue/trust), not a simplified
//     {success, queue_id, decision, reason} shape.
// See FRONTEND_HANDOFF.md for the doc's version; trust this file instead.

// Relative, not hardcoded to localhost:8081 — this assumes Flask serves the
// frontend itself (same origin as the API). That works unmodified both for
// local dev (page + API both on :8081) and once hosted on Replit (page + API
// both on whatever URL Replit assigns) — no per-environment config needed,
// and no CORS headers required since requests are never cross-origin.
// Requires: a Flask route serving frontend/ as static files (ask Kayden).
const API_BASE = '';

const REQUEST_TIMEOUT_MS = 10000;

// Never throws — every caller in app.js checks result.ok rather than using
// try/catch, so network errors and timeouts are normalized into the same
// {ok:false, status, body:{error_code, detail}} shape as a real 4xx/5xx
// response, instead of becoming an unhandled rejection.
async function request(path, options) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...options, signal: controller.signal });
  } catch (e) {
    clearTimeout(timeoutId);
    if (e.name === 'AbortError') {
      return { ok: false, status: 0, body: { error_code: 'TIMEOUT', detail: `Request to ${path} timed out after ${REQUEST_TIMEOUT_MS / 1000}s.` } };
    }
    return { ok: false, status: 0, body: { error_code: 'NETWORK_ERROR', detail: e.message } };
  }
  clearTimeout(timeoutId);
  let body;
  try {
    body = await res.json();
  } catch (e) {
    return { ok: false, status: res.status, body: { error_code: 'INVALID_RESPONSE', detail: `Non-JSON response from ${path}` } };
  }
  return { ok: res.ok, status: res.status, body };
}

// GET /api/gmail/messages?limit=20
// Response: { success, messages: [{id, subject, sender, receivedAt, snippet}] }
// or { success:false, error_code:"GMAIL_NOT_CONNECTED", detail }
export async function fetchInbox(limit = 20) {
  return request(`/api/gmail/messages?limit=${limit}`);
}

// POST /api/agent/run
// Response (verified real shape): { agent_status: "PROPOSAL"|"AGENT_NEEDS_CLARIFICATION"|"AGENT_UNAVAILABLE",
//   agent_proposal_id, proposal:{action_type,entities,intent}, grounding_confirmed,
//   selected_email, agent_prompt_version, taxonomy_version }
// or on AGENT_NEEDS_CLARIFICATION: { agent_status, uncertainties:[...], agent_prompt_version, taxonomy_version }
//   (no agent_proposal_id field at all in this case — doc wrongly claims agent_proposal_id:null)
export async function runAgent(command, selectedEmailId = null) {
  return request('/api/agent/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ command, selected_email_id: selectedEmailId }),
  });
}

// POST /api/agent/confirm
// Response: IDENTICAL shape to /api/propose —
//   { success, decision:"ALLOW"|"GATED"|"BLOCK", decision_dict:{...}, queue:{id,expires_at,status}|null, trust:{...}|null }
// On unknown/consumed proposal id: { success:false, error_code:"PROPOSAL_NOT_FOUND", detail } (404)
export async function confirmAgent(agentProposalId) {
  return request('/api/agent/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agent_proposal_id: agentProposalId }),
  });
}

// GET /api/queue
// Response: BARE ARRAY of queue items (NOT wrapped in {success, queue:[]})
export async function fetchQueue() {
  return request('/api/queue');
}

// GET /api/queue/<id>
export async function fetchQueueItem(queueId) {
  return request(`/api/queue/${queueId}`);
}

// POST /api/queue/<id>/approve
// Response: { success, id, status:"APPROVED", approved_at } — field is `id`, not `queue_id`
export async function approveQueue(queueId) {
  return request(`/api/queue/${queueId}/approve`, { method: 'POST' });
}

// POST /api/queue/<id>/reject
// Response: { success, id, status:"REJECTED", proposal_json } — field is `id`, not `queue_id`;
// returns proposal_json, not the reason text back.
export async function rejectQueue(queueId, reason = '') {
  return request(`/api/queue/${queueId}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  });
}

// POST /api/queue/<id>/cancel — not in the handoff doc at all, but real and live.
export async function cancelQueue(queueId) {
  return request(`/api/queue/${queueId}/cancel`, { method: 'POST' });
}

// GET /api/audit?limit=100
// Response: BARE ARRAY of audit entries (hash-chained: prev_entry_hash, entry_hash, etc.)
export async function fetchAudit(limit = 100) {
  return request(`/api/audit?limit=${limit}`);
}

// GET /api/audit/summary?since=<timestamp>
// Response: { since, decisions:{total,ALLOW,GATED,BLOCK,candidate_allow_downgraded_to_gated},
//   safety_downgrades_by_reason, human_oversight:{approvals,rejections,cancellations,approval_rate},
//   execution:{completed,manual_review_unresolved}, trust:{changes,net_delta}, note }
export async function fetchAuditSummary(since = 0) {
  return request(`/api/audit/summary?since=${since}`);
}

// GET /api/audit/verify — not in the doc. { valid, checked, chain_head, note }
export async function verifyAuditChain() {
  return request('/api/audit/verify');
}

// GET /api/audit/replay/<correlation_id> — not in the doc.
// Response: { correlation_id, label, events:[{...full audit row incl. payload}] }
export async function fetchAuditReplay(correlationId) {
  return request(`/api/audit/replay/${encodeURIComponent(correlationId)}`);
}

// GET /api/trust/<action_type>/history
// Response: { action_type, stepped:true, points:[{timestamp, previous_trust, delta, resulting_trust, reason}] }
export async function fetchTrustHistory(actionType) {
  return request(`/api/trust/${encodeURIComponent(actionType)}/history`);
}

// GET /api/trust/<action_type> — current snapshot, not in the doc but used for the
// trust selector's "current value" display alongside the history graph.
// Response: { success, action_type, trust, raw_trust, label, description, event_count,
//   inertia_active, damping_active, damping_remaining, overall_modifier, profile, ceiling }
export async function fetchTrustSnapshot(actionType) {
  return request(`/api/trust/${encodeURIComponent(actionType)}`);
}

// GET /api/gmail/status — { connected, email }
export async function fetchGmailStatus() {
  return request('/api/gmail/status');
}

// POST /api/templates — { contact, action_type, settings:{...} } -> { success, id, version }
export async function saveTemplate(payload) {
  return request('/api/templates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

// GET /api/templates — bare array of template objects
export async function fetchTemplates() {
  return request('/api/templates');
}

// DELETE /api/templates/<id>
export async function deleteTemplate(templateId) {
  return request(`/api/templates/${templateId}`, { method: 'DELETE' });
}

// GET /api/templates/match?contact=&action_type= — {status:"OK"|"DEFAULT"|"MANUAL_REVIEW", scope, settings, template_id}
export async function matchTemplate(contact, actionType) {
  const params = new URLSearchParams();
  if (contact) params.set('contact', contact);
  if (actionType) params.set('action_type', actionType);
  return request(`/api/templates/match?${params.toString()}`);
}

// GET /api/executions — bare array of pending_executions rows
export async function fetchExecutions() {
  return request('/api/executions');
}

// POST /api/executions/tick — explicit reconcile trigger; {success, counts:{status:count}}
export async function tickExecutions() {
  return request('/api/executions/tick', { method: 'POST' });
}

// GET /health
export async function fetchHealth() {
  return request('/health');
}

// POST /demo/reset — { success, demo_run_id, reset_at } or, if the server
// wasn't started with ARGUS_DEMO_MODE=1: { success:false, error_code:"DEMO_MODE_DISABLED" } (403)
export async function resetDemo() {
  return request('/demo/reset', { method: 'POST' });
}

// POST /api/gmail/test — { to, subject?, body? } -> { success, ...sendResult }
// or { success:false, error_code:"GMAIL_NOT_CONNECTED"|"MISSING_RECIPIENT"|"SEND_FAILED", detail }
export async function gmailTest(to, subject, body) {
  return request('/api/gmail/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ to, subject, body }),
  });
}
