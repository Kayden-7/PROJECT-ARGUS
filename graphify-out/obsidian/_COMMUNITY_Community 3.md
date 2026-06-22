---
type: community
cohesion: 0.12
members: 26
---

# Community 3

**Cohesion:** 0.12 - loosely connected
**Members:** 26 nodes

## Members
- [[ARGUS — Gmail client (Phase 5 Part 1)  Part 1 scope OAuth connection + a simple]] - rationale - argus/gmail_client.py
- [[Addremove labels — convergent, safe to replay (re-adding is a no-op).]] - rationale - argus/gmail_client.py
- [[Build the OAuth client config from .env (never hardcoded).]] - rationale - argus/gmail_client.py
- [[Create a Gmail draft. Returns the durable draft id (stable across crashes).]] - rationale - argus/gmail_client.py
- [[Current mailbox historyId — saved before a send as review evidence.]] - rationale - argus/gmail_client.py
- [[Gmail Client]] - code - HANDOFF.md
- [[Load token from disk, refreshing it if expired. Returns None if absent.]] - rationale - argus/gmail_client.py
- [[Move a message to trash (reversible, inspectable). Never permanent delete.]] - rationale - argus/gmail_client.py
- [[Part 1 ONLY a plain direct send to prove Gmail connectivity.     Not crash-safe]] - rationale - argus/gmail_client.py
- [[Send an existing draft. Gmail consumes the draft and returns the new sent     Me]] - rationale - argus/gmail_client.py
- [[True if the draft still exists (i.e. has NOT been sentconsumed).]] - rationale - argus/gmail_client.py
- [[_client_config()]] - code - argus/gmail_client.py
- [[_load_credentials()]] - code - argus/gmail_client.py
- [[_raw_message()]] - code - argus/gmail_client.py
- [[_save_credentials()]] - code - argus/gmail_client.py
- [[create_draft()]] - code - argus/gmail_client.py
- [[draft_exists()]] - code - argus/gmail_client.py
- [[get_connected_email()]] - code - argus/gmail_client.py
- [[get_history_id()]] - code - argus/gmail_client.py
- [[get_service()]] - code - argus/gmail_client.py
- [[is_connected()]] - code - argus/gmail_client.py
- [[modify_labels()]] - code - argus/gmail_client.py
- [[save_credentials_from_flow()]] - code - argus/gmail_client.py
- [[send_draft()]] - code - argus/gmail_client.py
- [[send_test_email()]] - code - argus/gmail_client.py
- [[trash_message()]] - code - argus/gmail_client.py

## Live Query (requires Dataview plugin)

```dataview
TABLE source_file, type FROM #community/Community_3
SORT file.name ASC
```

## Connections to other communities
- 10 edges to [[_COMMUNITY_Community 2]]
- 1 edge to [[_COMMUNITY_Community 1]]
- 1 edge to [[_COMMUNITY_Community 7]]
- 1 edge to [[_COMMUNITY_Community 5]]

## Top bridge nodes
- [[Gmail Client]] - degree 20, connects to 4 communities
- [[send_test_email()]] - degree 6, connects to 1 community
- [[get_connected_email()]] - degree 4, connects to 1 community
- [[is_connected()]] - degree 4, connects to 1 community
- [[save_credentials_from_flow()]] - degree 4, connects to 1 community