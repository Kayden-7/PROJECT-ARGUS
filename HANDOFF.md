# ARGUS — Handoff Log

## How to use this file
Update every time you push and pass work to your teammate.
Pull first. Push when done. WhatsApp when switching.

---

## Current handoff

**Last updated by:** Kayden
**Status:** Phase 1 DONE — /health returns 200, DB initialises clean
**Next person needs to:** Pull, run app on port 8081, start Phase 2
**Files changed:** app.py (port → 8081)
**Notes:** Run `pip install -r requirements.txt` then `python app.py`, hit http://127.0.0.1:8081/health

---

## Phase status

| Phase | What | Status |
|---|---|---|
| 1 | Setup — Flask skeleton, SQLite, kernel | DONE |
| 2 | Permission layer + conflict resolution | DONE |
| 3 | Approval queue | DONE |
| 4 | Trust ledger | NOT STARTED |
| 5 | Gmail integration | NOT STARTED |
| 6 | Calendar integration | NOT STARTED |
| 7 | Audit trail | NOT STARTED |
| 8 | Fail-safes | NOT STARTED |
| 9 | Demo mode | NOT STARTED |
| 10 | ElevenLabs voice | NOT STARTED |

---

## Frontend build guide (for friend)

Pull the repo. Run the backend first so you have live endpoints to test against.

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

### Needs Phase 5–6 done (Gmail + Calendar)
- Email draft preview (before/after)
- Calendar event display
- **Message template editor** — a settings panel where the user defines per-contact rules: tone, length, any phrases to avoid. Example: "Emails to boss → max 3 sentences, professional, no exclamation marks." Show the active template on the approval card when a draft is queued so the user can verify the AI followed it.

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
