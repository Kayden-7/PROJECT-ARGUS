# ARGUS FRONTEND — COMPREHENSIVE IMPLEMENTATION BRIEF

**For:** Baldwin (Frontend Developer)  
**Project:** ARGUS (Permission & Trust Layer for AI Agents)  
**Submission Deadline:** 25 Jun 2026 12pm SGT  
**Status:** Backend 100% done (748 tests passing). Frontend skeleton needed by EOD tomorrow to allow iteration.

---

## BUILD RULE
**Build frontend code ONLY.** No tests, no commits, no next-step planning. Create files, push to GitHub when done. Kayden will commit.

---

## OVERVIEW

ARGUS is a deterministic permission/trust layer for AI agents. The frontend must visually reinforce: **"AI proposes. Code decides."**

**Architecture:**
- Layer 1: GPT-4o (proposal only, never decides)
- Layer 2: Python policy engine (all decisions)
- Layer 3: Execution (Gmail integration)

**Your job:** Build a single-page app where users:
1. Login (fake credentials for dev)
2. Select a Gmail email from the inbox
3. Type a natural command ("reply saying I'll be there")
4. See AI proposal + policy decision + human approval control
5. Approve/reject
6. View audit trail + trust history

**Tech stack:**
- HTML5 / CSS3 / vanilla JavaScript (no frameworks)
- Fetch API for backend calls
- localStorage for session + state
- Responsive (desktop-first, mobile support)

**Design system:**
- **Palette:** Ink-navy (`#182128`) + evergreen (`#0E6254`) + amber/oxblood for states
- **Typography:** Libre Franklin (headings/body) + IBM Plex Mono (timestamps/badges/monospace)
- **Spacing:** 8px grid
- **Card radius:** 4px max
- **Shadows:** None (minimal aesthetic)

---

## FILE STRUCTURE

Create this folder structure in PROJECT-ARGUS:

```
PROJECT-ARGUS/
├── frontend/
│   ├── index.html                 ← Single entry point
│   ├── css/
│   │   └── style.css              ← All styling
│   └── js/
│       ├── app.js                 ← Main app orchestration
│       ├── api.js                 ← Backend API calls
│       └── state.js               ← State management (proposal, approval flow, etc.)
├── app.py                         ← Backend (already built)
├── config.py
├── argus/
├── tests/
└── ... (other backend files)
```

---

## FAKE LOGIN SETUP

**For development:** No real Gmail OAuth. Use hardcoded credentials.

**Credentials:**
```
Username: PROJECT_ARGUS
Password: ARGUS_DEMO
```

**Flow:**
1. User opens `index.html` → redirects to login page
2. User enters credentials above
3. On submit: validate locally, set `sessionStorage['argus_session'] = 'authenticated'`
4. Redirect to `/frontend/index.html?page=workbench`
5. If session missing/expired → redirect back to login

**No real OAuth.** Backend `/api/gmail/connect` exists but is optional for dev.

---

## BACKEND API CONTRACT

**Backend runs on:** `http://localhost:8081` (or env `PORT`)

**All endpoints return JSON.** Errors are 4xx/5xx with `{ error_code, detail }` structure.

### Authentication
None required for dev. (Fake login is frontend-only.)

### Core Endpoints

#### 1. GET `/api/gmail/messages?limit=20`
**Purpose:** Fetch user's inbox (read-only, for email selection)
**Response:**
```json
{
  "success": true,
  "messages": [
    {
      "id": "gmail_msg_id_123",
      "subject": "Sprint review tomorrow",
      "sender": "Maya Chen <maya@example.com>",
      "receivedAt": "2026-06-22T10:42:00Z",
      "snippet": "Can you confirm attendance?"
    },
    ...
  ]
}
```
**Error (Gmail not connected):**
```json
{
  "success": false,
  "error_code": "GMAIL_NOT_CONNECTED",
  "detail": "Connect Gmail first at /api/gmail/connect"
}
```

#### 2. POST `/api/agent/run`
**Purpose:** Send user command + optional selected email to GPT-4o for proposal
**Request:**
```json
{
  "command": "Reply saying I'll be there.",
  "selected_email_id": "gmail_msg_id_123"
}
```
**Response (PROPOSAL):**
```json
{
  "agent_status": "PROPOSAL",
  "agent_proposal_id": "uuid-1234",
  "grounding_confirmed": true,
  "selected_email": {
    "id": "gmail_msg_id_123",
    "subject": "Sprint review tomorrow",
    "sender": "Maya Chen <maya@example.com>",
    "receivedAt": "2026-06-22T10:42:00Z"
  },
  "proposal": {
    "action_type": "email.reply",
    "entities": {
      "recipient": "maya@example.com",
      "body": "I'll be there."
    },
    "intent": "Confirm attendance at sprint review"
  }
}
```
**Response (NEEDS_CLARIFICATION):**
```json
{
  "agent_status": "AGENT_NEEDS_CLARIFICATION",
  "uncertainties": ["Missing recipient. Is this for Maya or someone else?"],
  "agent_proposal_id": null
}
```
**Response (ERROR):**
```json
{
  "agent_status": "AGENT_OUTPUT_INVALID",
  "detail": "GPT-4o error or parsing failure"
}
```

#### 3. POST `/api/agent/confirm`
**Purpose:** Submit the stored proposal to policy engine for decision
**Request:**
```json
{
  "agent_proposal_id": "uuid-1234"
}
```
**Response (ALLOW):**
```json
{
  "success": true,
  "queue_id": "queue-uuid-456",
  "decision": "ALLOW",
  "reason": "Reply to known contact within trust threshold."
}
```
**Response (GATED):**
```json
{
  "success": true,
  "queue_id": "queue-uuid-456",
  "decision": "GATED",
  "reason": "External email action requires explicit approval."
}
```
**Response (BLOCK):**
```json
{
  "success": true,
  "queue_id": "queue-uuid-456",
  "decision": "BLOCK",
  "reason": "Recipient domain is not recognized by policy."
}
```

#### 4. GET `/api/queue`
**Purpose:** Fetch all pending/recent queue items
**Response:**
```json
{
  "success": true,
  "queue": [
    {
      "id": "queue-uuid-456",
      "proposal_json": { "action_type": "email.reply", ... },
      "decision_json": { "decision": "GATED", "reason": "..." },
      "status": "PENDING",
      "created_at": 1719050520,
      "expires_at": 1719050820,
      "approved_at": null
    }
  ]
}
```

#### 5. GET `/api/queue/<id>`
**Purpose:** Fetch one queue item
**Response:** Same as single item from `/api/queue`

#### 6. POST `/api/queue/<id>/approve`
**Purpose:** Approve a GATED action
**Request:** `{}` (empty body)
**Response:**
```json
{
  "success": true,
  "queue_id": "queue-uuid-456",
  "status": "APPROVED",
  "message": "Action approved and queued for execution."
}
```

#### 7. POST `/api/queue/<id>/reject`
**Purpose:** Reject an action
**Request:**
```json
{
  "reason": "I changed my mind."
}
```
**Response:**
```json
{
  "success": true,
  "queue_id": "queue-uuid-456",
  "status": "REJECTED",
  "reason": "I changed my mind."
}
```

#### 8. GET `/api/audit?limit=100`
**Purpose:** Fetch audit trail (append-only event log)
**Response:**
```json
{
  "success": true,
  "audit": [
    {
      "id": 1,
      "timestamp": 1719050520,
      "event_type": "DECISION_EVALUATED",
      "action_type": "email.reply",
      "outcome": "GATED",
      "reason": "External communication requires approval",
      "payload": { "recipient_scope": "UNRECOGNIZED_EXTERNAL", ... }
    },
    {
      "id": 2,
      "timestamp": 1719050525,
      "event_type": "QUEUE_TRANSITIONED",
      "outcome": "APPROVED",
      "payload": {}
    }
  ]
}
```

#### 9. GET `/api/audit/summary?since=<timestamp>`
**Purpose:** Summary stats (total actions, approval rate, etc.)
**Response:**
```json
{
  "decisions": { "total": 5, "ALLOW": 2, "GATED": 2, "BLOCK": 1 },
  "safety_downgrades_by_reason": { "external_forward": 1 },
  "human_oversight": { "approvals": 2, "rejections": 1, "approval_rate": 0.667 },
  "execution": { "completed": 2, "manual_review_unresolved": 0 },
  "trust": { "changes": 3, "net_delta": 5.5 }
}
```

#### 10. GET `/api/trust/<action_type>/history`
**Purpose:** Trust score timeline for one action type
**Response:**
```json
{
  "success": true,
  "action_type": "email.reply",
  "history": [
    { "timestamp": 1719050000, "trust_score": 40.0, "event": "Initial" },
    { "timestamp": 1719050500, "trust_score": 47.5, "event": "Approved action executed" },
    { "timestamp": 1719051000, "trust_score": 42.0, "event": "Rejection recorded" }
  ]
}
```

#### 11. GET `/health`
**Purpose:** Backend health check
**Response:**
```json
{
  "status": "ok",
  "system": "ARGUS",
  "version": "1.0"
}
```

---

## SCREEN-BY-SCREEN IMPLEMENTATION

### SCREEN 1: LOGIN PAGE

**URL:** `index.html` (default)  
**State:** Session not authenticated

**Layout:**
```
┌─────────────────────────────────────┐
│                                     │
│            ARGUS                    │
│   Permission & Trust Layer          │
│                                     │
│   Username: [PROJECT_ARGUS        ] │
│   Password: [ARGUS_DEMO           ] │
│                                     │
│         [ Sign In ]                 │
│                                     │
│   (No account needed for demo)      │
│                                     │
└─────────────────────────────────────┘
```

**HTML Structure:**
```html
<div id="login-page" class="page">
  <div class="login-container">
    <h1>ARGUS</h1>
    <p class="tagline">Permission & Trust Layer for AI Agents</p>
    <form id="login-form">
      <input type="text" id="username" placeholder="Username" required>
      <input type="password" id="password" placeholder="Password" required>
      <button type="submit">Sign In</button>
    </form>
    <p class="hint">Demo: PROJECT_ARGUS / ARGUS_DEMO</p>
  </div>
</div>
```

**JavaScript Logic:**
1. On form submit:
   - Validate: username === "PROJECT_ARGUS" && password === "ARGUS_DEMO"
   - If valid: set `sessionStorage['argus_session'] = 'authenticated'`
   - Redirect: `window.location.href = 'index.html?page=workbench'`
   - If invalid: show error message "Invalid credentials"

---

### SCREEN 2: DECISION WORKBENCH (Main)

**URL:** `index.html?page=workbench`  
**State:** Session authenticated, proposal/decision flow

**Layout (Desktop, ≥1024px):**
```
┌─────────────────────────────────────────────────────────────────────┐
│ ARGUS                                        Audit | Trust | Settings│
├──────────────────┬──────────────────────────────────────────────────┤
│ INBOX            │ DECISION WORKBENCH                               │
│                  │                                                  │
│ ┌──────────────┐ │ Selected email • Maya Chen • 10:42               │
│ │ Maya Chen    │ │                                                  │
│ │ 10:42        │ │ ┌──────────────────────────────────────────────┐│
│ │ Sprint       │ │ │ AI PROPOSAL                                  ││
│ │ review       │ │ │ ──────────────────────────────────────────   ││
│ │ tomorrow     │ │ │ Command: Reply saying I'll be there.          ││
│ │ [selected]   │ │ │ Interpreted: Reply confirming attendance.     ││
│ └──────────────┘ │ └──────────────────────────────────────────────┘│
│                  │                                                  │
│ [more emails]    │ ┌──────────────────────────────────────────────┐│
│                  │ │ POLICY DECISION                              ││
│ QUEUE:           │ │ ──────────────────────────────────────────   ││
│ Pending: 1       │ │ GATED                                         ││
│ Approved: 0      │ │ External email action requires approval.      ││
│ Rejected: 0      │ │ Policy: External communication / approval req ││
│                  │ └──────────────────────────────────────────────┘│
│                  │                                                  │
│                  │ AUTHORISATION        Expires in 00:45            │
│                  │ [ Approve & Execute                            ] │
│                  │ [ Reject                                       ] │
│                  │                                                  │
│ [Command box]    │                                                  │
│ [Generate]       │                                                  │
└──────────────────┴──────────────────────────────────────────────────┘
```

**Tablet (481–768px):**
```
Collapse inbox to modal/drawer
Stack vertically: selected email → proposal → decision → buttons
Full-width queue below
```

**Mobile (≤480px):**
```
One screen at a time
Inbox: full-screen sheet
Proposal/decision/buttons: full-screen card
Queue: full-screen sheet
Tap to switch between screens
```

**Components:**

#### 2.1 Inbox List (Left pane, desktop only)
```html
<div id="inbox" class="inbox-list">
  <h3>INBOX</h3>
  <div id="inbox-loader" class="loader">Loading emails...</div>
  <div id="inbox-items"></div>
  <!-- Item template -->
  <div class="inbox-item" data-email-id="gmail_msg_id">
    <strong>Sender Name</strong>
    <span class="time">10:42</span>
    <div class="subject">Subject line here</div>
    <div class="snippet">Email preview text...</div>
  </div>
</div>
```

**JavaScript Logic:**
1. On mount: fetch `/api/gmail/messages?limit=20`
2. Render each email as `.inbox-item`
3. On click: set `state.selectedEmailId = email.id`, highlight border
4. Show sender, subject, snippet, time

#### 2.2 Selected Email Context
```html
<div id="selected-email-context">
  <p><strong>Selected email</strong> • <span id="selected-sender"></span> • <span id="selected-time"></span></p>
</div>
```

#### 2.3 Command Composer
```html
<div id="command-composer">
  <h3>What should ARGUS do?</h3>
  <textarea id="command-input" placeholder="e.g., 'Reply saying I'll be there.'" maxlength="500"></textarea>
  <button id="generate-proposal-btn">Generate proposal</button>
  <div id="proposal-status"></div>
</div>
```

**JavaScript Logic:**
1. On click "Generate proposal":
   - Validate: command not empty, selected email exists
   - Set `state.isLoading = true`, show spinner
   - Call `POST /api/agent/run` with `{ command, selected_email_id }`
   - If response.agent_status === "PROPOSAL":
     - Store `state.proposal` = response.proposal
     - Store `state.agentProposalId` = response.agent_proposal_id
     - Show proposal card
     - Call `/api/agent/confirm` to get policy decision
   - If "NEEDS_CLARIFICATION":
     - Show clarification message
     - Ask user to refine command
   - If "AGENT_OUTPUT_INVALID":
     - Show error, let user retry

#### 2.4 Proposal Card (AI Proposal Layer)
```html
<div id="proposal-card" class="card proposal-card" style="display:none;">
  <div class="card-header">AI PROPOSAL</div>
  <div class="card-body">
    <div class="row">
      <span class="label">COMMAND:</span>
      <span id="proposal-command"></span>
    </div>
    <div class="row">
      <span class="label">INTERPRETED ACTION:</span>
      <span id="proposal-action"></span>
    </div>
  </div>
</div>
```

**Styling:**
- Background: `#EDF3F4` (proposal tint)
- Left border: `2px #AFC4CA` (proposal rule)
- Padding: `24px`
- Font: 15px/22px, `#667177` secondary ink

#### 2.5 Decision Card (Policy Layer)
```html
<div id="decision-card" class="card decision-card" style="display:none;">
  <div class="card-header">POLICY DECISION</div>
  <div class="card-body">
    <div id="decision-outcome" class="decision-badge"></div>
    <!-- ALLOW: green, GATED: amber, BLOCK: red -->
    <p id="decision-reason" class="decision-reason"></p>
    <p id="decision-policy" class="decision-policy"></p>
  </div>
</div>
```

**Decision Badges:**
- **ALLOW:** `background: #DDECE6`, `color: #0E6254`, `border: 1px #0E6254`
- **GATED:** `background: #F7EDD8`, `color: #98520A`, `border: 1px #98520A`
- **BLOCK:** `background: #F5E7E6`, `color: #7D2D32`, `border: 1px #7D2D32`

#### 2.6 Authorisation Section (Human Layer)
```html
<div id="authorisation" style="display:none;">
  <div class="auth-header">
    <h4>AUTHORISATION</h4>
    <span id="approval-countdown">Expires in 00:45</span>
  </div>
  <button id="approve-btn" class="btn btn-primary">Approve & Execute</button>
  <button id="reject-btn" class="btn btn-secondary">Reject</button>
  <div id="reject-reason-form" style="display:none;">
    <textarea id="reject-reason" placeholder="Optional reason for rejection" maxlength="500"></textarea>
    <button id="confirm-reject-btn">Confirm Rejection</button>
  </div>
</div>
```

**Button Styling:**
- **Approve & Execute:** `background: #0E6254` (evergreen), `color: white`, `font-weight: 600`, `padding: 16px`, `width: 100%`, `border-radius: 4px`
- **Reject:** `background: white`, `color: #182128` (ink), `border: 1px #182128`, `font-weight: 600`, `padding: 16px`, `width: 100%`, `border-radius: 4px`

**Countdown Logic:**
1. Parse `expires_at` from queue item
2. Calculate `remainingSeconds = (expires_at - now) / 1000`
3. Format as `MM:SS`
4. Update every 1 second
5. At 0 seconds: disable approve button, show "Approval expired"

**JavaScript Logic (Approve):**
1. On click "Approve & Execute":
   - Call `POST /api/queue/<id>/approve`
   - Show loading spinner
   - On success:
     - Show "Execution requested. Checking outcome…"
     - Poll `/api/queue/<id>` until status !== "APPROVED" (max 10 polls, 1s interval)
     - When resolved, refresh queue + audit
   - On error: show "Approval failed. [Retry]"

**JavaScript Logic (Reject):**
1. On click "Reject":
   - Show reason textarea
2. On "Confirm Rejection":
   - Call `POST /api/queue/<id>/reject` with `{ reason }`
   - Show success message
   - Clear proposal/decision cards
   - Refresh queue

#### 2.7 Queue Panel (Right pane or below)
```html
<div id="queue-panel">
  <h3>QUEUE</h3>
  <div id="queue-items"></div>
  <!-- Item template -->
  <div class="queue-item" data-queue-id="queue-uuid">
    <div class="queue-status-badge">PENDING</div>
    <div class="queue-action">email.reply</div>
    <div class="queue-expiry">Expires in 02:15</div>
  </div>
</div>
```

**JavaScript Logic:**
1. On mount + after approve/reject: fetch `/api/queue`
2. Render each item with status badge color:
   - PENDING: amber
   - APPROVED: green
   - REJECTED: red
   - EXECUTED: blue
3. On click item: load into main proposal/decision area

---

### SCREEN 3: AUDIT TRAIL

**URL:** `index.html?page=audit`

**Layout:**
```
┌─────────────────────────────────────────────────────────────────────┐
│ AUDIT TRAIL                                  [Refresh]               │
├─────────────────────────────────────────────────────────────────────┤
│ Summary: 5 total | 2 ALLOW | 2 GATED | 1 BLOCK | Approval rate: 67%│
├─────────────────────────────────────────────────────────────────────┤
│ Timestamp               Action                Outcome   Details      │
│ ─────────────────────────────────────────────────────────────────── │
│ 2026-06-22 10:41:12    Decision evaluated    GATED     [View]      │
│ 2026-06-22 10:41:18    Approval recorded     ALLOW     [View]      │
│ 2026-06-22 10:41:19    Execution resolved    COMPLETED [View]      │
└─────────────────────────────────────────────────────────────────────┘
```

**HTML:**
```html
<div id="audit-page" class="page">
  <h2>Audit Trail</h2>
  <div id="audit-summary"></div>
  <table id="audit-table">
    <thead>
      <tr>
        <th>Timestamp</th>
        <th>Action</th>
        <th>Outcome</th>
        <th>Details</th>
      </tr>
    </thead>
    <tbody id="audit-tbody"></tbody>
  </table>
</div>
```

**JavaScript Logic:**
1. On mount: fetch `/api/audit?limit=100` + `/api/audit/summary`
2. Render summary stats
3. Render table rows with semantic outcome colors (ALLOW=green, GATED=amber, BLOCK=red)
4. On click [View]: show detail panel with full event payload

---

### SCREEN 4: TRUST HISTORY

**URL:** `index.html?page=trust`

**Layout:**
```
┌─────────────────────────────────────────────────────────────────────┐
│ TRUST HISTORY                                                        │
├─────────────────────────────────────────────────────────────────────┤
│ Action type: [email.reply ▼]                                        │
├─────────────────────────────────────────────────────────────────────┤
│                    line graph (simple)                               │
│  Trust Score 100 |                                                   │
│              80 |      ╱╲                                            │
│              60 |     ╱  ╲                                           │
│              40 |────╱    ╲──                                        │
│              20 |          ╲                                         │
│               0 |___________╲___                                     │
│                  Jun 22 10am   12pm   2pm                            │
│                                                                      │
│ Tap/hover a point to see the underlying event                       │
└─────────────────────────────────────────────────────────────────────┘
```

**HTML:**
```html
<div id="trust-page" class="page">
  <h2>Trust History</h2>
  <div id="trust-selector">
    <label>Action type:</label>
    <select id="trust-action-type">
      <option value="">-- Select action --</option>
      <option value="email.reply">email.reply</option>
      <option value="email.send.external">email.send.external</option>
      <!-- etc -->
    </select>
  </div>
  <div id="trust-graph-container">
    <svg id="trust-graph" width="100%" height="300"></svg>
  </div>
</div>
```

**JavaScript Logic:**
1. On mount: populate action type dropdown from hardcoded list or derive from audit events
2. On select action: fetch `/api/trust/<action_type>/history`
3. Simple SVG line graph:
   - X-axis: timestamp (hours/days)
   - Y-axis: trust score (0–100)
   - Plot points from response.history
   - Draw line connecting points
4. On hover/tap point: show tooltip with timestamp + event description

---

### SCREEN 5: SETTINGS (Low Priority)

**URL:** `index.html?page=settings`

**Layout (Simple):**
```
┌─────────────────────────────────────────────────────────────────────┐
│ SETTINGS                                                             │
├─────────────────────────────────────────────────────────────────────┤
│ Policy Profile                                                       │
│ ☐ Strict   ☑ Balanced   ☐ Autonomous                               │
│ (Profile persistence not connected)                                 │
│                                                                     │
│ Gmail Connection                                                     │
│ Status: Connected (project.argus.242@gmail.com)                    │
│ [Test Connection]                                                   │
│                                                                     │
│ Demo Controls                                                        │
│ [Reset Demo State]                                                  │
└─────────────────────────────────────────────────────────────────────┘
```

**For now:** Just show placeholder text "Settings coming soon" or hardcode Balanced profile selection.

---

## STATE MANAGEMENT (js/state.js)

Manage all app state in a single object:

```javascript
const appState = {
  // Auth
  isAuthenticated: false,
  
  // Current page
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
  
  // Decision
  decision: null,
  decisionLoading: false,
  
  // Queue
  queueItems: [],
  queueLoading: false,
  
  // Audit
  auditEvents: [],
  auditSummary: null,
  auditLoading: false,
  
  // Trust
  trustHistory: [],
  selectedTrustActionType: null,
  trustLoading: false,
};

// Export functions to update state
export function setState(updates) {
  Object.assign(appState, updates);
  render(); // Re-render UI
}

export function getState() {
  return appState;
}
```

---

## API INTEGRATION (js/api.js)

Helper functions to call backend endpoints:

```javascript
const API_BASE = 'http://localhost:8081';

export async function fetchInbox(limit = 20) {
  const res = await fetch(`${API_BASE}/api/gmail/messages?limit=${limit}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function runAgent(command, selectedEmailId = null) {
  const res = await fetch(`${API_BASE}/api/agent/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ command, selected_email_id: selectedEmailId }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function confirmAgent(agentProposalId) {
  const res = await fetch(`${API_BASE}/api/agent/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agent_proposal_id: agentProposalId }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function fetchQueue() {
  const res = await fetch(`${API_BASE}/api/queue`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function approveQueue(queueId) {
  const res = await fetch(`${API_BASE}/api/queue/${queueId}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function rejectQueue(queueId, reason = '') {
  const res = await fetch(`${API_BASE}/api/queue/${queueId}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function fetchAudit(limit = 100) {
  const res = await fetch(`${API_BASE}/api/audit?limit=${limit}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function fetchAuditSummary() {
  const res = await fetch(`${API_BASE}/api/audit/summary`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function fetchTrustHistory(actionType) {
  const res = await fetch(`${API_BASE}/api/trust/${actionType}/history`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
```

---

## DESIGN TOKENS (CSS)

**File:** `css/style.css`

**Palette:**
```css
:root {
  --color-ink: #182128;
  --color-secondary-ink: #667177;
  --color-canvas: #F6F7F4;
  --color-surface: #FEFEFC;
  --color-slate-surface: #EEF1EE;
  --color-hairline: #D9DEDA;
  --color-strong-keyline: #293941;
  
  --color-evergreen: #0E6254;
  --color-evergreen-soft: #DDECE6;
  --color-amber: #98520A;
  --color-amber-soft: #F7EDD8;
  --color-oxblood: #7D2D32;
  --color-oxblood-soft: #F5E7E6;
  
  --color-proposal-tint: #EDF3F4;
  --color-proposal-rule: #AFC4CA;
  
  /* Typography */
  --font-sans: 'Libre Franklin', 'Segoe UI', Arial, sans-serif;
  --font-mono: 'IBM Plex Mono', ui-monospace, 'SFMono-Regular', Consolas, monospace;
  
  /* Spacing (8px grid) */
  --spacing-xs: 4px;
  --spacing-sm: 8px;
  --spacing-md: 16px;
  --spacing-lg: 24px;
  --spacing-xl: 32px;
  --spacing-xxl: 48px;
}
```

**Global Styles:**
```css
* {
  box-sizing: border-box;
}

body {
  background: var(--color-canvas);
  color: var(--color-ink);
  font-family: var(--font-sans);
  font-size: 15px;
  line-height: 1.5;
  margin: 0;
  padding: 0;
}

h1 { font-size: 28px; font-weight: 600; line-height: 1.2; }
h2 { font-size: 24px; font-weight: 600; line-height: 1.2; }
h3 { font-size: 20px; font-weight: 600; line-height: 1.4; }
h4 { font-size: 16px; font-weight: 600; }

.card {
  background: var(--color-surface);
  border: 1px solid var(--color-hairline);
  border-radius: 4px;
  padding: var(--spacing-lg);
  margin-bottom: var(--spacing-lg);
}

.card.proposal-card {
  background: var(--color-proposal-tint);
  border-left: 2px solid var(--color-proposal-rule);
  border-right: 1px solid var(--color-hairline);
  border-top: 1px solid var(--color-hairline);
  border-bottom: 1px solid var(--color-hairline);
}

.card.decision-card {
  border: 2px solid var(--color-strong-keyline);
}

.decision-badge {
  display: inline-block;
  padding: 8px 16px;
  font-weight: 600;
  text-transform: uppercase;
  border-radius: 4px;
  margin-bottom: var(--spacing-md);
}

.decision-badge.allow {
  background: var(--color-evergreen-soft);
  color: var(--color-evergreen);
  border: 1px solid var(--color-evergreen);
}

.decision-badge.gated {
  background: var(--color-amber-soft);
  color: var(--color-amber);
  border: 1px solid var(--color-amber);
}

.decision-badge.block {
  background: var(--color-oxblood-soft);
  color: var(--color-oxblood);
  border: 1px solid var(--color-oxblood);
}

button {
  font-family: var(--font-sans);
  font-size: 15px;
  font-weight: 600;
  padding: 12px 24px;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  transition: opacity 0.2s;
}

button:hover { opacity: 0.8; }
button:disabled { opacity: 0.5; cursor: not-allowed; }

.btn-primary {
  background: var(--color-evergreen);
  color: white;
  width: 100%;
  padding: 16px;
  min-height: 48px;
}

.btn-secondary {
  background: var(--color-surface);
  color: var(--color-ink);
  border: 1px solid var(--color-ink);
  width: 100%;
  padding: 16px;
  min-height: 48px;
}

.label {
  font-weight: 600;
  font-size: 12px;
  text-transform: uppercase;
  color: var(--color-secondary-ink);
  font-family: var(--font-mono);
}

/* Responsive */
@media (max-width: 768px) {
  h1 { font-size: 24px; }
  h2 { font-size: 20px; }
}

@media (max-width: 480px) {
  h1 { font-size: 20px; }
  body { font-size: 14px; }
}
```

---

## ERROR HANDLING

All errors follow a pattern:

```javascript
async function safeAPICall(fn) {
  try {
    return await fn();
  } catch (error) {
    console.error(error);
    return { error: true, message: error.message };
  }
}

// Usage
const inbox = await safeAPICall(() => fetchInbox());
if (inbox.error) {
  setState({ inboxError: 'Failed to load inbox. [Retry]' });
}
```

**Common errors to handle:**
- Gmail not connected
- Network timeout
- Backend 500
- Proposal NEEDS_CLARIFICATION
- Approval expired
- Queue item not found
- Trust history empty (show "No history" message)

---

## LOADING STATES

**Skeleton loaders for async content:**

```html
<div class="skeleton-row">
  <div class="skeleton-text"></div>
</div>
```

```css
@keyframes shimmer {
  0% { background-position: -100% 0; }
  100% { background-position: 100% 0; }
}

.skeleton-text {
  height: 16px;
  background: linear-gradient(90deg, #ddd 0%, #e0e0e0 50%, #ddd 100%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 4px;
  margin-bottom: 8px;
}
```

Use skeletons for:
- Inbox list (while loading emails)
- Proposal card (while GPT-4o interprets)
- Decision card (while policy engine decides)
- Audit table (while loading events)

**Spinners for in-progress actions:**

```html
<div class="spinner"></div>
```

```css
@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.spinner {
  width: 20px;
  height: 20px;
  border: 2px solid var(--color-hairline);
  border-top-color: var(--color-evergreen);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
```

---

## RESPONSIVE DESIGN

**Desktop (≥1024px):**
- Three-column layout (inbox | workbench | queue)
- All content visible at once

**Tablet (481–768px):**
- Inbox collapses to modal/drawer
- Workbench full width
- Queue below workbench

**Mobile (≤480px):**
- Single column
- Tab navigation between screens
- Inbox/queue/audit/trust as separate full-screen views
- Proposal/decision cards stack vertically
- Buttons full-width

---

## TESTING CHECKLIST

Before committing, verify:

- [ ] **Login** works with PROJECT_ARGUS / ARGUS_DEMO
- [ ] **Inbox loads** (GET /api/gmail/messages)
- [ ] **Email selection** highlights border, shows in context
- [ ] **Command input** accepts text, button generates proposal
- [ ] **Proposal card** displays AI interpretation correctly
- [ ] **Decision card** shows ALLOW/GATED/BLOCK with correct color/reason
- [ ] **Approve button** sends /api/queue/<id>/approve, polls for completion
- [ ] **Reject button** shows reason textarea, sends /api/queue/<id>/reject
- [ ] **Approval countdown** counts down from expires_at, disables at 0
- [ ] **Audit Trail** loads and displays events with correct outcome colors
- [ ] **Trust History** loads history for selected action type, draws line graph
- [ ] **Settings** shows placeholder
- [ ] **Error states** display gracefully (no red boxes, clear messaging)
- [ ] **Loading states** show skeletons/spinners during async calls
- [ ] **Mobile responsive** — test on phone (480px width)
- [ ] **No console errors** (F12 DevTools → Console)
- [ ] **All links work** — nav between Workbench/Audit/Trust/Settings
- [ ] **Session persists** across page refresh (sessionStorage)
- [ ] **Session clears** on logout or window close

---

## BUILD INSTRUCTIONS

1. **Create folder structure:**
   ```bash
   cd PROJECT-ARGUS
   mkdir -p frontend/css frontend/js
   ```

2. **Create files:**
   - `frontend/index.html`
   - `frontend/css/style.css`
   - `frontend/js/app.js`
   - `frontend/js/api.js`
   - `frontend/js/state.js`

3. **Implement:**
   - Start with index.html (login + workbench shell)
   - Add css/style.css (colors, typography, layout)
   - Add js/state.js (state management)
   - Add js/api.js (backend API calls)
   - Add js/app.js (main orchestration + event handlers)

4. **Test locally:**
   ```bash
   # Terminal 1: Backend
   cd PROJECT-ARGUS
   python app.py
   
   # Terminal 2: Open frontend
   open frontend/index.html
   # or drag into browser
   ```

5. **Verify on testing checklist** above

6. **Commit & push:**
   ```bash
   git add frontend/
   git commit -m "Add ARGUS frontend (HTML/CSS/JS)"
   git push origin main
   ```

---

## BUILD-ONLY RULE

**Do NOT:**
- Write tests
- Commit individually (one commit: all frontend files)
- Add next-step tasks or planning
- Go beyond the scope above (no animations, no frameworks, no extras)

**Do:**
- Build clean, readable code
- Use semantic HTML
- Follow the design tokens exactly
- Verify the testing checklist
- Push when done

---

## QUESTIONS?

If anything is unclear, ask Kayden before starting. Do not guess or deviate from this spec.

**Ready to build.** Go.
