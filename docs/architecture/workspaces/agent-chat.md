# Chat workspace

Chat is Kern's built-in threaded conversation workspace. Its index selects
only direct thread ids (`thread-1`, `thread-2`, ...). Scheduled agents are
indexed separately by `schedules` under stable `schedule-N` identities.
Messages, activity, errors, provider sessions, runtime, model, and effort
remain authoritative in the host thread tables under the same direct id.

The admin sidebar loads active threads through
`GET /v1/workspace/chat/threads`, creates a blank composer with **New chat**,
shows running state, and provides an archived list. Archive is idle-only and
preserves the complete host event history. Restore makes the thread writable
again.

The trusted UI runs in the authenticated admin document. It calls the admin
API directly; there is no iframe or `postMessage` bridge. Each visited thread
keeps independent composer text, bounded event pages, and cursors. Opening a
thread always lands on its newest message; background polling preserves the
reader's current position. History loads newest-first and scrolling upward
fetches older pages. Activity visibility changes rendering only, never stored
history.

After working memory is cleared, Chat treats the latest
`thread.memory_cleared` event as the visible start of the thread and does not
offer pagination into earlier events. Those retained events are not deleted:
the admin and conversation-history APIs remain authoritative for audit and
history access.

The backend filters host thread-list queries with `prefix=thread-` and joins
the results to its own index. The filter is an optimization; the product row
is the authority for whether a thread is visible, named, archived, or writable.
Scheduled agents use a separate index request filtered with
`prefix=schedule-`; only active schedules appear there, and schedule threads
never appear in the Chat index or archive.
An idle message starts a turn and a running message steers when supported.
Runtime/model/effort changes remain idle-only and create the host's visible
session-change activity. Stop and archive are separate server-validated
operations.

Agents can search retained messages and read bounded pages from any host thread through
the typed `search_conversation_history` and `read_thread_history` MCP tools.
Those read-only calls use the peer-authenticated Workspace agent socket. Search
uses host-owned full-text, local vector, and timestamp indexes. A compact ONNX
model runs behind a network-isolated, socket-activated service and pgvector
stores derived message vectors; reciprocal-rank fusion favors exact lexical
evidence while adding conceptual matches. Lexical search remains available
during backfill or model failure. Search covers any retained host thread without
filtering through Chat's product index. Activity is excluded by default and,
when requested, is reduced to bounded normalized summaries. Historical content
is returned as untrusted data, never as a command protocol.

Markdown links whose target is an absolute path under
`/mnt/kern-agent/agent-home` render as file controls. The trusted Chat surface
normalizes the target to the Files explorer's virtual `/` root and asks the
host shell to open that file; paths outside the agent home remain plain text.
The file helper remains the authority for traversal and symlink rejection.
