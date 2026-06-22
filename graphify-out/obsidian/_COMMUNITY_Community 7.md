---
type: community
cohesion: 0.23
members: 18
---

# Community 7

**Cohesion:** 0.23 - loosely connected
**Members:** 18 nodes

## Members
- [[ARGUS — Gmail execution layer (Phase 5 Part 2)  Implements the locked, stress-te]] - rationale - argus/executor.py
- [[Executor]] - code - HANDOFF.md
- [[Promote ready approvals, then advance every in-flight execution one step.]] - rationale - argus/executor.py
- [[Single idempotent Gmail call (currently email.delete - trash).]] - rationale - argus/executor.py
- [[Turn APPROVED queue items whose undo window has elapsed into exactly one     pen]] - rationale - argus/executor.py
- [[Write the execution trust event once, keyed by execution_id (idempotent).]] - rationale - argus/executor.py
- [[_advance_direct_action()]] - code - argus/executor.py
- [[_advance_draft_action()]] - code - argus/executor.py
- [[_db()]] - code - argus/executor.py
- [[_entities()]] - code - argus/executor.py
- [[_mark_queue_executed()]] - code - argus/executor.py
- [[_read_undo_window()]] - code - argus/executor.py
- [[_to_manual_review()]] - code - argus/executor.py
- [[_trust_written()]] - code - argus/executor.py
- [[_write_execution_trust()]] - code - argus/executor.py
- [[advance_executions()]] - code - argus/executor.py
- [[promote_approved()]] - code - argus/executor.py
- [[reconcile()]] - code - argus/executor.py

## Live Query (requires Dataview plugin)

```dataview
TABLE source_file, type FROM #community/Community_7
SORT file.name ASC
```

## Connections to other communities
- 2 edges to [[_COMMUNITY_Community 10]]
- 2 edges to [[_COMMUNITY_Community 4]]
- 2 edges to [[_COMMUNITY_Community 2]]
- 1 edge to [[_COMMUNITY_Community 1]]
- 1 edge to [[_COMMUNITY_Community 3]]
- 1 edge to [[_COMMUNITY_Community 5]]

## Top bridge nodes
- [[Executor]] - degree 19, connects to 5 communities
- [[_write_execution_trust()]] - degree 7, connects to 1 community
- [[reconcile()]] - degree 6, connects to 1 community