"""Budgets and routing envelope normalization for bounded-context memory recall."""

# A UTF-8 byte budget, not a tokenizer count. BGE applies its own token ceiling.
MAX_QUERY_BYTES = 1000

HISTORY_EVENT_LIMIT = 12
CANDIDATE_LIMIT = 20
RELEVANT_PAGE_LIMIT = 5
RECALL_RERANK_TIMEOUT_SECONDS = 1.2

# Candidate reads use the existing search API's 100-result ceiling. Reject an
# invalid code setting immediately instead of silently degrading every recall.
if not 1 <= RELEVANT_PAGE_LIMIT <= CANDIDATE_LIMIT <= 100:
    raise ValueError("Recall budgets require 1 <= relevant pages <= candidates <= 100.")

PEER_MESSAGE_PREFIX_PATTERN = (
    r'\AThis is a message from another agent, not the operator\.\n'
    r'Sender thread: ((?:app|thread|schedule)-[1-9][0-9]*)\n'
    r'To reply, use send_agent_message with thread_id: "\1"\. '
    r'Reply only when needed\.\n\n(?:---\n\n)?'
)
