# Phase 5 Part 4 — Test Plan (three-angle)

Provisional — finalize after the Part 4 brainstorm + stress test lock the design.
Part 4 = (1) execution safety filter (actions that never auto-execute regardless of
trust) + (2) structured Gmail error handling (TRANSIENT / PERMANENT / UNKNOWN).

Target: a decent batch in each of the three angles, mirroring test_phase_5 / test_templates.

---

## NORMAL — expected behaviour

1. `email.delete` at trust=100, Autonomous profile → decision is GATED (queued), NOT ALLOW.
2. External send to an unknown/new domain at max trust → GATED, not auto-executed.
3. External forward at max trust → GATED.
4. A non-filtered gated action (e.g. internal reply) at high trust → still auto-ALLOWs (filter doesn't over-block).
5. Gmail TRANSIENT error (429 rate limit) → execution marked retryable, not FAILED, no trust FAILURE.
6. Gmail PERMANENT error (invalid recipient / 400) → execution FAILED + trust FAILURE recorded.
7. Gmail UNKNOWN error (timeout / 5xx) → MANUAL_REVIEW.
8. Filter is downgrade/block-only: it never turns a GATED/BLOCK into ALLOW.
9. Safety-filtered action still executes normally AFTER explicit human approval (filter only blocks the AUTO path).

## HACKER — adversarial

1. External send disguised as internal via recipient-domain spoof → still filtered.
2. Lookalike / nested domain (`user@known.com.evil.com`, `known.com@evil.com`) → treated as unknown/external → filtered.
3. Display-name trick (`"Boss" <attacker@evil.com>`) → filter keys off the real address, not the display name.
4. action_type case/whitespace variants (`Email.Delete`, ` email.delete `) → still matched to the filter (or rejected upstream).
5. A TRANSIENT retry where the send ACTUALLY already completed → no double-send (reuses the crash-safe draft/idempotency guard).
6. Crafted input attempting to make the filter GRANT auto-execute → must be impossible (filter can only downgrade).
7. Unmapped / garbage Gmail error object → defaults to UNKNOWN → MANUAL_REVIEW (never silently passes as success).
8. Rapid repeated 429 → no retry-storm / infinite loop (reconcile-on-read, no background worker).
9. Prime Rule BLOCK + safety filter both apply → Prime Rule BLOCK wins; filter never downgrades a hard block to merely gated.
10. Multiple recipients where one is external-unknown → whole action filtered (no partial auto-send).
11. Empty / malformed recipient on a filtered action → fail closed, never auto-execute.

## STRICT TEACHER — exact-match nitpicks

1. Filter fires identically in EVERY profile (Strict, Balanced, Autonomous) — same downgrade.
2. Exact hierarchy position: SYSTEM_HARD_STOP > Prime Rule > FREE > policy gate > contact relax > **safety filter** > trust check. Verify the filter sits immediately before the trust auto-allow.
3. Gmail error → class mapping is exhaustive: every handled code maps to exactly one of TRANSIENT/PERMANENT/UNKNOWN; unmapped → UNKNOWN.
4. Filtered action at trust = threshold-1, = threshold, = threshold+1, and = 100 → ALWAYS GATED (never ALLOW at any value).
5. Non-filtered action at the threshold boundary → ALLOW at >= threshold, GATED below (existing behaviour unchanged by the filter).
6. `status_reason` / `failure_reason_code` on a filter downgrade is populated and explains "held: irreversible/external action," distinct from a trust-based gate.
7. A retryable (TRANSIENT) error sets a defined retry state with a tracked count — not a silent no-op.
8. TRANSIENT that keeps failing → escalates to MANUAL_REVIEW after a defined bound (no infinite retry).
9. The filtered-action set is exactly the agreed list (no more, no fewer) — assert membership precisely.
10. Decision trace records that the safety filter fired (auditability), separate from the trust layer.

up:: [[ARGUS]]
