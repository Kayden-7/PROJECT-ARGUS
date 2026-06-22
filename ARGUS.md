---
system: ARGUS
type: hub
---

# ARGUS
> Deterministic compliance engine with an AI interface layer. Competition project for TheFirst Spark Challenge (Challenge A1 — trust & permissions for autonomous agents). Submit by 25 June 2026 12pm SGT. See [[REALM-STATUS]] for current phase.

- **Source:** `C:\Users\kayde\PROJECT-ARGUS\`
- **Vault map:** [[CLAUDE]]
- **GitHub:** https://github.com/Kayden-7/PROJECT-ARGUS

## Core Idea
ARGUS separates intelligence from authority. The AI interprets intent and proposes actions; a deterministic Python policy engine enforces all permissions using fixed rules and append-only logs. No AI system makes safety or permission decisions. A reversible decision system, not an approval system — the undo window after approval is the key differentiator.

## Architecture (3 layers)
- **Layer 1 — Proposal:** GPT-4o parses intent → structured JSON proposal (never decides)
- **Layer 2 — Policy:** Python policy engine, all permission decisions (deterministic)
- **Layer 3 — Execution:** two-phase simulate → commit, crash-safe state machine

## Key Files
- [[README]] — project overview
- [[HANDOFF]] — frontend build guide
- [[PITCH]] — pitch content for the deck
- [[SUBMISSION]] — Devpost submission material
- [[DEFERRED]] — items parked for post-competition

## Knowledge Graph
- [[graphify-out/GRAPH_REPORT|GRAPH_REPORT]] — full knowledge graph report

## Connected Systems
- [[THE-ORDER]] — tracks ARGUS build state and review discipline ([[argus_project_state]], [[feedback_argus_always_review]], [[feedback_argus_chatgpt_models]], [[feedback_argus_test_design]])
- [[POLARIS]] — post-competition vision: rebuild ARGUS as a Claude-native tool (policy engine + trust ledger stay; Claude replaces GPT-4o; approval queue becomes a Claude tool call)
