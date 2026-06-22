# Graph Report - C:/Users/kayde/PROJECT-ARGUS  (2026-06-21)

## Corpus Check
- Corpus is ~29,062 words - fits in a single context window. You may not need a graph.

## Summary
- 259 nodes · 486 edges · 15 communities
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 15 edges (avg confidence: 0.77)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]

## God Nodes (most connected - your core abstractions)
1. `Policy Engine` - 26 edges
2. `create_app()` - 21 edges
3. `Gmail Client` - 20 edges
4. `Trust Ledger` - 19 edges
5. `Executor` - 19 edges
6. `evaluate()` - 17 edges
7. `record_event()` - 16 edges
8. `Database Module (SQLite)` - 15 edges
9. `get_trust()` - 14 edges
10. `kernel_entry()` - 12 edges

## Surprising Connections (you probably didn't know these)
- `Kernel Hard-Stop (SYSTEM_HARD_STOP)` --semantically_similar_to--> `Fail-Closed on Uncertainty`  [INFERRED] [semantically similar]
  HANDOFF.md → PITCH.md
- `ARGUS — Gmail client (Phase 5 Part 1)  Part 1 scope: OAuth connection + a simple` --rationale_for--> `Gmail Client`  [EXTRACTED]
  argus/gmail_client.py → HANDOFF.md
- `ARGUS — Gmail execution layer (Phase 5 Part 2)  Implements the locked, stress-te` --rationale_for--> `Executor`  [EXTRACTED]
  argus/executor.py → HANDOFF.md
- `Google Calendar Integration` --conceptually_related_to--> `Execution Layer (Two-Phase)`  [INFERRED]
  HANDOFF.md → ARGUS.md
- `Frontend Index Template` --references--> `Flask App (app.py)`  [INFERRED]
  templates/index.html → HANDOFF.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **3-Layer Proposal/Policy/Execution Flow** — argus_proposal_layer, argus_policy_engine, argus_execution_layer [EXTRACTED 1.00]
- **Approval Lifecycle (Queue, States, Undo)** — argus_approval_queue, argus_seven_state_machine, argus_undo_window, argus_expiry_countdown [INFERRED 0.85]
- **Trust Score Dynamics** — argus_trust_inertia, argus_post_failure_damping, argus_recency_weighting, argus_trust_ceiling, argus_trust_decay [EXTRACTED 1.00]

## Communities (15 total, 0 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.11
Nodes (25): 6-Layer Conflict Resolution Hierarchy, Determinism (Same Input, Same Outcome), is_hard_stop(), kernel_entry(), set_hard_stop(), Policy Engine, _allow(), _block() (+17 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (29): Core Bottleneck (AI Decides Own Permissions), Google Calendar Integration, Core Invariant: LLMs Propose, Code Decides, Crash-Safe State Machine, Demo Mode (Reset / Reseed), Emergency Stop, Execution Layer (Two-Phase), Execution State Machine (DRAFT_PENDING to COMPLETED) (+21 more)

### Community 2 - "Community 2"
Cohesion: 0.19
Nodes (19): create_app(), init_db(), build_auth_flow(), approve(), cancel(), _db(), enqueue(), expire_stale() (+11 more)

### Community 3 - "Community 3"
Cohesion: 0.12
Nodes (26): Gmail Client, _client_config(), create_draft(), draft_exists(), get_connected_email(), get_history_id(), get_service(), is_connected() (+18 more)

### Community 4 - "Community 4"
Cohesion: 0.15
Nodes (15): Trust Ledger, _db(), get_trust(), Returns effective trust score with recency weighting applied.     Raw accumulate, Records a trust event and updates trust_current + damping state + overall modifi, _read_action_count(), _read_active_profile(), _read_damping() (+7 more)

### Community 5 - "Community 5"
Cohesion: 0.12
Nodes (8): clean(), db(), DeadGmail, FakeGmail, make_approved(), one_exec(), ARGUS Phase 5 Tests — Gmail Execution Layer (Part 2) Run standalone: python test, Insert an APPROVED queue item; undo_elapsed=True puts approved_at in the past.

### Community 6 - "Community 6"
Cohesion: 0.11
Nodes (19): Action Taxonomy (FREE / GATED), Approval Queue, Audit Trail (Append-Only Log), ElevenLabs Voice Narration, Approval Expiry Countdown (300s), FREE Actions (9), GATED Actions (11), Permission Profiles (Strict / Balanced / Autonomous) (+11 more)

### Community 7 - "Community 7"
Cohesion: 0.23
Nodes (18): Executor, _advance_direct_action(), _advance_draft_action(), advance_executions(), _db(), _entities(), _mark_queue_executed(), promote_approved() (+10 more)

### Community 8 - "Community 8"
Cohesion: 0.16
Nodes (17): canonical_contact(), _count_paragraphs(), _count_sentences(), _count_words(), ARGUS — Message templates (Phase 5 Part 3)  Templates are a STYLE POLICY only: t, Upsert one template at its scope (contact/action_type, either may be None)., Deterministic precedence: exact -> contact-wide -> action-wide -> global -> defa, Produce the body-only style constraints for the model. Renders fixed strings (+9 more)

### Community 9 - "Community 9"
Cohesion: 0.19
Nodes (9): add_prime_rule(), db_exec(), db_one(), db_query(), ARGUS Integration Tests — Cross-Phase Flows + Chaos Run standalone: python tests, remove_prime_rule(), set_hard_stop(), set_profile() (+1 more)

### Community 10 - "Community 10"
Cohesion: 0.15
Nodes (6): Flask App (app.py), Database Module (SQLite), close_db(), Flask, Frontend Index Template, ARGUS Phase 5 Part 3 Tests — Message Templates Run standalone: python tests/test

### Community 11 - "Community 11"
Cohesion: 0.53
Nodes (8): bold(), cyan(), green(), main(), print_suite_output(), red(), run_suite(), yellow()

### Community 12 - "Community 12"
Cohesion: 0.50
Nodes (4): Containment Message (on Block), Decision Outcomes (ALLOW / GATED / BLOCK), Explanation Fingerprint, Reasoning Trace (Why Decision)

## Knowledge Gaps
- **21 isolated node(s):** `Two-Phase Execution (Simulate then Commit)`, `Emergency Stop`, `FREE Actions (9)`, `6-Layer Conflict Resolution Hierarchy`, `7-State Approval Machine` (+16 more)
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Executor` connect `Community 7` to `Community 1`, `Community 3`, `Community 4`, `Community 5`, `Community 10`?**
  _High betweenness centrality (0.191) - this node is a cross-community bridge._
- **Why does `Policy Engine` connect `Community 0` to `Community 1`, `Community 4`, `Community 6`, `Community 10`, `Community 12`?**
  _High betweenness centrality (0.190) - this node is a cross-community bridge._
- **Why does `Database Module (SQLite)` connect `Community 10` to `Community 0`, `Community 2`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 9`?**
  _High betweenness centrality (0.186) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `Executor` (e.g. with `Flask App (app.py)` and `Execution Layer (Two-Phase)`) actually correct?**
  _`Executor` has 3 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Two-Phase Execution (Simulate then Commit)`, `Emergency Stop`, `FREE Actions (9)` to the rest of the system?**
  _56 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.10756302521008404 - nodes in this community are weakly interconnected._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.07389162561576355 - nodes in this community are weakly interconnected._
