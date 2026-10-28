"""Operator-only PostgreSQL storage snapshot and retention explanations.

Read catalog estimates and allocated sizes without scanning stored records.
Policy copy describes existing cleanup; it never configures or runs it.
Keep this inventory and database-retention.md aligned with policy changes.
"""
from typing import Any

from host.runtime.core import db
from host.runtime.core.state import (
    AGENT_EVENT_LIMIT, NETWORK_EVENT_LIMIT, TOOL_EVENT_LIMIT, HOST_DIAGNOSTIC_LIMIT,
    BEDROCK_USAGE_RETAIN_DAYS, HOST_INFERENCE_USAGE_RETAIN_DAYS,
    CONVERSATION_EMBEDDING_MESSAGE_LIMIT,
)

# Only whole-table history targets belong here. Per-owner quotas and limits on
# subsets (e.g. decided approvals) cannot be compared with a table's row count.
ROW_TARGETS = {
    "agent_events": AGENT_EVENT_LIMIT,
    "network_events": NETWORK_EVENT_LIMIT,
    "tool_events": TOOL_EVENT_LIMIT,
    "host_diagnostics": HOST_DIAGNOSTIC_LIMIT,
}

# table: (what is stored, what removes or replaces it)
POLICIES: dict[str, tuple[str, str]] = {
    "schema_migrations": ("Applied database migrations", "No automatic deletion."),
    "config": ("Host configuration", "One current record; replaced during provisioning."),
    "operator_connections": ("SSH and tunnel configuration", "One current record per connection mode; replaced when configured."),
    "counters": ("Lifetime usage and activity totals", "No automatic pruning. Totals survive deletion of detailed history."),
    "thread_sessions": ("Agent runtime session mappings", "Automatically removes oldest unreferenced mappings beyond 100,000 per runtime. Retained messages, activity or usage protect their mappings."),
    "agent_events": ("Conversation messages and agent activity", f"Automatically deletes oldest messages and activity across all conversations, aiming to keep the newest {AGENT_EVENT_LIMIT:,} events. Gaps in event numbering can leave fewer rows; cleanup runs in batches. Keeping a Chat does not keep its messages indefinitely."),
    "conversation_message_embeddings": ("Derived conversation search vectors", f"Keeps vectors for the newest {CONVERSATION_EMBEDDING_MESSAGE_LIMIT:,} user/agent messages globally, with batched pruning. Source-message deletion also removes vectors. Pruning vectors does not delete messages."),
    "oauth_logins": ("In-progress provider sign-ins", "One current login per supported runtime; replaced or removed by the sign-in flow."),
    "provider_accounts": ("Linked provider accounts", "No age-based deletion. Explicit account reset removes the selected account."),
    "network_events": ("Network allow/deny history", f"Automatically deletes oldest network records aiming to keep the newest {NETWORK_EVENT_LIMIT:,} events. Gaps in event numbering can leave fewer rows; cleanup runs in batches."),
    "network_policy": ("Network policy", "One current record; replaced when policy is saved."),
    "allowed_domains": ("Allowed network domains", "Replaced when network policy is saved."),
    "domain_methods": ("Allowed HTTP methods", "Replaced with the parent domain policy."),
    "domain_path_guards": ("Allowed URL path patterns", "Replaced with the parent domain policy."),
    "proxy_provider_pins": ("Active provider account pins", "Reconciled with current runtime settings; clearing a pin does not delete the linked account."),
    "managed_integrations": ("Network integration settings", "Current configuration from the bundled catalog; replaced when policy is saved."),
    "github_repositories": ("Allowed GitHub repositories", "Replaced when network policy is saved."),
    "secret_keys": ("Stored-secret encryption key", "No automatic deletion."),
    "github_credential": ("GitHub credential", "No automatic expiry cleanup; replaced or explicitly cleared by the operator."),
    "proxy_github_token": ("Working GitHub token", "Replaced on credential refresh or cleared with the GitHub connection."),
    "github_repo_audit": ("GitHub repository audit results", "One current result per configured repository; removed when the repository is no longer configured."),
    "github_settings": ("GitHub approval settings", "One current record; replaced when configured."),
    "pending_pushes": ("GitHub push approvals", "Automatically keeps newest 100 resolved pushes. Pending pushes are not pruned; at most 10 may be pending."),
    "claude_settings": ("Claude settings", "One current record; replaced when configured."),
    "enabled_tools": ("Enabled tools", "Removed when a tool is disabled; no age-based pruning."),
    "tool_config": ("Tool configuration", "Current values per tool and key; replaced or explicitly cleared."),
    "tool_credentials": ("Connected tool accounts", "No automatic age pruning or connection-count cap. Disconnect removes the selected connection."),
    "tool_secrets": ("Tool private secrets", "One current object per tool, at most 16 KiB; replaced or explicitly cleared. No automatic expiry."),
    "tool_approval_risk_assessments": ("Approval risk annotations", "Automatically removed when the parent approval is deleted."),
    "tool_costs": ("Reported tool charges", "No automatic pruning; retained independently of tool audit history."),
    "tool_cost_daily": ("Daily tool cost totals", "No automatic pruning; retained independently of tool audit history."),
    "host_inference_providers": ("Host AI provider configuration", "One current record per supported provider; replaced or explicitly cleared."),
    "host_inference_usage": ("Daily Host AI usage", f"Automatically deletes daily counters older than {HOST_INFERENCE_USAGE_RETAIN_DAYS} days, on the next metered write each UTC day."),
    "xai_video_storage": ("Video storage configuration", "One optional record; replaced or explicitly cleared."),
    "tool_approvals": ("Tool approvals and their results", "Automatically deletes non-pending approvals outside the newest 10,000 approval-number window. Pending approvals expire after 24 hours; at most 1,000 may be pending."),
    "tool_events": ("Tool calls and connection activity", f"Automatically deletes oldest tool audit records aiming to keep the newest {TOOL_EVENT_LIMIT:,} events. Gaps in event numbering can leave fewer rows; cleanup runs in batches."),
    "bedrock_credentials": ("Bedrock connection", "One current record; replaced or explicitly disconnected."),
    "turn_usage": ("Detailed token usage per turn", "Automatically deletes records last measured more than 90 days ago. Lifetime token totals remain in counters."),
    "bedrock_usage": ("Daily Bedrock usage", f"Automatically deletes daily counters older than {BEDROCK_USAGE_RETAIN_DAYS} days."),
    "host_diagnostics": ("Errors, warnings and maintenance reports", f"Automatically keeps newest {HOST_DIAGNOSTIC_LIMIT:,} coalesced reports; oldest reports are deleted. Pruning is batched."),
    "admin_passkey_config": ("Administrator passkey identity", "One current record; no age-based deletion."),
    "admin_passkeys": ("Administrator passkey", "At most one passkey; explicit reset before replacement. No age-based deletion."),
    "conversation_search_state": ("Conversation search generations", "One current record; updated as search state changes."),
    "conversation_embedding_queue": ("Pending conversation indexing", "Removed after indexing or five failed attempts, when the source message is deleted, or when outside the newest 250,000-message window."),
    "workspace_navigation_order": ("Sidebar ordering", "Current App and Standing agent ordering; reconciled with existing items."),
    "workspace_onboarding_dismissal": ("Getting-started dismissal", "One optional record; no automatic deletion."),
    "swarm_agent_ai": ("Current agent summaries", "Replaced or invalidated by agent activity; removed when the parent runtime session is deleted."),
    "swarm_interaction_days": ("Daily agent interaction counts", "Keeps today and the preceding six UTC dates. Older counts are excluded on reads and deleted on accepted delivery."),
    "web_app_collection_state": ("App collection quota totals", "One record per App; removed with its App. No age-based pruning."),
    "web_app_collection_rows": ("Current App collection data", "Limits per App: 64 collections, 100,000 rows, 50 MiB. Excess writes are rejected, not automatically pruned. Rows change through App edits or restoration; deletion of an App removes its rows."),
    "workspace_seen": ("Conversation read markers", "Schedule markers are removed after the deleted schedule's 90-day restore window. Other markers follow their retained Chat or App."),
    "chat_threads": ("Chat names and archive state", "Creation blocked at 10,000 Chats; no age-based deletion of Chat records. Messages have separate automatic retention in agent_events."),
    "web_apps": ("Current App UI and data", "Creation blocked at 100 Apps; no age-based deletion. Current data changes through App edits or restoration; older checkpoints have separate retention."),
    "private_notes": ("Current agent private notes", "Creation blocked at ten notes per agent. Updates replace the previous text without revision history; explicit deletion removes a note. No automatic age pruning."),
    "memory_pages": ("Current self and shared memories", "Creation blocked at 10,000 retained pages. Only pages already marked deleted are permanently removed after 90 days; active pages do not expire."),
    "memory_page_revisions": ("Memory edit history", "Automatically keeps newest 100 revisions per page; older revisions are deleted. Deleting the parent page also removes its revisions."),
    "memory_page_embeddings": ("Derived memory search vectors", "One current vector per page/model; replaced on updates and removed when the page is soft-deleted. Does not delete the memory text."),
    "memory_page_links": ("Current shared-memory links", "Rebuilt on page edits, at most 100 outgoing links per page; removed with the source page."),
    "schedules": ("Standing agent definitions", "Creation blocked at 100 active agents. Deleted definitions are permanently removed after 90 days; conversation history follows agent_events retention."),
    "schedule_revisions": ("Standing agent edit history", "Automatically keeps newest 100 revisions per agent; older revisions are deleted. Deleting the parent definition also removes its revisions."),
    "web_app_revisions": ("App recovery checkpoints", "Per App: keeps newest 5 exact revisions, then one per four hours during day one and one per day through day seven, at most 17. Older checkpoints are automatically deleted; the newest 5 remain even when older than seven days."),
    "web_app_ui_versions": ("Shared App recovery UI", "Automatically removes versions no retained App checkpoint needs; also removed with the App."),
    "web_app_document_versions": ("Shared App recovery documents", "Automatically removes versions no retained App checkpoint needs; also removed with the App."),
    "web_app_collection_versions": ("App collection recovery history", "Keeps current row intervals and history needed by retained App checkpoints. Other history is automatically removed; also removed with the App."),
    "auto_approval_policies": ("Tool auto-approval instructions", "Current policy per tool action; replaced or explicitly cleared."),
    "auto_approval_reviews": ("Automatic approval review history", "Automatically removed when the related tool approval or GitHub push approval is deleted."),
    "auto_approval_settings": ("Auto-approval sleep window", "One current record; replaced when configured."),
    "browser_settings": ("Browser connection settings", "One current record; replaced when configured."),
    "browser_accounts": ("Saved Browser accounts and sessions", "No age-based deletion. Disconnect removes an account. Saving is rejected above 1 GB total encrypted state; existing saved accounts are not automatically pruned."),
    "tool_action_runs": ("Daily tool action limits", "One counter per limited action; resets on the next run on a new UTC day."),
    "agent_reminders": ("One-time reminders and outcomes", "Daily cleanup keeps newest 1,000,000 finished reminders globally. Pending reminders are not pruned by that limit; at most five per agent. Permanently deleting a Standing agent also deletes its reminders."),
}


def snapshot() -> dict[str, Any]:
    with db.transaction() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SET LOCAL statement_timeout = '3s'")
        cur.execute("SET LOCAL lock_timeout = '500ms'")
        cur.execute("SELECT pg_database_size(current_database())")
        database_row = cur.fetchone()
        assert database_row is not None
        cur.execute(
            "SELECT c.relname, CASE WHEN c.reltuples < 0 THEN NULL"
            " ELSE c.reltuples::bigint END, pg_total_relation_size(c.oid)"
            " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relkind = 'r'"
            " ORDER BY 3 DESC, c.relname"
        )
        rows = cur.fetchall()
    return {
        "database_bytes": database_row[0],
        "tables": [
            {
                "name": name,
                "estimated_rows": estimated_rows,
                "total_bytes": total_bytes,
                "description": POLICIES.get(name, ("", ""))[0],
                "retention": POLICIES.get(name, ("", "Policy not documented; do not assume this data is kept indefinitely."))[1],
                "row_target": ROW_TARGETS.get(name),
            }
            for name, estimated_rows, total_bytes in rows
        ],
    }
