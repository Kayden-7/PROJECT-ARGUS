# ARGUS — Pitch Source

Single source of truth for the pitch deck, demo video, and Devpost copy.
Built from real design decisions made across Phases 1–5. Keep the deck consistent with this.

Challenge: **TheFirst Spark Challenge — A1: Trust & permissions for autonomous agents.**
Submit: 25 June 2026, 12pm SGT. Deck ≤10 slides, demo ≤3 min.

---

## Target user (ICP — Level 4: role + moment + struggle)

> A **solo founder / freelance consultant whose livelihood runs on email** — who wants an AI assistant to handle their inbox, but won't hand it over because one wrong auto-sent message to a client (or a deleted thread) could cost them a relationship or a contract. So they keep doing it all manually.

Every ARGUS feature maps to this person's fear: GATED actions, the undo window, and MANUAL_REVIEW all exist to remove "the AI did something irreversible I didn't intend."

---

## The one-liner (TheFirst Spark template)

> We help **solo founders who depend on email** who struggle with **fear of an AI sending or deleting the wrong thing** during **delegating their inbox to an AI assistant** by solving **the fact that AI agents make their own permission decisions** through **a deterministic layer where the AI proposes but code decides and trust is earned** so they can **finally let AI handle email without risking a costly mistake.**

---

## Pitch Spine

- **Challenge:** Trust & permissions for autonomous agents (A1)
- **Target user:** Solo founders / freelancers who depend on email
- **Use case (the moment):** Delegating inbox triage + replies to an AI agent
- **Pain point (symptom):** Afraid it'll send the wrong thing, email the wrong person, or delete something important
- **Core bottleneck (ROOT CAUSE — attack this):** Today's AI agents *make their own permission decisions.* It's all-or-nothing: babysit every action (no time saved) or hand over full control (too risky). No deterministic, auditable layer decides what the AI may do.
- **Current alternative:** Babysit the AI (defeats the point) or refuse to use it (no benefit)
- **Value proposition:** Delegate to AI *without* the all-or-nothing risk — AI proposes, code decides, trust is earned gradually, and when unsure it stops and asks
- **How it works:** GPT-4o proposes → Python policy engine decides → crash-safe execution with an undo window

---

## Taglines

- **"Every AI assistant asks for permission. ARGUS manages how permission is *earned*."** (one-liner)
- **"ARGUS separates intelligence from authority."** (pitch statement)
- **"A reversible decision system, not an approval system."** (demo framing — the undo window)
- **"A verifiable delegation control system with human-readable trust memory — not a human-like assistant."** (frame/tone)

---

## The core invariant (the differentiator)

**"LLMs propose. Code decides."**
AI interprets intent and proposes actions; a deterministic Python engine makes *every* permission decision. **No AI system makes a safety or permission call.**

---

## The safety story (lead with this — emotional payoff)

> **"ARGUS never silently double-sends and never silently loses an email. On any uncertainty, it stops and asks the human."**

Crash-safe execution (drafts + atomic claim); when it can't be sure, it fails closed to MANUAL_REVIEW instead of guessing. This emerged from 4 rounds of adversarial stress testing.

**Credibility line:** we scoped honestly — not "guaranteed delivery" (Gmail can't promise that) but "never silently mis-classifies an uncertain outcome." Knowing that difference shows depth.

---

## Proof points by phase (how-it-works + credibility)

| From | Element |
|---|---|
| Phase 2 | 6-layer conflict-resolution hierarchy; FREE (9) vs GATED (11) taxonomy — dangerous actions are gated |
| Phase 3 | The **undo window** — approvals are reversible for a safety-hold period; 7-state machine |
| Phase 4 | **Trust is earned, not given** — 0–100, inertia, post-failure damping, recency weighting, ceilings (can't coast to blind auto-approval) |
| Phase 4 | **Recovery story** — after failure, trust visibly Recovers → Stabilizes → Rebuilds. The system *manages* recovery |
| Phase 5 | **Fail-closed crash-safe execution** (the safety story above) |

## Trust-psychology touches (depth, if room)
- **Explanation fingerprint** — one plain sentence per decision ("Allowed: stable history, 12/12 success")
- **Containment message** on every block — "Action blocked before execution. No external effects occurred."
- Clinical, auditable tone — not a chatbot, not a gamified score

---

## Slide-by-slide (TheFirst Spark recommended flow, ≤10 slides)

| # | Section | What to put |
|---|---|---|
| 1 | Title + one-liner | ARGUS + "Every AI assistant asks for permission. ARGUS manages how it's *earned*." |
| 2 | Challenge statement | A1: trust & permissions for autonomous agents |
| 3 | Target user & use case | The solo-founder ICP; make them a real person; survey quote |
| 4 | Pain point & bottleneck | Two boxes: Pain = "afraid AI sends/deletes wrong thing"; Bottleneck = "AI decides its own permissions; all-or-nothing." **Pitch hinges on this distinction.** |
| 5 | Current alternative | Babysit (no time saved) or don't use it (no benefit) — both fail |
| 6 | Solution & value prop | "LLMs propose. Code decides." + earned trust + "when unsure, it stops and asks" |
| 7 | How it works | 3-layer diagram + undo window |
| 8 | Live demo | command → proposal → decision → approve → execute → trust moves |
| 9 | Validation (bonus) | Interview findings — real people's AI-trust fears |
| 10 | Vision / ask | Claude-native rebuild, real product |

**Most important rule (from their guide):** the solution must attack the **bottleneck**, not just the pain. ARGUS's bottleneck = "the AI makes its own permission decisions." Hammer that every time.

---

## The 5 judge questions, answered
1. **Who?** Solo founders/freelancers who live in their inbox
2. **What problem?** Want AI on email but can't risk an irreversible mistake
3. **Bottleneck?** AI decides its own permissions; all-or-nothing; no auditable control layer
4. **How removed?** Separate intelligence from authority — AI proposes, code decides, trust earned, uncertainty pauses for review
5. **Why it fits A1?** ARGUS *is* a deterministic permission + earned-trust layer for agents
