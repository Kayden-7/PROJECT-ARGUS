# ARGUS — Handoff Log

## How to use this file
Update every time you push and pass work to your teammate.
Pull first. Push when done. WhatsApp when switching.

---

## Current handoff

**Last updated by:** Kayden
**Status:** Phase 5 Parts 1–3 DONE — Gmail connected + crash-safe execution + message templates. 688 tests 100% passing.
**Next person needs to:** Pull, run `python app.py`, build frontend against existing endpoints
**Files changed:** app.py, argus/gmail_client.py, argus/executor.py, argus/db.py, requirements.txt, tests/*
**Notes:** Run `pip install -r requirements.txt` then `python app.py` (port 8081). Run `python run_tests.py` to verify all green. Gmail OAuth: visit `/api/gmail/connect` once to authorise the demo inbox.

---

## Phase status

| Phase | What | Status |
|---|---|---|
| 1 | Setup — Flask skeleton, SQLite, kernel | DONE |
| 2 | Permission layer + conflict resolution | DONE |
| 3 | Approval queue | DONE |
| 4 | Trust ledger | DONE |
| 5 | Gmail integration | Part 1+2+3 DONE (connect + execution + templates); safety filter next |
| 6 | Calendar integration | NOT STARTED |
| 7 | Audit trail | NOT STARTED |
| 8 | Fail-safes | NOT STARTED |
| 9 | Demo mode | NOT STARTED |
| 10 | ElevenLabs voice | NOT STARTED |

---

## Frontend build guide (for friend)

Pull the repo. Run the backend first so you have live endpoints to test against.

### ⚠ READ FIRST — Trust psychology & tone (shapes EVERYTHING below)

ARGUS is **a verifiable delegation control system with human-readable trust memory — NOT a human-like assistant.** Judges are scoring trust. The UI must feel like a system you can *audit and understand*, not a chatbot that's trying to be liked.

**Tone:** calm, clinical, precise. Like a cockpit instrument or a compliance dashboard. Never bubbly, never emotional.

**DO:**
- Show *why* for every decision — a collapsible "Why this was allowed / blocked / gated" reasoning trace (collapsed by default, expandable).
- On every BLOCK, prominently show the **containment message**: e.g. "Action blocked before execution. No external effects occurred." This reassurance matters more than the block itself.
- Show a **recovery state** when trust recently dropped and is climbing back: labels like *Recovering / Stabilizing / Rebuilding Confidence*. This shows the system *manages* recovery, not just scores it. (Backend will expose this field.)
- Show the **explanation fingerprint** — one plain sentence per decision, e.g. "Allowed: stable email.send.external history (12/12 success)." (Backend will provide this.)
- Lean on determinism as a selling point: same input → same outcome, every time.

**DON'T (these read as fake/superficial to judges — automatic credibility loss):**
- ❌ No gamified XP-style trust bars or "level up" framing. Trust is a serious signal, not a game score.
- ❌ No fake emotion or anthropomorphism ("ARGUS is learning from its mistakes like a human"). Cut it.
- ❌ No inflated confidence language ("Highly confident!") unless it's mathematically grounded in success history.
- ❌ No "magic" trust score with no visible reasoning. If the user can't see why, they won't trust it.

The trust gauge is still the visual centrepiece (see Phase 4 section) — but present it as a **trust memory / audit readout**, not a game bar.

### Start immediately — no backend needed
- Overall layout and dark theme (bg `#080C12`, accent `#E8B84B`, monospace font)
- Navigation between 3 panels: Agent Console, Approval Queue, Audit Trail
- Static component skeletons (cards, tables, buttons)
- Permission profile switcher (Strict / Balanced / Autonomous) — visual only for now
- Emergency stop button — visual only for now

### Login page (no backend needed — hardcoded)
- Simple login screen before the dashboard
- Username: `admin` / Password: `argus2026` (hardcoded, checked client-side for demo only)
- On success: set a session flag and redirect to dashboard
- Don't build real auth — this is demo polish only

### Integrations page (frontend placeholder — no backend needed yet)
- A settings/integrations panel where users "connect" their Gmail and Google Calendar
- For the demo, show two cards:
  - **Gmail** — "Connect Gmail" button, shows `project.argus.242@gmail.com` as connected (hardcoded)
  - **Google Calendar** — "Connect Calendar" button, shows as connected (hardcoded)
- These are visual placeholders — the real OAuth is handled by the backend in Phase 5/6
- Label them clearly: "Connected via Google OAuth" with a green status dot
- This makes the demo feel like a real product without requiring live OAuth in the frontend

### Needs Phase 2 done (decision engine)
- Decision display card — shows `decision` (ALLOW / GATED / BLOCK), `decision_source`, `failure_reason_code`, `terminated_at`, `trace` list

### Needs Phase 3 done (approval queue)
- Approval queue panel — list of pending GATED actions
- Approve / Reject buttons per card
- Expiry countdown timer (300s)

### Needs Phase 4 done (trust ledger)
- Trust gauge per action type — shows label + contextual description:
  - 0–20: Untrusted (Requires Oversight)
  - 21–40: Low Trust (Learning Phase)
  - 41–60: Developing (Generally Reliable)
  - 61–80: Trusted (Safe to Delegate)
  - 81–100: Highly Reliable (Autonomous Range)
- Before/after trust delta snapshot per decision — e.g. `email.send.external: 52 → 59 (+7)`
- Decision narrative — 1–2 line plain English explanation of why trust changed
- Overall trust modifier display (0.8–1.2 range)
- **Trust ceiling indicator** — show the profile's ceiling (e.g. Balanced caps at 85). Visual marker on the gauge so user sees the ceiling before it's hit.
- **Trust decay indicator** — if an action type hasn't been used in a while, show a small warning icon that trust is drifting back toward baseline (40).
- **This is the visual centrepiece — make it look good**

### If we have time (frontend)
- Demo amplification mode — exaggerate visual trust changes for the 3-min demo without changing internal values

### Needs Phase 9 done (GPT-4o agent) — the demo centerpiece
The natural-language front door. Endpoints LIVE: `POST /api/agent/run` ({command}) returns a **proposal, NOT an execution**; `POST /api/agent/confirm` ({agent_proposal_id}) routes it through policy and returns the decision; `POST /demo/reset` (only when server in demo mode).
- **The trust moment:** show the user's command beside **"here's what I understood"** (the proposal) — labelled **"GPT-4o proposal — not a permission decision."** Then the separate **"ARGUS decision — computed from policy, trust, and safety rules."** Make the two-owner split unmistakable.
- Flow to render: `command → GPT-4o proposal → user confirms interpretation → ARGUS decides → execute/hold`. A compact, expandable **decision trace** ("✓ proposal schema valid · ✓ policy evaluated · → approval required: external forward").
- Proposal must be **editable / cancellable** before confirm. Show the recipient and body clearly so the user can catch a misread.
- **Agent states are NOT policy outcomes** — render them distinctly: `AGENT_NEEDS_CLARIFICATION` ("More detail required — no action proposed"), `AGENT_UNAVAILABLE`, `AGENT_OUTPUT_INVALID`. Never show these as a BLOCK/GATED.
- **Anti-patterns:** no "Done" before a decision exists, no green success on a proposal, no "the AI decided", no "AI confidence: 94%", no "ARGUS knows what you meant". Raw JSON only in a debug/judge view, not the default.
- Judge-facing line: **"GPT-4o never acts and never decides permission. It converts a request into an inspectable proposal; ARGUS validates, decides, and controls execution."**

### Needs Phase 5 Part 4 done (safety filter) — framing matters a LOT here
ARGUS now downgrades certain actions to "always needs your approval" **regardless of how much trust the AI has earned** (delete, external forward, send to a recipient whose domain isn't on the trusted list, anything with Bcc). The UI framing is the whole point:
- Frame these as **delegation boundaries, not limitations or low confidence.** ARGUS has *defined* autonomy, not unlimited autonomy.
- The key trust phrase to show: **"requires your approval regardless of trust level."** This stops the user reading the hold as "the AI isn't trusted enough."
- **Distinguish two kinds of hold:** trust-based gate ("not yet earned") vs safety-boundary gate ("even a trusted delegate needs you for this"). The decision carries `failure_reason_code` starting with `SAFETY_DOWNGRADE_*` and `candidate_decision: "ALLOW"` for the safety kind.
- Reason codes to render: `SAFETY_DOWNGRADE_DELETE`, `SAFETY_DOWNGRADE_EXTERNAL_FORWARD`, `SAFETY_DOWNGRADE_UNRECOGNIZED_DOMAIN`, `SAFETY_DOWNGRADE_BCC`, `SAFETY_DOWNGRADE_MALFORMED_RECIPIENT`, `SAFETY_DOWNGRADE_NEW_RECIPIENTS`.
- For the unrecognized-domain case, say: "Recipient domain is not on your trusted-domain list" (NOT "unknown/new domain" — ARGUS has no history model).
- Execution/error states carry specific reasons too: `RECIPIENT_MISMATCH` (draft changed before send), `UNKNOWN_DELIVERY_STATE` (Gmail uncertain — "Delivery status unresolved, not resent"). Use precise categories — **Approval required / Paused safely / Delivery status unresolved / Retry eligible** — and reserve "failed" for a confirmed permanent failure.
- **Anti-patterns:** no "ARGUS protected you!" banners for routine gating, no red danger styling, no anthropomorphism ("ARGUS felt unsure"), no confidence score beside a safety hold, never hide an ambiguous Gmail outcome behind a green success.

### Needs Phase 5 done (Gmail execution) — endpoints now live
- **Execution status panel** — `GET /api/executions` lists every execution with its state: `DRAFT_PENDING → DRAFT_READY → SENDING → COMPLETED`, or `MANUAL_REVIEW`. Show as a small pipeline/status chip per item.
- **MANUAL_REVIEW surface (important)** — when an execution hits `MANUAL_REVIEW`, surface it prominently with its `status_reason` (e.g. "Crashed during send — verify in Gmail Sent folder"). This is ARGUS's core trust behaviour: *on any uncertainty it stops and asks the human, never silently double-sends or loses an email.* Make this visible and reassuring, not alarming.
- `POST /api/executions/tick` — manual "process now" button if you want one; the queue poll already drives execution.
- Email draft preview (before/after)
- Calendar event display
- **Message template editor** — settings panel to define per-contact/per-action style rules. Endpoints are LIVE: `GET/POST/DELETE /api/templates`, and `GET /api/templates/match?contact=&action_type=` (returns the matched template snapshot, its scope, or the conservative default). Form collects only structured fields — tone, formality, length preset, greeting, sign-off, optional "avoid phrases" chips, enabled toggle. **No free-form instructions textarea** (deliberate — it's an injection surface).
  - Frame it as **"communication boundaries," NOT an "AI humanizer."** Clinical, not charming.
  - **"Applied writing rules" panel** beside the draft (Phase 9): show which template matched, its scope ("Contact + Reply"), and the rules. Distinguish **Applied** (rule passed to the model) vs **Checked** (validator confirmed: "3 sentences ✓, no exclamation marks ✓") vs **Not mechanically verified** (subjective tone). It's a verifier readout, not an AI claim.
  - **"Why this template?"** one-liner: "Selected because this is a reply to [contact], matching your contact-specific reply template."
  - When no template matches, show: "No saved template matched. Conservative default applied."
  - Anti-patterns to avoid: no "writes exactly like you" claims, no humanness score, no gamified voice-matching.

### Needs Phase 7 done (audit trail)
- Audit log table — scrollable, show decision, action type, timestamp, reason code
- **Monthly/periodic summary report view** — a dedicated panel or modal showing a digest for a selected time period: total actions, FREE vs GATED breakdown, approval rate, rejection rate, any prime rule triggers, any hard stop events, trust trajectory summary. This is the user's "health check" to verify ARGUS behaved correctly.
- **Trust replay mode** — click any past decision in the audit log and see a step-by-step breakdown of every modifier that was applied (inertia weight, overall modifier, severity tier, contact relax amount, final delta). Shows the user exactly why the trust score moved the way it did.
- **Visual trust timeline** — line graph of trust score over time per action type. User selects an action from a dropdown, graph renders from trust history. Shows trust trajectory at a glance.

### Needs Phase 8 done (fail-safes)
- **Private Contact List management** — a settings panel where the user can add contacts whose emails ARGUS will never process or make decisions on. Add/remove contacts. When a proposal involves a private contact, ARGUS blocks it before the AI even sees the email content. Label clearly: "ARGUS will not read or act on emails involving these people."

### Needs Phase 9 done (demo mode)
- Demo reset button — calls `POST /demo/reset`, reseeds the inbox for a clean 3-min demo run
- **GPT-4o prompt framework indicator** — small UI element showing the active prompt framework version (e.g. "Framework v1.2") so the user knows which set of constraints GPT-4o is operating under. Useful for the demo to show the system is structured, not free-form.
- **Re-consent banner** — if the monthly re-consent is overdue, show a banner on the dashboard: "ARGUS activity review due. Review the monthly summary and confirm you're happy with how it's been operating." Confirm button calls `POST /api/consent`. If not confirmed within 3 days, the dashboard shows a warning that the profile has been downgraded to Balanced automatically.

---

## API endpoints (wire frontend against these)

| Method | Endpoint | What it does |
|---|---|---|
| GET | /health | Health check |
| POST | /api/agent/run | Send user command → get proposal + decision |
| POST | /api/propose | Send proposal JSON → get decision |
| GET | /api/queue | List pending approvals |
| POST | /api/queue/<id>/approve | Approve a queued action |
| POST | /api/queue/<id>/reject | Reject a queued action |
| GET | /api/trust/<action_type> | Get trust score for one action type |
| GET | /api/audit | Get audit log (last 100 entries) |
| POST | /api/emergency/stop | Trigger SYSTEM_HARD_STOP |
| POST | /api/emergency/resume | Clear SYSTEM_HARD_STOP |
| POST | /api/profile | Switch permission profile |
| POST | /demo/reset | Reseed demo inbox |
