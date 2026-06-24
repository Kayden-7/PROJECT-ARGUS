# ARGUS — Backend Fixes Needed (from frontend QA) · 2026-06-24

Found while building/verifying the Phase 8 frontend. These are **not fixed** —
each needs a backend decision/change from Kayden before the frontend can do
anything more than show the problem. Two genuinely need backend work; two
turned out to be frontend-only and are already fixed (listed at the bottom
for completeness, since they came from the same bug reports).

---

## 1. Execution-level MANUAL_REVIEW has no way to be resolved

**Symptom:** An execution sits in `MANUAL_REVIEW` in Live Execution
(e.g. *"Crashed during send — outcome unknown. Verify in Gmail Sent
folder."* or *"Ambiguous draft creation — verify in Gmail."*) forever. There
is no Approve/Retry/Resolve action anywhere in the product for it.

**Why this isn't a frontend gap:** I checked — `reopen()`
(`argus/queue.py`) only acts when the **queue** item's status is already
`HELD` / `MANUAL_REVIEW_TIMEOUT` / `TRANSITION_LOCKED`. When an *execution*
(`pending_executions.status`) goes to `MANUAL_REVIEW` or `FAILED`, nothing
updates the linked **queue** item at all — it just sits at `APPROVED`
forever, which is never a reopen-eligible state. There's no endpoint to call
here; I can't wire a button to something that doesn't exist.

**This matches a gap your own `DEFERRED.md` already names:**
> Phase 8 Part 6 — Fence B (FAILED→queue HELD bridge) — deferred after
> stress test... nothing in the live code currently RESOLVES an execution to
> FAILED... With no producer of proven-unsent FAILED, Fence B has no
> trigger to fire on.

That note focuses on the `FAILED` case; in practice I'm also seeing live
`MANUAL_REVIEW` executions (crash-recovery, orphan-draft-guard) that have the
exact same dead-end — not just the `FAILED` path.

**What's needed (your call on direction):**
- Either build the Fence B bridge so a resolved execution outcome
  (`FAILED`, or a human-confirmed `MANUAL_REVIEW` outcome) moves the linked
  queue item to `HELD` with a real reason, making it reopen-eligible — *or*
- A new endpoint that lets an owner directly resolve an execution-level
  `MANUAL_REVIEW`/`FAILED` row after they've manually checked Gmail (e.g.
  "I confirmed in Gmail this sent" → mark `COMPLETED`; "confirmed it didn't
  send" → supersede + allow a fresh approval).

Once either exists, the frontend changes needed are small (a button + a
reason prompt) — same pattern already built for queue-level reopen.

---

## 2. ALLOW outcomes for trust-promoted GATED actions never execute

**Symptom:** Once an action type's trust rises above the active profile's
threshold, the policy engine returns `ALLOW` (not `GATED`) even for
`email.reply` / `email.send.external` — actions that are normally GATED.
The decision narrative literally says *"Trust X meets threshold Y.
Auto-executing."* — but nothing actually sends anything. No draft, no Gmail
call, no queue item. Just a trust `SUCCESS` event recorded for a send that
never happened. Confirmed in `app.py`'s `_route_proposal()`: the `ALLOW`
branch only does `record_event(...)` and returns — there is no call into
the executor or Gmail anywhere in that branch.

**Reproduction:** keep approving/sending the same action type until its
trust crosses the profile threshold (Balanced = 70, Autonomous = 40);
the next proposal of that type comes back `ALLOW` instead of `GATED`, the
UI says it auto-executed, and the email never arrives.

**What's needed:** route `ALLOW` outcomes for non-`FREE_ACTIONS` through the
same executor pipeline `GATED` → `APPROVED` already uses (create the
`pending_executions` row immediately rather than waiting on the queue), or —
if that's not ready — change the narrative text so it stops claiming
"Auto-executing" for an action type that the executor can't actually reach
yet. Right now it's a false success claim, which is worse than just being
slow.

---

## Already fixed — frontend-only, no backend change needed (FYI)

These came from the same bug reports but turned out not to need you:

- **Queue-level `MANUAL_REVIEW` had no Approve/Reject buttons.** (Different
  from #1 above — this is a *queue* item awaiting a decision under extra
  scrutiny, not an *execution* that already passed approval.) The backend
  already supports approving/rejecting it (`VALID_TRANSITIONS["MANUAL_REVIEW"]`
  includes `APPROVED`/`REJECTED`) — the frontend just never showed the
  controls, only ever checking for `PENDING`. Fixed and verified live
  (injected a `MANUAL_REVIEW` row, confirmed `approve()` works on it).
- **Workbench's Policy Decision / Authorisation cards stayed stale after
  approving the same item from the Executions page.** Pure frontend state
  sync — the two pages' approve buttons didn't know about each other. Now
  tracks which item the Workbench cards belong to and clears them from
  either approve path.
