---
type: community
cohesion: 0.11
members: 35
---

# Community 0

**Cohesion:** 0.11 - loosely connected
**Members:** 35 nodes

## Members
- [[6-Layer Conflict Resolution Hierarchy]] - concept - PITCH.md
- [[ARGUS Phase 1 Tests — Flask Skeleton & Database Run standalone python teststes]] - rationale - tests/test_phase_1.py
- [[ARGUS Phase 2 Tests — Validation & Policy Engine Run standalone python testste]] - rationale - tests/test_phase_2.py
- [[Determinism (Same Input, Same Outcome)]] - rationale - HANDOFF.md
- [[Policy Engine]] - concept - ARGUS.md
- [[_allow()]] - code - argus/policy_engine.py
- [[_block()]] - code - argus/policy_engine.py
- [[_check_field()]] - code - argus/validation.py
- [[_db()_1]] - code - argus/policy_engine.py
- [[_db_fail_gated()]] - code - argus/policy_engine.py
- [[_gated()]] - code - argus/policy_engine.py
- [[_read_action_count()]] - code - argus/policy_engine.py
- [[_read_active_profile_threshold()]] - code - argus/policy_engine.py
- [[_read_contact()]] - code - argus/policy_engine.py
- [[_read_overall_modifier()]] - code - argus/policy_engine.py
- [[_read_policy_gate()]] - code - argus/policy_engine.py
- [[_read_prime_rules()]] - code - argus/policy_engine.py
- [[_read_trust()]] - code - argus/policy_engine.py
- [[_step()]] - code - argus/policy_engine.py
- [[check()_2]] - code - tests/test_phase_1.py
- [[check()_3]] - code - tests/test_phase_2.py
- [[config.py]] - code - config.py
- [[evaluate()]] - code - argus/policy_engine.py
- [[is_hard_stop()]] - code - argus/kernel.py
- [[kern()]] - code - tests/test_phase_2.py
- [[kernel.py]] - code - argus/kernel.py
- [[kernel_entry()]] - code - argus/kernel.py
- [[sec()_1]] - code - tests/test_phase_1.py
- [[sec()_2]] - code - tests/test_phase_2.py
- [[set_hard_stop()]] - code - argus/kernel.py
- [[sv()]] - code - tests/test_phase_1.py
- [[test_phase_1.py]] - code - tests/test_phase_1.py
- [[test_phase_2.py]] - code - tests/test_phase_2.py
- [[validate_proposal()]] - code - argus/validation.py
- [[validation.py]] - code - argus/validation.py

## Live Query (requires Dataview plugin)

```dataview
TABLE source_file, type FROM #community/Community_0
SORT file.name ASC
```

## Connections to other communities
- 10 edges to [[_COMMUNITY_Community 2]]
- 4 edges to [[_COMMUNITY_Community 4]]
- 4 edges to [[_COMMUNITY_Community 9]]
- 3 edges to [[_COMMUNITY_Community 10]]
- 2 edges to [[_COMMUNITY_Community 1]]
- 2 edges to [[_COMMUNITY_Community 6]]
- 1 edge to [[_COMMUNITY_Community 12]]

## Top bridge nodes
- [[Policy Engine]] - degree 26, connects to 5 communities
- [[test_phase_2.py]] - degree 14, connects to 2 communities
- [[kernel_entry()]] - degree 12, connects to 2 communities
- [[kernel.py]] - degree 10, connects to 2 communities
- [[test_phase_1.py]] - degree 10, connects to 2 communities