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
| 2 | Permission layer + conflict resolution | NOT STARTED |
| 3 | Approval queue | NOT STARTED |
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
- **This is the visual centrepiece — make it look good**

### If we have time (frontend)
- Visual timeline of trust evolution per action type (line graph or bar chart)
- Trust replay mode — step through any past decision and see each modifier applied in order
- Demo amplification mode — exaggerate visual trust changes for the 3-min demo without changing internal values

### Needs Phase 5–6 done (Gmail + Calendar)
- Email draft preview (before/after)
- Calendar event display

### Needs Phase 7 done (audit trail)
- Audit log table — scrollable, show decision, action type, timestamp, reason code

### Needs Phase 9 done (demo mode)
- Demo reset button — calls `POST /demo/reset`, reseeds the inbox for a clean 3-min demo run

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
