# ARGUS — Devpost Submission Checklist

TheFirst Spark Challenge — A1: Trust & permissions for autonomous agents.
**Deadline: 25 June 2026, 12pm SGT.**

> ⚠️ Incomplete submissions will not be reviewed. Every link must work and every file must be viewable. Check all of these before submitting.

---

## Required fields

### 1. Project name and team members
- **Project name:** ARGUS
- **Team members:**
  - Kayden — backend (Flask, SQLite, policy engine, trust ledger, Gmail integration)
  - Baldwin — frontend (dashboard, approval cards, audit/trust UI)
- Status: completed

### 2. Problem statement and target user
- **Problem statement:** AI agents are increasingly given permission to *act* — send email, delete, schedule — but today the AI decides its own permissions. It's all-or-nothing: babysit every action (no time saved) or hand over full control (too risky). There's no deterministic, auditable layer that decides what an AI agent may do and lets it earn trust over time.
- **Target user:** A solo founder / freelance consultant whose livelihood runs on email, who wants an AI assistant to handle their inbox but won't, because one wrong auto-sent or deleted message could cost them a client.
- Source: see [PITCH.md](PITCH.md). Status: ✅ drafted

### 3. Solution summary (2–3 sentences)
> ARGUS is a deterministic permission and trust layer for AI agents: the AI proposes actions, but a Python policy engine makes every permission decision — no AI ever makes a safety call. Trust is earned gradually through a transparent ledger, dangerous actions are gated for human approval with a reversible undo window, and when the system is ever unsure it stops and asks rather than guessing. The result is an AI you can delegate email to without the all-or-nothing risk.
- Status: ✅ drafted (tighten before submit)

### 4. Demo video (YouTube, ≤3 min)
- Link: ⏳ TODO — record + upload
- Flow to record: command → GPT-4o proposal → policy decision → approval card → approve → crash-safe execution → trust updates → show a MANUAL_REVIEW "stop and ask" moment
- Status: ⛔ not started

### 5. Live demo link / prototype / screenshots
- Live demo (Replit): ⏳ TODO — deploy + confirm public URL works
- Fallback: screenshots of dashboard, approval queue, trust gauge, audit log
- Status: ⛔ not started (Replit set up at replit.com/@kaydenlow24/PROJECT-ARGUS)

### 6. Pitch deck (≤10 slides)
- Link: ⏳ TODO — build from [PITCH.md](PITCH.md) slide-by-slide, export shareable link
- Status: 🟡 content ready in PITCH.md, slides not built

### 7. Tools used
- **OpenAI GPT-4o** — proposal layer (intent → structured JSON); never makes decisions
- **Python / Flask** — policy engine, trust ledger, approval queue, execution layer
- **SQLite** — append-only trust events, queue, execution state
- **Gmail API (Google OAuth)** — crash-safe email execution
- **Google Calendar API** — (Phase 6, if built)
- **Replit + GitHub** — deployment + version control
- **ElevenLabs** — voice narration (Phase 10, only if time; access 24 Jun)
- Status: ✅ list ready; trim to what's actually shipped before submit

### 8. Optional (do if time — strengthens submission)
- **GitHub repo:** https://github.com/Kayden-7/PROJECT-ARGUS ✅ public
- **Technical architecture:** 3-layer (GPT-4o proposes → policy engine decides → crash-safe execution); diagram ⏳ TODO
- **User feedback:** survey results from AI-trust interviews ⏳ TODO (collecting)
- **Figma:** ⏳ optional, frontend friend

---

## Final pre-submit check (do all before 25 Jun 12pm SGT)
- [ ] Friend's name added
- [ ] Demo video uploaded, public, ≤3 min, link works
- [ ] Live demo URL loads for a stranger (test in incognito)
- [ ] Pitch deck exported, link viewable by anyone
- [ ] GitHub repo public and builds (`pip install -r requirements.txt` → `python app.py`)
- [ ] Solution summary tightened to 2–3 sentences
- [ ] Tools list matches what actually shipped
- [ ] Every link opened in a fresh browser to confirm it works
