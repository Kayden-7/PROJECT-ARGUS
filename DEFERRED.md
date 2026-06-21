# ARGUS — Deferred Items

Things parked during competition build. Come back to these after June 25.

---

## Architecture & Design

### Idempotency on POST /api/propose
Currently: same proposal submitted twice = two separate queue items.
Deferred: decide whether duplicate detection is needed. Would require hashing proposal content and checking against recent queue entries within a time window. Risk: retry storms from frontend could create duplicate approvals.

### Policy Gate BLOCK trust philosophy
Moved to active build — Phase 4.
Policy gate BLOCKs will carry a small negative trust signal. Repeated blocking = AI overreaching.

### Trust event failure after ALLOW
Moved to active build — Phase 4.
Trust write failure after ALLOW triggers a compensating reconciliation event, not silent skip.

### MANUAL_REVIEW full lifecycle
Moved to active build — Phase 8.
Timeout after MANUAL_REVIEW_TIMEOUT (600s), escalation path, dashboard alert.

---

## Endpoints & API

### GET /api/queue/history
Returns resolved items (APPROVED, REJECTED, EXPIRED, EXECUTED, CANCELLED).
Deferred: not needed for demo, covered partially by Phase 7 audit trail.

### Duplicate proposal detection
Moved to active build — Phase 8.
Hash proposal content, reject duplicates within a time window.

### Oversized rejection reason validation
Moved to active build — Phase 8.
500 char cap on rejection reason string.

---

## Trust System

### Trust replay mode
Moved to active build — Phase 7.
Step through any past decision and see each modifier applied in order.

### Visual trust timeline
Moved to active build — Phase 7.
Line graph of trust evolution per action type. Depends on trust_events table (Phase 4).

### Variance penalty layer (Phase 4 extension — if time)
Track variance of success/failure outcomes per action type. High variance = erratic AI = slower trust ceiling growth rate. Consistent agents scale faster. Requires tracking outcome spread across trust_events.

### Challenge expectation signal (Phase 4 extension — if time)
If AI only executes low-risk/TRIVIAL actions, trust grows slower. Trust requires exposure to meaningful risk to be valid. Requires tracking severity distribution of executed actions.

### Behavioral predictability score (Phase 4 extension — if time)
Derived metric per action type — variance of outcomes under similar inputs. Used as a modifier to trust growth rate (not trust score itself). Most experimental of the three — needs concrete definition before building.

### Demo amplification mode
Exaggerate visual trust changes for the 3-min demo without changing internal values.
Deferred: useful for demos, low priority until core is done.

---

## Security & Production

### Real authentication
Currently: hardcoded admin/argus2026 login (frontend only).
Deferred: proper Flask session auth with hashed passwords when running as a real product.

### Full advisory locking
Currently: SQLite conditional UPDATEs handle race conditions for single-user demo.
Deferred: proper row-level locking or optimistic concurrency for multi-user deployment.

### Rate limiting invalid transitions
Moved to active build — Phase 8.
Invalid state transition attempts are rate-limited to prevent retry storms.

### Multi-user support
Currently: single user (Kayden) owns all permissions and approvals.
Deferred: per-user trust ledgers, permission profiles, and approval queues.

---

## Features

### Message templates
Moved to active build — Phase 5.
Per-contact tone and length rules stored in DB. GPT-4o reads matching template before drafting.

### ElevenLabs voice (Phase 10)
Voice narration of ARGUS decisions.
Note: ElevenLabs access arrives June 24 — one day before deadline. Build only if everything else is done.

---

## Post-Competition Vision

Run ARGUS as a Claude-native tool (not a website). 
- Replace Flask frontend with Claude conversation interface
- ARGUS policy engine and trust ledger remain as Python backend
- Claude handles the user-facing conversation and proposal parsing (replacing GPT-4o)
- Approval queue becomes a Claude tool call
- Audit trail readable directly in conversation
