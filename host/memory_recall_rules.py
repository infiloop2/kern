"""Editable rules for best-effort automatic memory recall.

Keep growing keyword lists, patterns and selection budgets here rather than
adding task-specific branches in admission/search code. Changes ship through
the normal host deployment; these are code settings, not operator UI fields.
Heuristics need not recognize every phrasing. Explicit memory search remains
available when automatic recall misses context.
"""

import re

# Shared with explicit weak search; query-only filler belongs in QUERY_FILLER.
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "could",
    "did", "do", "does", "for", "from", "had", "has", "have", "he", "her",
    "his", "how", "i", "if", "in", "into", "is", "it", "its", "may", "me",
    "might", "must", "my", "not", "of", "on", "or", "our", "ours", "shall",
    "she", "should", "so", "that", "the", "their", "them", "then", "they",
    "this", "to", "us", "was", "we", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "would", "you", "your",
})
MAX_QUERY_BYTES = 1000

QUERY_FILLER = frozenset({"please", "thanks", "thank", "okay", "ok", "yes", "well",
                          "continue", "proceed", "ahead", "also", "hello", "hi", "these", "those"})

HISTORY_EVENT_LIMIT = 12
CANDIDATE_LIMIT = 20
RELEVANT_PAGE_LIMIT = 5
VAGUE_TOPIC_LIMIT = 3

# Candidate reads use the existing search API's 100-result ceiling. Reject an
# invalid code setting immediately instead of silently degrading every recall.
if not 1 <= RELEVANT_PAGE_LIMIT <= CANDIDATE_LIMIT <= 100:
    raise ValueError("Recall budgets require 1 <= relevant pages <= candidates <= 100.")

AMBIGUOUS_SEARCH_OPERATORS = frozenset({"OR"})
ALL_CAPS_TOPICS = frozenset({"IT", "US", "OR", "MAY", "CAN"})
NAMED_STOPWORDS = frozenset({"May", "Will"})
MODAL_OPENERS = frozenset({"can", "may", "will"})
SUBJECT_PRONOUNS = frozenset({"i", "you", "we", "they", "he", "she", "it"})

CONTINUATION_VERBS = ("do", "deploy", "run", "test", "retry", "ship")
CONTINUATION_PHRASES = ("continue", "proceed", "go ahead", "do so")
QUALIFIED_CONTINUATIONS = ("continue", "proceed", "go ahead")
QUALIFIER_PREPOSITIONS = ("in", "on", "at", "using")
MODAL_PREFIXES = ("can", "could", "would", "will")
BACKWARD_REFERENCES = ("this", "that", "these", "those", "them")
REFERENCE_SUFFIXES = ("in", "on", "at", "to", "for", "with", "using", "today", "tomorrow", "please", "thanks")

# Regex patterns are also configurable for phrasing that needs more than a word.
BARE_CONTINUATION_PATTERN = r"\s*(?:please\s+)?go ahead[.!?\s]*"
STANDALONE_FOLLOWUP_PATTERNS = (r"\s*\?+\s*", r"\s*yes[.!]?\s*")
TOKEN_PATTERN = r"[^\W_]+(?:['’][^\W_]+)*(?:\+\+|#)?"
NEGATIVE_CONTRACTION_PATTERN = r"n['’]t$"
WORD_SUFFIX_PATTERN = r"['’](?:s|re|ve|ll|d|m)$"


def _choices(words: tuple[str, ...]) -> str:
    return "|".join(re.escape(word) for word in words)


POLITE_PREFIX_PATTERN = (r"\s*(?:please\s+)?(?:(?:" + _choices(MODAL_PREFIXES)
                         + r")\s+you\s+)?(?:please\s+)?")
IT_COMMAND_PATTERN = POLITE_PREFIX_PATTERN + r"(?:" + _choices(CONTINUATION_VERBS) + r")\s+it\b"
CONTINUATION_WORDS_PATTERN = r"\b(?:" + _choices(CONTINUATION_PHRASES) + r")\b"
QUALIFIED_CONTINUATION_PATTERN = (r"^\s*(?:please\s+)?(?:" + _choices(QUALIFIED_CONTINUATIONS)
                                  + r")\s+(?:" + _choices(QUALIFIER_PREPOSITIONS) + r")\b")
REFERENCE_PATTERN = (r"\b(?:(?i:" + _choices(BACKWARD_REFERENCES) + r")|it|It)\b"
                     + r"(?=\s*(?:$|[.!?]|(?i:" + _choices(REFERENCE_SUFFIXES) + r")\b))")
COMMAND_REFERENCE_PATTERN = POLITE_PREFIX_PATTERN + r"[^\W_]+\s+" + REFERENCE_PATTERN
