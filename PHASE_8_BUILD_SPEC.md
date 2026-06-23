# PHASE 8 — FAIL-SAFES: BUILD SPEC (single source of truth)

Locked after a 6-round brainstorm → stress-test → re-validation chain (GPT-5.5). This is the only Phase 8 design doc; all scratch iteration prompts were discarded. Build from this.

**Environment:** single Flask app, single SQLite DB (`instance/argus.db`, WAL), single owner. All local state changes use SQLite transactions (`BEGIN IMMEDIATE`). No distributed locking. Executor already fences with `owner_token` (Phase 5).

---

## THE 7 CONTROLS

1. Emergency stop (hard stop) + epoch
2. Business-action rate limiting (10/hour)
3. Private-contact protection
4. MANUAL_REVIEW timeout (lazy, on read)
5. Duplicate proposal detection (exact-canonical, 60s window)
6. Rejection-reason cap (≤500 chars)
7. Invalid-transition rate limiting (5 invalid/60s → lock)

---

## TWO ORTHOGONAL COUNTERS (core invariant machinery)

- **`HARD_STOP_EPOCH`** — GLOBAL (`system_state`). Bumped when hard stop is enabled. Stales every outstanding approval at once.
- **`approval_queue.approval_generation`** — PER ITEM. Incremented on every `APPROVE`. Gives each (re-)approval a unique execution identity so superseded executions never collide with fresh ones.

---

## PRECEDENCE (one shared admission function — no per-endpoint reordering)

```
1. SYSTEM_HARD_STOP
2. Private-contact check (before any GPT exposure)
3. Request/schema validation
4. Duplicate suppression  ┐ ONE atomic admission txn
5. User rate limit        ┘
6. Policy engine (existing)
7. Safety filter (existing, one-way ALLOW→GATED)
8. Queue transition controls (timeout, invalid-transition lock)
9. Executor preflight: hard-stop recheck + epoch match + private-contact recheck
10. Gmail execution / Phase 5 reconciliation
11. MANUAL_REVIEW timeout escalation (lazy, on read)
```

Interaction rules: hard stop wins; private-contact wins over a stale ALLOW; duplicate before rate limit (dup doesn't consume quota); invalid-transition limiter must not block executor recovery transitions; timeout-vs-approval first-durable-transition wins; record the primary enforcing reason only.

---

## STATE MACHINES

**`approval_queue.status`** — existing `PENDING, APPROVED, REJECTED, EXPIRED, MANUAL_REVIEW, EXECUTED, CANCELLED` + new `HELD, MANUAL_REVIEW_TIMEOUT, TRANSITION_LOCKED`.

**`pending_executions.status`** — existing `DRAFT_PENDING, DRAFT_READY, SENDING, COMPLETED, MANUAL_REVIEW, FAILED` + new `HELD, SUPERSEDED`.

All transitions are CAS: `UPDATE ... WHERE id=? AND status=? AND version=?` (or status+generation for executions). First committed transition wins; loser gets `409`.

---

## CONTROL 1 — EMERGENCY STOP + EPOCH

- `system_state` rows: `SYSTEM_HARD_STOP` (0/1), `HARD_STOP_EPOCH` (int, start 0). One authoritative row per key (PK). Columns `updated_at, updated_by, reason(≤500)`.
- **Enable** (1 txn): `SYSTEM_HARD_STOP=1`, `HARD_STOP_EPOCH+=1`, audit `SYSTEM_HARD_STOP_ENABLED`.
- **Disable** (1 txn): `SYSTEM_HARD_STOP=0`, epoch unchanged, audit `SYSTEM_HARD_STOP_DISABLED`.
- Checked at: kernel entry, every queue/approval transition, executor preflight.
- **Executor preflight** (after claiming `owner_token`, before Gmail): if `is_hard_stop()` OR `execution.approval_epoch != current HARD_STOP_EPOCH` OR private-contact hit → transition `SENDING → HELD` (fenced), do NOT send. Honest boundary: cannot recall a Gmail request already dispatched.
- **Stale-APPROVED materialization (lazy):** on queue read/action, CAS `APPROVED → HELD WHERE approval_epoch != current HARD_STOP_EPOCH`, audit `HELD_STALE_EPOCH`. Makes held items reopenable without an executor run.
- D1: held items NEVER auto-execute on resume — must be reopened → re-approved.

---

## CONTROL 2 — RATE LIMITING

- `rate_limits(user_id, window_started_at, action_count, updated_at)` PK `(user_id, window_started_at)`. Half-open `[start, end)` windows, DB-UTC only, never client time.
- 10 business actions / rolling 60-min window. On exceed: reject (`RATE_LIMIT_EXCEEDED`), no queue item, return retry timestamp. NOT MANUAL_REVIEW.
- Reject invalid config at save: `window<=0`, negative limits, overflow.
- Separate category/counter from invalid-transition abuse (Control 7).

---

## CONTROL 3 — PRIVATE-CONTACT PROTECTION

- `private_contacts(id, normalized_email UNIQUE, display_label, enabled DEFAULT 1, created_at, updated_at)`.
- Match = **exact normalized address only** (lowercased full address). No `+tag` strip, no name match. Applies to BOTH selected-email source AND outgoing recipient/forward target.
- Checked: after grounding, before any GPT exposure (eligibility gate) AND again at executor preflight (live re-check; newly-added contact blocks a pending send → `EXECUTOR_BLOCKED_PRIVATE_CONTACT`).
- On hit: do not send content to GPT, no proposal, no queue. Reason `PRIVATE_CONTACT_PROTECTED`, redacted contact ref in audit.
- List mutations are owner-only + audited atomically.

---

## CONTROL 4 — MANUAL_REVIEW TIMEOUT (lazy)

- `approval_queue` adds `manual_review_generation`, `manual_review_started_at`.
- On entry to `MANUAL_REVIEW`: `manual_review_generation+=1`, set `manual_review_started_at=db_utc`.
- Lazy on read/action: CAS `MANUAL_REVIEW → MANUAL_REVIEW_TIMEOUT WHERE now-manual_review_started_at>600`, idempotent audit keyed `(queue_id, manual_review_generation)`.
- A `MANUAL_REVIEW_TIMEOUT` item CANNOT be approved directly (`409`); must go through reopen.
- Never decrement generation; clear timestamp on exit.

---

## CONTROL 5 — DUPLICATE DETECTION (exact canonical)

- `proposal_dedup(proposal_hash, user_id, proposal_id, created_at, expires_at)` PK `(user_id, proposal_hash)`.
- `proposal_hash` = SHA-256 of stable-key-order canonical JSON of: `action_type`, role-qualified recipients `{role, normalized_address}` (role∈to/cc/bcc, sorted), normalized subject (trim+collapse ws), canonical body/intent, `source_ref` (= `selected_email_id` else `thread_id` else null). EXCLUDES timestamps, IDs, UI fields, model metadata, policy/trust outcome.
- Exact-canonical only; NOT semantic. Document in UI + code comment.
- Within 60s window: return existing proposal / `DUPLICATE_SUPPRESSED`, do NOT consume rate-limit quota. Expiry handled atomically in the insert/claim (no read-then-revive gap).

---

## CONTROL 6 — REJECTION-REASON CAP

- Validate length ≤500 in code BEFORE write; `CHECK(length(status_reason)<=500)` on `approval_queue` + `pending_executions` as backstop.
- Specific code `REJECTION_REASON_TOO_LONG`, never generic DB error. Never silent-truncate.
- Applies to ALL reason-like fields: rejection reason, reopen reason, timeout/manual notes, settings-change reason.

---

## CONTROL 7 — INVALID-TRANSITION RATE LIMITING

- `queue_transition_attempts(id, queue_id, attempted_from, attempted_to, valid, created_at)`.
- Invalid transition → always hard-reject immediately (`INVALID_TRANSITION_REJECTED`), record attempt.
- 5 invalid attempts / 60s on same item AND item in `{PENDING, MANUAL_REVIEW, MANUAL_REVIEW_TIMEOUT}` → CAS to `TRANSITION_LOCKED`, set `transition_lock_reason`, `transition_locked_at`, audit `QUEUE_TRANSITION_LOCKED`.
- NEVER lockable from `APPROVED, EXECUTED, REJECTED, EXPIRED, CANCELLED, HELD` or claimed-execution items (prevents terminal resurrection).
- `status='TRANSITION_LOCKED'` is sole authority — NO separate `transition_lock_flag`.

---

## R-REOPEN — unified owner-only recovery (serves HELD / MANUAL_REVIEW_TIMEOUT / TRANSITION_LOCKED)

`POST /api/queue/<id>/reopen` — owner-only, CSRF-protected, server-derived identity, reason required (≤500, pre-validated).

In ONE transaction:
1. **Fence A — claim-conditional supersede:**
   ```sql
   UPDATE pending_executions SET status='SUPERSEDED'
    WHERE approval_id=? AND approval_generation=?
      AND status IN ('DRAFT_PENDING','DRAFT_READY','HELD') AND owner_token IS NULL;
   ```
   - If linked execution is `SENDING` or `MANUAL_REVIEW` (ambiguous delivery) → **rollback, return `409 EXECUTION_OUTCOME_UNRESOLVED`**, route to Phase 5 resolver. NEVER supersede an ambiguous-delivery execution.
   - If the guarded UPDATE affects 0 rows (executor claimed it mid-reopen) → rollback, `409 EXECUTION_OUTCOME_UNRESOLVED`.
2. Only if supersede succeeded (or no linked execution exists): CAS queue `{HELD|MANUAL_REVIEW_TIMEOUT|TRANSITION_LOCKED} → PENDING`, clear lock fields, audit `QUEUE_REOPENED`.
3. Subsequent fresh approval increments `approval_generation`, creates a NEW execution (new generation) — no collision with the `SUPERSEDED` row.

**Invariant:** `SUPERSEDED` is reserved strictly for executions that provably never dispatched to Gmail. The only path to a second send for one queue item is a Phase-5-confirmed `FAILED` + explicit fresh approval. Reopen alone can never double-send.

---

## EXECUTOR / PHASE 5 BRIDGE

- Executor writes are status+generation fenced (a superseded worker can't revive its row; only persists `DRAFT_READY` if row still in expected status/generation).
- **Fence B — FAILED→queue bridge:** when Phase 5 resolves `MANUAL_REVIEW → FAILED` (proven unsent), the SAME txn moves the linked queue item to `HELD` (reason `EXECUTION_PROVEN_UNSENT`). Only then is normal reopen/re-approval allowed. No generic "reopen from MANUAL_REVIEW" exception.
- Resolver proves sent → execution `COMPLETED`, queue `EXECUTED` (generation-aware reconciliation may set `EXECUTED` even if a hard-stop read briefly showed `HELD`).

---

## ATOMIC APPROVAL TRANSACTION

`BEGIN IMMEDIATE`:
1. assert `SYSTEM_HARD_STOP=0`; capture `HARD_STOP_EPOCH`
2. CAS `approval_queue: PENDING → APPROVED` (WHERE status='PENDING' AND version=?)
3. `approval_generation += 1`
4. INSERT exactly one `pending_executions {approval_id, approval_generation, approval_epoch, status='DRAFT_PENDING'}` — `UNIQUE(approval_id, approval_generation)`
5. INSERT audit (same txn)
`COMMIT`

Crash → full rollback; retry re-enters from `PENDING`. Stop enabled post-commit → invalidated at executor preflight via epoch mismatch.

---

## CONTROL-PLANE AUTHORIZATION (C2)

Privileged endpoints: emergency-stop toggle, private-contact add/remove, queue reopen, demo reset.
- `POST/PATCH/DELETE` only (never `GET`); owner-only; identity from server session, never request `user_id`; CSRF / origin protection.
- No generic "clear flag" endpoint — reopen is the only recovery, owner-only + reasoned.
- Every settings mutation commits with audit `{actor, old, new, reason, ts}` in same txn.

---

## AUDIT (C6 coupling)

- Material mutation + audit event commit in the SAME SQLite txn. If audit write fails → whole txn rolls back. A stateless rejected request whose audit fails → deny/hold with `AUDIT_WRITE_FAILED` (never proceed).
- Source-event idempotency keys (not event-type).

---

## REASON-CODE REGISTRY (complete)

`HARD_STOP_ACTIVE, PRIVATE_CONTACT_PROTECTED, DUPLICATE_SUPPRESSED, RATE_LIMIT_EXCEEDED, MANUAL_REVIEW_TIMEOUT, MANUAL_REVIEW_TIMEOUT_ESCALATED, INVALID_TRANSITION_REJECTED, INVALID_TRANSITION_RATE_LIMITED, REJECTION_REASON_TOO_LONG, AUDIT_WRITE_FAILED, EXECUTOR_BLOCKED_HARD_STOP, EXECUTOR_BLOCKED_PRIVATE_CONTACT, SYSTEM_HARD_STOP_ENABLED, SYSTEM_HARD_STOP_DISABLED, QUEUE_TRANSITION_LOCKED, QUEUE_REOPENED, HELD_STALE_EPOCH, EXECUTION_PROVEN_UNSENT, EXECUTION_OUTCOME_UNRESOLVED.`

---

## SCHEMA DELTA (consolidated)

- `system_state`: rows `SYSTEM_HARD_STOP`, `HARD_STOP_EPOCH`; cols `updated_at, updated_by, reason(≤500)`.
- `approval_queue`: status CHECK += `HELD, MANUAL_REVIEW_TIMEOUT, TRANSITION_LOCKED`; cols `version, approval_epoch, approval_generation, manual_review_generation, manual_review_started_at, transition_lock_reason, transition_locked_at`; `CHECK(length(status_reason)<=500)`.
- `pending_executions`: status CHECK += `HELD, SUPERSEDED`; cols `approval_epoch, approval_generation`; constraint `UNIQUE(approval_id, approval_generation)`; `CHECK(length(status_reason)<=500)`.
- `private_contacts` (new), `proposal_dedup` (new), `queue_transition_attempts` (new) — as defined above.
- `rate_limits` (exists) — business window + separate invalid-transition counter.

---

## DEMO-SAFETY

- Demo reset atomically reseeds hard-stop state + epoch, private-contact fixtures, rate counters, dedup records, timeout/lock fields, queue data. REFUSES while any item is `SENDING` or unresolved.
- Hard-stop toggle owner-only, audited. Queue card under stop: "Execution paused by emergency stop. This item remains queued and will not send automatically when the stop is lifted."

---

## BUILD ORDER (controlled, feature code only per part)

1. Schema migration (all tables/cols/constraints/CHECKs) + `db.py`
2. Control 1 — hard stop + epoch (`kernel.py`, settings endpoint, executor preflight)
3. Atomic admission — Controls 5 + 2 (dedup + rate limit, one shared function)
4. Control 3 — private contacts (entry gate + executor re-check)
5. Queue lifecycle — Controls 4 + 7 + R-REOPEN (`queue.py`)
6. Executor/Phase 5 bridge — Fence A + Fence B + atomic approval
7. Control 6 (reason cap) + API endpoints + audit wiring + demo reset
```
