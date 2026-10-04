# Kern messages and transcript notices

[`host/agent_messages.py`](../../host/agent_messages.py) owns every model-facing
Kern preamble and the finite notice-kind catalog. It covers identity/memory
injection, session handoff, memory suggestions, scheduled triggers, approval
outcomes, restart recovery, and peer messages. Delivery, recall, permissions,
and history bounds remain with their owning services. The suggestion helpers
are shared with the follow-up memory-monitor PR; this base does not run a monitor.

## Event contract

All newly recorded Kern messages, context injections, and action outcomes use
`thread.notice`. Ordinary operator and assistant conversation uses
`thread.message`; general execution traces use `thread.activity`.

The standard event envelope contains `seq`, `event_id`, `timestamp`, `thread_id`,
`event_type`, and `payload`. Every notice payload contains `notice.kind` and
`notice.summary`. Summaries are a single line, at most **100 characters**.

| Category | Kinds | Additional payload fields |
| --- | --- | --- |
| Delivered input | `scheduled_trigger`, `approval_outcome`, `restart`, `agent_message` | `source: "user"`, `message`: exact provider input, including its routing preamble |
| Context | `history_transfer`, `memory_injection`, `memory_suggestion` | `message`: summary; memory notices retain `memory_page_ids` and `memory_recall_details`; transfers retain `historical_context` |
| Messaging and agents | `agent_message_sent`, `agent_spawned`, `agent_archived` | `notice.details`: outcome and request |
| Memory writes | `self_memory_saved`, `shared_memory_saved`, `shared_memory_deleted` | `notice.details`: outcome and request |
| Standing agents | `standing_agent_created`, `standing_agent_updated`, `standing_agent_deleted` | `notice.details`: outcome and request |
| Apps | `app_created`, `app_renamed`, `app_agent_updated`, `app_ui_published`, `app_data_changed`, `app_collection_changed` | `notice.details`: outcome and request |

There are no generic kinds. `INPUT_KINDS`, `CONTEXT_KINDS`, `ACTION_KINDS`, and
`NOTICE_KINDS` define the catalog centrally. No separate origin, status, or tool
payload field is added. The kind identifies the category; incoming messages
retain the authenticated sender header. Action errors appear explicitly at the
start of the summary and in the expanded details. Approval summaries use the
recorded decision and action description, never raw result JSON.

Only delivered input has `source: "user"`. Context and action notices have no
source and are excluded from subsequent provider handoffs. Search, embeddings,
message counts, unread markers, and handoffs include delivered notices.
Conversation-history reads default to ordinary messages and delivered notices.
`roles` selects conversation messages; `notice_kinds` independently selects
input, context, or action notices. Returned notices retain their kind and summary
instead of becoming user messages. `include_details` opts into bounded context
and action details. Filtering happens before pagination in every cursor mode.
Search retains the same typed provenance, with filters limited to messages and
delivered input. See the [history API](workspaces/workspace-agent-api.md) for
request fields, response shapes, and examples.

## Producers and presentation

The Workspace service supplies private `kern_notice: {kind, summary}` metadata
when delivering a trigger or peer message. Approval and restart producers also
supply their specific metadata. Admin rejects incoming Kern messages without
it. Operator input receives an explicit `operator` marker on `thread.message`;
pasting a Kern preamble does not change its classification.

The migration converts existing structured context rows to specific notice
kinds and extends message indexes to cover delivered notices. Older user-channel
messages without origin metadata remain as recorded, including in searches that
exclude automated triggers. Runtime text guessing does not invent provenance for
them.

[`workspace/agent_notices.py`](../../host/runtime/workspace/agent_notices.py)
records selected core mutations after dispatch, independently of provider tool
formats. Summaries identify resources and changes: `Published Portfolio UI`,
`Saved release guidance memory`, or `Updated Portfolio / holdings: 2 saved,
1 deleted`. Peer notices include names and short message previews. Data batches
include operation counts and a few affected paths. Names are resolved from
Workspace records; lookup failure uses the resource category and cannot block
action delivery. IDs and full request/result data remain in the expanded details.

Outcome recording uses the Workspace-only
`POST /v1/threads/{thread_id}/notices` route and cannot start a turn. It is best
effort: a recording failure goes to Host diagnostics and does not change an
already completed action's result. Encoded action details are bounded to 20 KB
with an explicit truncation marker; the receiving route caps the envelope at
24 KiB. History transfers preserve their existing 24 KiB beginning/end preview.

Chat and App history share `KernRichText.eventNotice` and `renderNotice`. Notices
stay visible with Activity off. Click, Enter, or touch opens literal details;
Escape or an outside click closes them. Polling preserves the opened notice,
and a working-memory clear hides older notices with the earlier transcript.
Reads and general integration/MCP calls remain ordinary Activity.

Run `tests/scripts/test test_agent_notices test_state test_migrate` for summary,
provenance, persistence, migration, and history boundaries. Run
`python3 tests/smoke-ui/admin_ui_smoke.py --port 8004 --scope notices` for desktop
and touch interaction in both transcript surfaces.
